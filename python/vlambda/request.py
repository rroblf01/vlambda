"""Request object passed to lambda handlers."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from vlambda.capabilities import fetch as _fetch_proxy
from vlambda.capabilities import kv as _kv_proxy

_log_sink = None


def set_log_sink(sink):
    """Route Request.log() to an alternate sink (used by sandboxed runners)."""
    global _log_sink
    _log_sink = sink


def clear_log_sink():
    global _log_sink
    _log_sink = None


class Request:
    __slots__ = (
        "method", "path", "query_string", "query", "headers", "cookies",
        "body", "request_id", "slug", "remote_ip", "host", "scheme", "env", "kv", "fetch",
        "trigger_workflow", "trigger_client",
    )

    def __init__(self, method="GET", path="/", query_string="", headers=None, body=b"",
                 request_id="", slug="", remote_ip="", host="", scheme="http", env=None,
                 kv_client=None, fetch_client=None, trigger_client=None):
        self.method = method
        self.path = path
        self.query_string = query_string
        self.query = {k: v[0] for k, v in parse_qs(query_string).items()}
        self.headers = dict(headers or {})
        self.body = body
        self.request_id = request_id
        self.slug = slug
        self.remote_ip = remote_ip
        self.host = host
        self.scheme = scheme
        self.env = dict(env or {})
        self.kv = kv_client if kv_client is not None else _kv_proxy
        self.fetch = fetch_client.fetch if fetch_client is not None else _fetch_proxy
        self.trigger_client = trigger_client if trigger_client is not None else (kv_client if kv_client is not None else None)
        self.trigger_workflow = self._trigger_workflow
        cookie_header = self.header("cookie")
        self.cookies = {}
        for part in cookie_header.split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                self.cookies[key.strip()] = value.strip()

    def header(self, name, default=""):
        wanted = name.lower()
        for key, value in self.headers.items():
            if key.lower() == wanted:
                return value
        return default

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body)

    def form(self):
        return {k: v[0] for k, v in parse_qs(self.body.decode("utf-8", "replace")).items()}

    def _trigger_workflow(self, workflow_id, payload=None):
        client = self.trigger_client
        if client is None:
            try:
                client = current()
            except Exception:
                client = None
        if client is not None:
            return client.trigger_workflow(workflow_id, payload)
        from vlambda.workflows import trigger_workflow_for_slug

        if not self.slug:
            raise CapabilityUnavailable("capability broker is not available in this invocation")
        import json as _json

        result = trigger_workflow_for_slug(self.slug, workflow_id, _json.dumps(payload or {}))
        return _json.loads(result)

    def log(self, message, level="info"):
        """Persist a log line for this invocation."""
        if _log_sink is not None:
            _log_sink(self.request_id, self.slug, level, message)
            return None
        from vlambda.metrics import record_log
        record_log(self.request_id, self.slug, level, message)
        return None
