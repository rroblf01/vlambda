"""Local capability broker for sandboxed lambdas.

The broker is a Unix-socket JSON-RPC server owned by the vlambda process. Each
invocation gets a short-lived token that is scoped to one lambda. The runner
uses the broker for KV access (and, in 0.4.0, controlled egress) without ever
receiving a network socket.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import secrets
import socket
import socketserver
import ssl
import sys
import threading
import time
import urllib.parse

from vlambda import egress_store, kv_store

_MAX_LINE = 1024 * 1024
_MAX_OPS = 200
_MAX_HTTP_CALLS = 20
_MAX_HTTP_BODY = 1024 * 1024
_MAX_HTTP_RESPONSE = 2 * 1024 * 1024
_TOKEN_TTL = 3600.0

_tokens: dict[str, dict] = {}
_tokens_lock = threading.Lock()
_server = None
_thread = None
SOCKET_PATH = ""


class BrokerError(RuntimeError):
    pass


def _default_socket() -> str:
    base = "/data/broker" if os.path.isdir("/data") else "/tmp/opencode"
    return os.path.join(base, f"vlambda-{os.getpid()}.sock")


def _socket_path() -> str:
    return os.environ.get("VLAMBDA_BROKER_SOCKET") or _default_socket()


def issue(slug: str, request_id: str = "") -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _tokens_lock:
        if len(_tokens) > 1000:
            for key in [key for key, value in _tokens.items() if value["expires"] <= now]:
                _tokens.pop(key, None)
        _tokens[token] = {
            "slug": slug,
            "request_id": request_id,
            "expires": time.time() + _TOKEN_TTL,
            "ops": 0,
        }
    return token


def release(token: str | None) -> None:
    if not token:
        return
    with _tokens_lock:
        _tokens.pop(token, None)


def clear_tokens() -> None:
    with _tokens_lock:
        _tokens.clear()


def _context(token: str, operation: str = "") -> dict:
    with _tokens_lock:
        context = _tokens.get(token)
        if context is None:
            raise BrokerError("invalid capability token")
        if context["expires"] <= time.time():
            _tokens.pop(token, None)
            raise BrokerError("capability token expired")
        context["ops"] += 1
        if context["ops"] > _MAX_OPS:
            raise BrokerError("capability operation quota exceeded")
        if operation == "http.fetch":
            context["http"] = context.get("http", 0) + 1
            if context["http"] > _MAX_HTTP_CALLS:
                raise BrokerError("egress call quota exceeded")
        return dict(context)


def _audit(context: dict, operation: str, message: str) -> None:
    try:
        from vlambda.metrics import record_log

        record_log(
            str(context.get("request_id", "")),
            str(context.get("slug", "")),
            "warn",
            f"capability {operation}: {message}",
        )
    except Exception:
        pass


def _wire_value(value):
    if isinstance(value, (bytes, bytearray)):
        import base64

        return {
            "binary": True,
            "value_b64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    return {"binary": False, "value": value}


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTP connection that connects to an already validated IP address."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self._pinned_ip = ip

    def connect(self):
        self.sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection pinned to an IP, with SNI and certificate checks for the host."""

    def __init__(self, host: str, ip: str, port: int, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "0").lower() in ("1", "true", "yes", "on")


