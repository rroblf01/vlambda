"""Request object passed to lambda handlers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass
class Request:
    method: str
    path: str
    query_string: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def query(self):
        return QueryParams(self.query_string)

    @property
    def json_body(self) -> Any:
        import json
        if not self.body:
            return None
        return json.loads(self.body)


class QueryParams:
    def __init__(self, qs: str):
        self._params = parse_qs(qs)

    def get(self, key: str, default: Any = None) -> Any:
        vals = self._params.get(key)
        if vals is None:
            return default
        return vals[0] if vals else default

    def getlist(self, key: str) -> list[str]:
        return self._params.get(key, [])
