"""Local capability broker for sandboxed lambdas.

The broker is a Unix-socket JSON-RPC server owned by the vlambda process. Each
invocation gets a short-lived token that is scoped to one lambda. The runner
uses the broker for KV access (and, in 0.4.0, controlled egress) without ever
receiving a network socket.
"""

from __future__ import annotations

import json
import os
import secrets
import socketserver
import sys
import threading
import time

from vlambda import kv_store

_MAX_LINE = 1024 * 1024
_MAX_OPS = 200
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
    with _tokens_lock:
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


def _context(token: str) -> dict:
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
                context = _context(token)
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
