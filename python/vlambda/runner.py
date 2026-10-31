"""Isolated runner process for vlambda lambdas.

Reads one JSON request per line from stdin and writes one JSON response per
line to stdout.  Used by the optional process-based pool so a runaway lambda
can be killed without taking down the whole server.
"""

from __future__ import annotations

import base64
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from vlambda.cache import get as get_compiled
from vlambda.request import Request
from vlambda.sandbox import make_sandbox


def _execute(code: str, method: str, path: str, query: str, body: bytes):
    compiled, ns_template, handler = get_compiled(code)
    if handler is None:
        return {"error": "no handler function defined"}
    ns = dict(ns_template)
    req = Request(method=method, path=path, query_string=query, headers={}, body=body)
    ns["request"] = req
    return handler(req)


def main() -> None:
    # Keep stdout clean: only protocol JSON goes there.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            code = req.get("code", "")
            method = req.get("method", "GET")
            path = req.get("path", "/")
            query = req.get("query_string", "")
            body = base64.b64decode(req.get("body_b64", "")) if req.get("body_b64") else b""
            result = _execute(code, method, path, query, body)
            if isinstance(result, str):
                payload = result
            elif isinstance(result, bytes):
                payload = result.decode("utf-8", "replace")
            elif isinstance(result, (dict, list, tuple)):
                import json as _json

                payload = _json.dumps(result)
            else:
                payload = str(result)
            print(json.dumps({"ok": True, "payload": payload}), flush=True)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}), flush=True)


if __name__ == "__main__":
    main()
