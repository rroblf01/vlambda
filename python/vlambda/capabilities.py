"""Client helpers for the sandbox capability broker.

The runner receives a per-invocation Unix socket path and token. Code lambdas
use ``request.kv``; projects can use ``from vlambda import kv``.
"""

from __future__ import annotations

import json
import socket
import threading

_MAX_RESPONSE = 4 * 1024 * 1024


class CapabilityError(RuntimeError):
    pass


class EgressResponse:
    def __init__(self, status: int, headers: dict, body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.body)

    def __repr__(self):
        return f"EgressResponse(status={self.status}, bytes={len(self.body)})"


class CapabilityUnavailable(CapabilityError):
    pass


class CapabilityClient:
    def __init__(self, socket_path: str, token: str):
        self.socket_path = socket_path
        self.token = token

    def _call(self, operation: str, **fields):
        if not self.socket_path or not self.token:
            raise CapabilityUnavailable("capability broker is not configured")
        payload = {"op": operation, "token": self.token}
        payload.update(fields)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(10)
                connection.connect(self.socket_path)
                connection.sendall(data + b"\n")
                chunks = []
                size = 0
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _MAX_RESPONSE:
                        raise CapabilityError("capability response too large")
                    if chunk.endswith(b"\n"):
                        break
        except CapabilityError:
            raise
        except OSError as error:
            raise CapabilityUnavailable(f"capability broker unavailable: {error}") from error
        if not chunks:
            raise CapabilityUnavailable("capability broker closed the connection")
        try:
            response = json.loads(b"".join(chunks).decode("utf-8", "replace").splitlines()[-1])
        except Exception as error:
            raise CapabilityError(f"invalid capability response: {error}") from error
        if not response.get("ok"):
            raise CapabilityError(str(response.get("error", "capability call failed")))
        return response.get("result")

    def get(self, key: str, default=None):
        result = self._call("kv.get", key=key)
        if not result.get("exists"):
            return default
        if result.get("binary"):
            import base64

            return base64.b64decode(str(result.get("value_b64", "")).encode("ascii"))
        return result.get("value")

    def set(self, key: str, value, ttl: float | None = None):
        fields = {"key": key}
        if isinstance(value, (bytes, bytearray)):
            import base64

            fields["value_b64"] = base64.b64encode(bytes(value)).decode("ascii")
        else:
            fields["value"] = value
        if ttl is not None:
            fields["ttl"] = ttl
        self._call("kv.set", **fields)
        return value

    def delete(self, key: str) -> bool:
        return bool(self._call("kv.delete", key=key).get("deleted"))

    def clear(self) -> int:
        return int(self._call("kv.clear").get("deleted", 0))

    def list(self, prefix: str = "") -> dict:
        result = self._call("kv.list", prefix=prefix)
        values = {}
        for item in result.get("items", []):
            if item.get("binary"):
                import base64

                values[item["key"]] = base64.b64decode(str(item.get("value_b64", "")).encode("ascii"))
            else:
                values[item["key"]] = item.get("value")
        return values

    def incr(self, key: str, amount: int | float = 1):
        return self._call("kv.incr", key=key, amount=amount).get("value")

    def fetch(self, url: str, method: str = "GET", headers=None, data=None,
              json_payload=None, timeout: float | None = None) -> EgressResponse:
        fields = {"url": url, "method": method}
        if headers:
            fields["headers"] = headers
        if data is not None:
            if isinstance(data, (bytes, bytearray)):
                import base64

                fields["data_b64"] = base64.b64encode(bytes(data)).decode("ascii")
            else:
                fields["body"] = str(data)
        if json_payload is not None:
            fields["json"] = json_payload
        if timeout is not None:
            fields["timeout"] = timeout
        result = self._call("http.fetch", **fields)
        import base64

        return EgressResponse(
            int(result.get("status", 0)),
            dict(result.get("headers") or {}),
            base64.b64decode(str(result.get("body_b64", "")).encode("ascii")),
        )


_local = threading.local()


def set_current(client: CapabilityClient | None) -> None:
    _local.client = client


def clear_current() -> None:
    _local.client = None


def current() -> CapabilityClient:
    client = getattr(_local, "client", None)
    if client is None:
        raise CapabilityUnavailable("capability broker is not available in this invocation")
    return client


class _KVProxy:
    """Project-facing proxy that resolves the current invocation context."""

    def get(self, key, default=None):
        return current().get(key, default)

    def set(self, key, value, ttl=None):
        return current().set(key, value, ttl=ttl)

    def delete(self, key):
        return current().delete(key)

    def clear(self):
        return current().clear()

    def list(self, prefix=""):
        return current().list(prefix)

    def incr(self, key, amount=1):
        return current().incr(key, amount)


class _FetchProxy:
    """Project-facing proxy for allow-listed egress."""

    def __call__(self, url, method="GET", headers=None, data=None, json=None, timeout=None):
        return current().fetch(
            url, method=method, headers=headers, data=data,
            json_payload=json, timeout=timeout,
        )


kv = _KVProxy()
fetch = _FetchProxy()
