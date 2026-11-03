"""Request object passed to lambda handlers."""

from __future__ import annotations

_log_sink = None


def set_log_sink(sink):
    """Route Request.log() to an alternate sink (used by sandboxed runners)."""
    global _log_sink
    _log_sink = sink


def clear_log_sink():
    global _log_sink
    _log_sink = None


class Request:
    __slots__ = ("method", "path", "query_string", "headers", "body", "request_id", "slug")

    def __init__(self, method="GET", path="/", query_string="", headers=None, body=b"",
                 request_id="", slug=""):
        self.method = method
        self.path = path
        self.query_string = query_string
        self.headers = headers or {}
        self.body = body
        self.request_id = request_id
        self.slug = slug

    def log(self, message, level="info"):
        """Persist a log line for this invocation."""
        if _log_sink is not None:
            _log_sink(self.request_id, self.slug, level, message)
            return None
        from vlambda.metrics import record_log
        record_log(self.request_id, self.slug, level, message)
        return None
