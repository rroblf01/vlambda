"""Request object passed to lambda handlers."""

from __future__ import annotations


class Request:
    __slots__ = ("method", "path", "query_string", "headers", "body")

    def __init__(self, method="GET", path="/", query_string="", headers=None, body=b""):
        self.method = method
        self.path = path
        self.query_string = query_string
        self.headers = headers or {}
        self.body = body
