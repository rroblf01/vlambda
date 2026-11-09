"""Request/response contract for vlambda handlers."""

from __future__ import annotations

import json
import os
import re

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_HOP_BY_HOP = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

_MAX_RESPONSE_BYTES = int(os.environ.get("VLAMBDA_MAX_RESPONSE", str(10 * 1024 * 1024)))
_MAX_HEADERS = 100


def _check_header(name, value) -> tuple[str, str]:
    name = str(name)
    value = str(value)
    if not _HEADER_NAME.match(name):
        raise ValueError(f"invalid header name: {name!r}")
    if "\r" in value or "\n" in value:
        raise ValueError(f"invalid header value for {name!r}")
    return name, value


class Response:
    """Explicit HTTP response returned by a lambda handler."""

    def __init__(self, body=b"", status: int = 200, headers=None, content_type: str | None = None, cookies=None):
        self.body = body
        self.status = int(status)
        if not 100 <= self.status <= 599:
            raise ValueError(f"invalid HTTP status: {self.status}")
        self.headers: list[tuple[str, str]] = []
        self.cookies: list[str] = []
        if content_type:
            self.set_header("Content-Type", content_type)
        elif isinstance(body, (bytes, bytearray)):
            self.set_header("Content-Type", "application/octet-stream")
        if headers:
            items = headers.items() if isinstance(headers, dict) else headers
            for key, value in items:
                self.set_header(str(key), str(value))
        if cookies:
            for cookie in cookies:
                self._append_cookie(str(cookie))

    def set_header(self, name: str, value: str):
        name, value = _check_header(name, value)
        if name.lower() in _HOP_BY_HOP:
            return self
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()]
        self.headers.append((name, value))
        return self

    def _append_cookie(self, cookie: str):
        if "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid cookie value")
        self.cookies.append(cookie)

    def set_cookie(self, name: str, value: str, path: str = "/", **attrs):
        parts = [f"{name}={value}", f"Path={path}"]
        for key, val in attrs.items():
            if val is True:
                parts.append(str(key))
            elif val not in (None, False):
                parts.append(f"{key}={val}")
        self._append_cookie("; ".join(parts))
        return self

    @classmethod
    def json(cls, data, status: int = 200, headers=None, **kwargs):
        kwargs.setdefault("default", _json_default)
        return cls(json.dumps(data, **kwargs), status=status, headers=headers, content_type="application/json")

    @classmethod
    def text(cls, data, status: int = 200, headers=None):
        return cls(str(data), status=status, headers=headers, content_type="text/plain; charset=utf-8")

    @classmethod
    def html(cls, data, status: int = 200, headers=None):
        return cls(str(data), status=status, headers=headers, content_type="text/html; charset=utf-8")

    @classmethod
    def redirect(cls, location: str, status: int = 302):
        return cls(b"", status=status, headers={"Location": location})

    @classmethod
    def no_content(cls, status: int = 204):
        return cls(b"", status=status)


def serialize(result):
    """Convert a handler return value into the runner protocol response."""
    import base64

    if isinstance(result, Response):
        response = result
    elif isinstance(result, (dict, list, tuple)):
        response = Response.json(result)
    elif isinstance(result, str):
        response = Response(result, content_type=_guess_type(result))
    elif isinstance(result, (bytes, bytearray)):
        response = Response(bytes(result), content_type="application/octet-stream")
    else:
        response = Response(str(result), content_type="text/plain; charset=utf-8")

    headers = []
    for name, value in response.headers:
        name, value = _check_header(name, value)
        if name.lower() in _HOP_BY_HOP:
            continue
        headers.append((name, value))
    for cookie in response.cookies:
        if "\r" in cookie or "\n" in cookie:
            raise ValueError("invalid cookie value")
        headers.append(("Set-Cookie", cookie))
    if len(headers) > _MAX_HEADERS:
        raise ValueError(f"too many response headers (max {_MAX_HEADERS})")
    body = response.body
    if isinstance(body, (bytes, bytearray)):
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ValueError(f"response exceeds {_MAX_RESPONSE_BYTES} bytes")
        return {
            "status": response.status,
            "headers": [{"name": k, "value": v} for k, v in headers],
            "body": "",
            "body_b64": base64.b64encode(bytes(body)).decode("ascii"),
        }
    text = str(body)
    if len(text.encode("utf-8", "replace")) > _MAX_RESPONSE_BYTES:
        raise ValueError(f"response exceeds {_MAX_RESPONSE_BYTES} bytes")
    return {
        "status": response.status,
        "headers": [{"name": k, "value": v} for k, v in headers],
        "body": text,
        "body_b64": "",
    }


def _json_default(value):
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, set):
        return list(value)
    return str(value)


def _guess_type(text: str) -> str:
    stripped = text.lstrip().lower()
    if stripped.startswith("<!doctype html") or stripped.startswith("<html"):
        return "text/html; charset=utf-8"
    return "application/json"