def _http_fetch(context: dict, payload: dict) -> dict:
    url = str(payload.get("url", ""))
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise BrokerError(f"invalid url: {error}") from error
    if parsed.scheme not in ("http", "https"):
        raise BrokerError("only http and https URLs are allowed")
    if parsed.username or parsed.password:
        raise BrokerError("URL credentials are not allowed")
    if parsed.fragment:
        raise BrokerError("URL fragments are not allowed")
    if parsed.scheme == "http" and not _bool_env("VLAMBDA_EGRESS_ALLOW_HTTP"):
        raise BrokerError("http egress is disabled")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise BrokerError("URL has no host")
    if not egress_store.host_allowed(context["slug"], host):
        raise BrokerError(f"host not allowed: {host}")
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    allow_private = _bool_env("VLAMBDA_EGRESS_ALLOW_PRIVATE")
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise BrokerError(f"cannot resolve {host}: {error}") from error
    if not addresses:
        raise BrokerError(f"cannot resolve {host}")
    if not allow_private:
        for info in addresses:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError:
                raise BrokerError(f"invalid resolved address for {host}")
            if not address.is_global:
                raise BrokerError(f"egress target is not public: {host}")
    method = str(payload.get("method", "GET")).upper()
    if method not in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"):
        raise BrokerError("method not allowed")
    supplied = payload.get("headers") or {}
    if isinstance(supplied, list):
        supplied = dict(supplied)
    if not isinstance(supplied, dict) or len(supplied) > 32:
        raise BrokerError("invalid headers")
    headers = {}
    for key, value in supplied.items():
        key = str(key)
        value = str(value)
        lowered = key.lower()
        if not key or any(char in key + value for char in ("\r", "\n", "\x00")):
            raise BrokerError("invalid header")
        if lowered in (
            "host", "content-length", "connection", "transfer-encoding",
            "upgrade", "keep-alive", "te", "trailer", "proxy-authorization",
        ):
            continue
        headers[key] = value
    body = None
    if "json" in payload:
        try:
            body = json.dumps(
                payload["json"], ensure_ascii=False, allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise BrokerError(f"invalid json body: {error}") from error
        headers.setdefault("Content-Type", "application/json")
    elif "data_b64" in payload:
        try:
            body = base64.b64decode(str(payload["data_b64"]).encode("ascii"))
        except Exception as error:
            raise BrokerError("invalid data_b64") from error
    elif "body" in payload:
        body = str(payload["body"]).encode("utf-8")
    if body is not None and len(body) > _MAX_HTTP_BODY:
        raise BrokerError(f"request body exceeds {_MAX_HTTP_BODY} bytes")
    if method in ("GET", "HEAD"):
        body = None
    try:
        timeout = float(payload.get("timeout", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    timeout = min(10.0, max(0.1, timeout))
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    last_error = None
    status = 0
    raw = b""
    response_headers = {}
    for info in addresses:
        address = info[4][0]
        connection = None
        try:
            if parsed.scheme == "https":
                connection = _PinnedHTTPSConnection(host, address, port, timeout)
            else:
                connection = _PinnedHTTPConnection(host, address, port, timeout)
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            status = int(response.status)
            raw = response.read(_MAX_HTTP_RESPONSE + 1)
            response_headers = dict(response.getheaders())
            break
        except (OSError, http.client.HTTPException) as error:
            last_error = error
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
    else:
        raise BrokerError(
            f"egress request failed: {type(last_error).__name__ if last_error else 'unknown error'}"
        )
    if len(raw) > _MAX_HTTP_RESPONSE:
        raise BrokerError(f"response exceeds {_MAX_HTTP_RESPONSE} bytes")
    return {
        "status": status,
        "headers": response_headers,
        "body_b64": base64.b64encode(raw).decode("ascii"),
    }


def _dispatch(payload: dict, context: dict):
    operation = payload.get("op")
    slug = context["slug"]
    if operation == "kv.get":
        exists, value, expires = kv_store.get_value(slug, str(payload.get("key", "")))
        result = {"exists": exists, "expires_at": expires}
        result.update(_wire_value(value))
        return result
    if operation == "kv.set":
        ttl = payload.get("ttl")
        if "value_b64" in payload:
            import base64

            value = base64.b64decode(str(payload["value_b64"]).encode("ascii"))
        else:
            value = payload.get("value")
        return kv_store.set_value(slug, str(payload.get("key", "")), value, ttl)
    if operation == "kv.delete":
        return {"deleted": kv_store.delete_value(slug, str(payload.get("key", "")))}
    if operation == "kv.clear":
        return {"deleted": kv_store.clear_values(slug)}
    if operation == "kv.list":
        items = []
        for entry in kv_store.list_values(slug, str(payload.get("prefix", ""))):
            item = {"key": entry["key"], "expires_at": entry.get("expires_at")}
            item.update(_wire_value(entry.get("value")))
            items.append(item)
        return {"items": items}
    if operation == "kv.incr":
        return kv_store.incr_value(
            slug, str(payload.get("key", "")), payload.get("amount", 1)
        )
    if operation == "http.fetch":
        return _http_fetch(context, payload)
    if operation == "workflow.trigger":
        try:
            workflow_id = int(payload.get("workflow_id"))
        except (TypeError, ValueError):
            raise BrokerError("invalid workflow id")
        workflow_input = payload.get("payload", {})
        if workflow_input is None:
            workflow_input = {}
        if not isinstance(workflow_input, dict):
            raise BrokerError("workflow payload must be an object")
        if len(json.dumps(workflow_input).encode("utf-8")) > 256 * 1024:
            raise BrokerError("workflow payload is too large")
        from vlambda import workflows

        result = json.loads(workflows.trigger_workflow_for_slug(
            slug, workflow_id, json.dumps(workflow_input, ensure_ascii=False)
        ))
        if result.get("error"):
            raise BrokerError(str(result["error"]))
        return result
    raise BrokerError(f"unknown operation: {operation}")


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.connection.settimeout(10)
            line = self.rfile.readline(_MAX_LINE + 1)
            if not line:
                return
            if len(line) > _MAX_LINE:
                self._write({"ok": False, "error": "request too large"})
                return
            try:
                payload = json.loads(line.decode("utf-8", "replace"))
            except Exception:
                self._write({"ok": False, "error": "invalid json"})
                return
            if not isinstance(payload, dict):
                self._write({"ok": False, "error": "request must be an object"})
                return
            token = str(payload.pop("token", ""))
            try:
                context = _context(token, str(payload.get("op", "")))
            except BrokerError as error:
                self._write({"ok": False, "error": str(error)})
                return
            try:
                result = _dispatch(payload, context)
                self._write({"ok": True, "result": result})
            except Exception as error:
                message = f"{type(error).__name__}: {error}"
                _audit(context, str(payload.get("op", "?")), message)
                self._write({"ok": False, "error": message})
        except Exception:
            return

    def _write(self, payload: dict) -> None:
        try:
            self.wfile.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            self.wfile.flush()
        except OSError:
            pass


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        limit = max(1, int(os.environ.get("VLAMBDA_BROKER_MAX_CONNECTIONS", "64")))
        self._slots = threading.BoundedSemaphore(limit)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.close()
            except OSError:
                pass
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def shutdown_request(self, request):
        try:
            super().shutdown_request(request)
        finally:
            self._slots.release()


def start() -> bool:
    """Start the broker if it is not already running."""
    global _server, _thread, SOCKET_PATH
    if _server is not None:
        return True
    SOCKET_PATH = _socket_path()
    directory = os.path.dirname(SOCKET_PATH) or "."
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        server = _Server(SOCKET_PATH, _Handler)
        os.chmod(SOCKET_PATH, 0o600)
    except Exception as error:
        print(
            f"WARNING: capability broker disabled: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        _server = None
        return False
    _server = server
    os.environ["VLAMBDA_BROKER_SOCKET"] = SOCKET_PATH
    _thread = threading.Thread(target=server.serve_forever, name="vlambda-broker", daemon=True)
    _thread.start()
    return True


def stop() -> None:
    global _server, _thread
    server = _server
    _server = None
    _thread = None
    clear_tokens()
    if server is not None:
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
    try:
        if SOCKET_PATH:
            os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def running() -> bool:
    return _server is not None


def attach(meta_json: str, slug: str, request_id: str = "") -> tuple[str, str | None]:
    """Add a broker socket/token to a runner payload.

    Returns the new metadata and the issued token, or the original metadata and
    ``None`` when the broker is unavailable.
    """
    if _server is None or not slug:
        return meta_json, None
    try:
        meta = json.loads(meta_json or "{}")
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        meta = {}
    token = issue(slug, request_id)
    meta["broker_socket"] = SOCKET_PATH
    meta["broker_token"] = token
    return json.dumps(meta), token
