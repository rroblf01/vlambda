"""Request/response contract for vlambda handlers."""

from __future__ import annotations

import json


class Response:
    """Explicit HTTP response returned by a lambda handler."""

    def __init__(self, body=b"", status: int = 200, headers=None, content_type: str | None = None, cookies=None):
        self.body = body
        self.status = int(status)
        self.headers: list[tuple[str, str]] = []
        self.cookies: list[str] = []
        if content_type:
            self.headers.append(("Content-Type", content_type))
        if headers:
            items = headers.items() if isinstance(headers, dict) else headers
            for key, value in items:
                self.headers.append((str(key), str(value)))
        if cookies:
            for cookie in cookies:
                self.cookies.append(str(cookie))

    def set_header(self, name: str, value: str):
        self.headers = [(k, v) for k, v in self.headers if k.lower() != name.lower()]
        self.headers.append((str(name), str(value)))
        return self

    def set_cookie(self, name: str, value: str, path: str = "/", **attrs):
        parts = [f"{name}={value}", f"Path={path}"]
        for key, val in attrs.items():
            if val is True:
                parts.append(str(key))
            elif val not in (None, False):
                parts.append(f"{key}={val}")
        self.cookies.append("; ".join(parts))
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

    headers = list(response.headers)
    for cookie in response.cookies:
        headers.append(("Set-Cookie", cookie))
    body = response.body
    if isinstance(body, (bytes, bytearray)):
        return {
            "status": response.status,
            "headers": [{"name": k, "value": v} for k, v in headers],
            "body": "",
            "body_b64": base64.b64encode(bytes(body)).decode("ascii"),
        }
    return {
        "status": response.status,
        "headers": [{"name": k, "value": v} for k, v in headers],
        "body": str(body),
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
