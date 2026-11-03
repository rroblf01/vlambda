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
from vlambda.capabilities import CapabilityClient, clear_current, set_current
from vlambda.request import Request, set_log_sink
from vlambda.response import Response, serialize
from vlambda.sandbox import make_sandbox

_current_logs: list[dict] = []
_injected_env: set[str] = set()
_RESERVED_ENV_PREFIXES = ("VLAMBDA_", "VLRUN_", "LD_")
_RESERVED_ENV_NAMES = {"PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PYTHONINSPECT"}


def _is_reserved_env(name: str) -> bool:
    return name in _RESERVED_ENV_NAMES or name.startswith(_RESERVED_ENV_PREFIXES)


def _apply_env(env) -> None:
    """Replace the environment values injected for the previous invocation."""
    global _injected_env
    values = {}
    if isinstance(env, dict):
        for key, value in env.items():
            key = str(key)
            if not _is_reserved_env(key):
                values[key] = str(value)
    for key in _injected_env - set(values):
        os.environ.pop(key, None)
    os.environ.update(values)
    _injected_env = set(values)



def _log_sink(request_id: str, slug: str, level: str, message: str):
    _current_logs.append({
        "request_id": request_id,
        "slug": slug,
        "level": level,
        "message": str(message),
    })


set_log_sink(_log_sink)


def _execute(code: str, method: str, path: str, query: str, body: bytes, request_id: str = "", slug: str = "",
             headers=None, remote_ip: str = "", host: str = "", scheme: str = "http", env=None,
             kv_client=None):
    compiled, ns_template, handler = get_compiled(code)
    if handler is None:
        raise ValueError("no handler function defined")
    ns = dict(ns_template)
    req = Request(method=method, path=path, query_string=query, headers=headers or {}, body=body,
                  request_id=request_id, slug=slug, remote_ip=remote_ip, host=host, scheme=scheme, env=env,
                  kv_client=kv_client)
    ns["request"] = req
    return handler(req)


def _execute_project(project_dir: str, entrypoint: str, method: str, path: str, query: str, body: bytes,
                     request_id: str = "", slug: str = "", headers=None, remote_ip: str = "",
                     host: str = "", scheme: str = "http", env=None, kv_client=None):
    from vlambda.project import execute_project
    req = Request(method=method, path=path, query_string=query, headers=headers or {}, body=body,
                  request_id=request_id, slug=slug, remote_ip=remote_ip, host=host, scheme=scheme, env=env,
                  kv_client=kv_client)
    return execute_project(project_dir, entrypoint, req)


def _sandbox_args(argv: list[str]) -> tuple[bool, list[str], list[str], int]:
    enabled = False
    read_paths: list[str] = []
    rw_paths: list[str] = []
    cpu_sec = 5
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--sandbox" and index + 1 < len(argv):
            enabled = argv[index + 1] == "landlock"
            index += 2
        elif arg == "--allow-read" and index + 1 < len(argv):
            read_paths.append(argv[index + 1])
            index += 2
        elif arg == "--allow-rw" and index + 1 < len(argv):
            rw_paths.append(argv[index + 1])
            index += 2
        elif arg == "--cpu" and index + 1 < len(argv):
            try:
                cpu_sec = max(1, int(argv[index + 1]))
            except ValueError:
                pass
            index += 2
        else:
            index += 1
    return enabled, read_paths, rw_paths, cpu_sec


def handle_payload(req: dict) -> dict:
    """Execute one request payload and return the protocol envelope."""
    _current_logs.clear()
    client = None
    try:
        code = req.get("code", "")
        method = req.get("method", "GET")
        path = req.get("path", "/")
        query = req.get("query_string", "")
        body = base64.b64decode(req.get("body_b64", "")) if req.get("body_b64") else b""
        try:
            meta = json.loads(req.get("request_meta", "{}") or "{}")
        except Exception:
            meta = {}
        headers = meta.get("headers", {}) if isinstance(meta, dict) else {}
        remote_ip = meta.get("remote_ip", "") if isinstance(meta, dict) else ""
        host = meta.get("host", "") if isinstance(meta, dict) else ""
        scheme = meta.get("scheme", "http") if isinstance(meta, dict) else "http"
        env = meta.get("env", {}) if isinstance(meta, dict) else {}
        broker_socket = meta.get("broker_socket", "") if isinstance(meta, dict) else ""
        broker_token = meta.get("broker_token", "") if isinstance(meta, dict) else ""
        if broker_socket and broker_token:
            client = CapabilityClient(str(broker_socket), str(broker_token))
        set_current(client)
        _apply_env(env)
        vendor_dir = req.get("vendor_dir", "")
        if vendor_dir and os.path.isdir(vendor_dir) and vendor_dir not in sys.path:
            sys.path.insert(0, vendor_dir)
        if req.get("kind") == "project":
            result = _execute_project(
                req.get("project_dir", ""), req.get("entrypoint", "main.py"),
                method, path, query, body, req.get("request_id", ""), req.get("slug", ""),
                headers=headers, remote_ip=remote_ip, host=host, scheme=scheme, env=env,
                kv_client=client,
            )
        else:
            result = _execute(code, method, path, query, body, req.get("request_id", ""), req.get("slug", ""),
                              headers=headers, remote_ip=remote_ip, host=host, scheme=scheme, env=env,
                              kv_client=client)
        return {"ok": True, "response": serialize(result), "logs": list(_current_logs)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "logs": list(_current_logs)}
    finally:
        clear_current()


def main() -> None:
    sandboxed, read_paths, rw_paths, cpu_sec = _sandbox_args(sys.argv[1:])
    if sandboxed:
        try:
            from vlambda.sandbox_os import apply_sandbox
            apply_sandbox(read_paths, rw_paths, cpu_sec)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"sandbox: {type(e).__name__}: {e}"}), flush=True)
            return
    # Keep stdout clean: only protocol JSON goes there.
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}", "logs": []}), flush=True)
            continue
        print(json.dumps(handle_payload(req)), flush=True)


if __name__ == "__main__":
    main()
