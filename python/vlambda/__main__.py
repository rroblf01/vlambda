"""CLI entry point: python -m vlambda."""

from __future__ import annotations

import argparse
import os
import signal
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="vlambda", description="vlambda server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VLAMBDA_PORT", "8080")))
    parser.add_argument("--sqlite_path", default=os.environ.get("VLAMBDA_DB", "./vlambda.db"))
    parser.add_argument("--num_coroutines", type=int, default=int(os.environ.get("VLAMBDA_COROUTINES", "10")))
    parser.add_argument("--max_body_size", type=int, default=int(os.environ.get("VLAMBDA_MAX_BODY", "1048576")))
    parser.add_argument("--rate_limit", type=int, default=int(os.environ.get("VLAMBDA_RATE_LIMIT", "100")))
    parser.add_argument("--invoke_timeout", type=int, default=int(os.environ.get("VLAMBDA_TIMEOUT", "30")))
    parser.add_argument("--max_memory_mb", type=int, default=int(os.environ.get("VLAMBDA_MAX_MEMORY_MB", "0")))
    parser.add_argument("--cors_origin", default=os.environ.get("VLAMBDA_CORS", "*"))
    parser.add_argument("--cert", default=os.environ.get("VLAMBDA_CERT", ""))
    parser.add_argument("--key", default=os.environ.get("VLAMBDA_KEY", ""))
    args = parser.parse_args()

    # Try to use jemalloc for better allocation performance
    try:
        import ctypes
        ctypes.CDLL("libjemalloc.so.2", mode=ctypes.RTLD_GLOBAL)
    except OSError:
        pass

    from vlambda.sandbox import execute_lambda

    def _validate_code(code):
        import ast
        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return f"syntax error: {e}"
        # Check for handler function
        has_handler = any(
            isinstance(node, ast.FunctionDef) and node.name == "handler"
            for node in ast.walk(tree)
        )
        if not has_handler:
            return "missing handler function"
        return ""  # valid
    from vlambda.auth import verify_token, create_token, hash_password, check_password
    import sqlite3
    from vlambda.request import Request
    from vlambda_native import start_server, db_init, register_pool, register_auth_verifier, register_auth_register, register_auth_login, configure, register_validate_code, register_ui_html, register_create_task, register_get_task, register_record_metric, register_query_metrics, register_run_project, register_create_project, register_delete_project, set_overloaded

    if not db_init(args.sqlite_path):
        print("Failed to initialize database", file=sys.stderr)
        sys.exit(1)

    from vlambda.dbpool import init as pool_init
    pool_init(args.sqlite_path)

    # GC tuning: disable during request handling, collect periodically
    import gc
    gc.set_threshold(0, 0, 0)  # disable automatic GC during requests

    metrics_enabled = os.environ.get("VLAMBDA_METRICS_ENABLED", "1") == "1"
    metrics_sample = int(os.environ.get("VLAMBDA_METRICS_SAMPLE", "1"))

    configure(
        args.max_body_size, args.rate_limit, args.invoke_timeout,
        args.max_memory_mb, args.cors_origin, args.cert, args.key,
        metrics_enabled, metrics_sample,
    )

    metrics_db = os.environ.get("VLAMBDA_METRICS_DB", args.sqlite_path.replace(".db", "_metrics.db"))
    metrics_retention = int(os.environ.get("VLAMBDA_METRICS_RETENTION", "7"))

    from vlambda.metrics import init as metrics_init
    metrics_init(metrics_db, metrics_retention)

    from vlambda.pool import Pool
    pool = Pool(args.num_coroutines)

    from vlambda.tasks import create_task, get_task

    def _create_task(code, method, path, query, body):
        import json
        task_id = create_task(code, method, path, query, body, pool)
        return json.dumps({"task_id": task_id, "status": "pending"})

    def _get_task(task_id):
        import json
        task = get_task(task_id)
        if task is None:
            return json.dumps({"error": "task not found"})
        result = {"task_id": task_id, "status": task["status"]}
        if task["status"] == "completed":
            r = task["result"]
            result["result"] = json.loads(r) if isinstance(r, str) else r
        elif task["status"] == "failed":
            result["error"] = task["error"]
        return json.dumps(result)

    def _pool_execute(code, method, path, query, body):
        method = method or "GET"
        path = path or "/"
        query = query or ""
        body = body or b""
        # Response cache for GET requests, storing the final serialized payload.
        cache_key = None
        if method == "GET":
            cache_key = f"{code}:{path}:{query}"
            from vlambda.cache import get_response
            cached = get_response(cache_key)
            if cached is not None:
                return cached
        try:
            import orjson
            _dumps = orjson.dumps
        except ImportError:
            import json
            _dumps = lambda x: json.dumps(x).encode()
        result = pool.execute(code, method, path, query, body)
        if isinstance(result, str):
            payload = result
        elif isinstance(result, bytes):
            payload = result.decode()
        else:
            dumped = _dumps(result)
            payload = dumped.decode() if isinstance(dumped, bytes) else dumped
        if cache_key is not None:
            from vlambda.cache import set_response
            set_response(cache_key, payload)
        return payload

    register_pool(_pool_execute)

    def _auth_verify(token):
        data = verify_token(token)
        if data is None:
            return -1
        try:
            return int(data.get("sub", -1))
        except (ValueError, TypeError):
            return -1

    register_auth_verifier(_auth_verify)

    # Auth: register user
    def _auth_register(username, password):
        from vlambda.dbpool import get as db_get
        try:
            with db_get(args.sqlite_path) as conn:
                conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                             (username, hash_password(password)))
                conn.commit()
                uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
                return uid
        except Exception as e:
            if "UNIQUE" in str(e) or "IntegrityError" in str(type(e).__name__):
                return -1
            return -1

    # Auth: login
    def _auth_login(username, password):
        from vlambda.dbpool import get as db_get
        with db_get(args.sqlite_path) as conn:
            row = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return ""
            uid, stored = row
            if not check_password(password, stored):
                return ""
            return create_token(uid, username)

    # Register V bridges
    from vlambda_native import register_auth_register, register_auth_login
    register_auth_register(_auth_register)
    register_auth_login(_auth_login)

    register_validate_code(_validate_code)

    # Load UI HTML
    from vlambda.assets import UI_HTML as _ui_html

    def _get_ui_html():
        return _ui_html

    register_ui_html(_get_ui_html)

    from vlambda.assets import FAVICON as _favicon

    def _get_favicon():
        return _favicon

    from vlambda_native import register_favicon
    register_favicon(_get_favicon)

    register_create_task(_create_task)
    register_get_task(_get_task)

    import atexit
    from vlambda.metrics import record as _metrics_record, query as _metrics_query
    import json as _json

    def _record_metric(slug, method, path, latency_ms, error):
        _metrics_record(slug, method, path, float(latency_ms), error)

    def _query_metrics(days):
        try:
            d = int(days)
        except (ValueError, TypeError):
            d = 7
        return _json.dumps(_metrics_query(d))

    register_record_metric(_record_metric)
    register_query_metrics(_query_metrics)

    from vlambda.project import create_project as _create_project, execute_project as _execute_project, delete_project as _delete_project_impl

    def _run_project(project_dir, entrypoint, request_dict):
        from vlambda.request import Request
        req = Request(
            method=request_dict.get("method", "GET"),
            path=request_dict.get("path", "/"),
            query_string=request_dict.get("query_string", ""),
            headers={},
            body=request_dict.get("body", b""),
        )
        try:
            result = _execute_project(project_dir, entrypoint, req)
            import json
            if isinstance(result, str):
                return result
            if isinstance(result, (dict, list)):
                return json.dumps(result)
            return str(result)
        except Exception as e:
            import json
            return json.dumps({"error": f"{type(e).__name__}: {e}"})

    def _delete_project(project_dir):
        _delete_project_impl(project_dir)
        return None

    register_run_project(_run_project)
    register_create_project(_create_project)
    register_delete_project(_delete_project)

    # Prewarm caches for existing lambdas/projects to reduce cold starts.
    try:
        from vlambda.dbpool import get as _db_get
        with _db_get(args.sqlite_path) as _conn:
            _rows = _conn.execute("SELECT slug, code, kind, entrypoint, project_dir FROM lambdas").fetchall()
        for _slug, _code, _kind, _entrypoint, _project_dir in _rows:
            try:
                vlambda_native.db_get_lambda_code(_slug)
            except Exception:
                pass
            if _kind == "project" and _project_dir:
                try:
                    from vlambda.project import _load_handler
                    _load_handler(_project_dir, _entrypoint or "main.py")
                except Exception:
                    pass
            elif _code:
                try:
                    from vlambda.cache import prewarm as _prewarm_code
                    _prewarm_code(_code)
                except Exception:
                    pass
    except Exception as e:
        print(f"Prewarm failed: {e}", file=sys.stderr)

    import vlambda.tasks as _tasks
    atexit.register(lambda: _tasks._get_executor().shutdown(wait=False) if _tasks._EXECUTOR is not None else None)
    atexit.register(pool.stop)

    # Run server in a thread so signals can be handled
    import threading
    _server_done = threading.Event()

    def _run_server():
        rc = start_server(args.port, args.sqlite_path, args.num_coroutines)
        _server_done.set()

    _server_thread = threading.Thread(target=_run_server, daemon=True)
    _server_thread.start()

    # Memory monitoring thread (if max_memory_mb > 0)
    if args.max_memory_mb > 0:
        import threading, time, resource
        def _mem_monitor():
            limit_bytes = args.max_memory_mb * 1024 * 1024
            overloaded = False
            while True:
                time.sleep(5)
                try:
                    import gc
                    gc.collect()
                    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                    if usage > limit_bytes * 0.9 and not overloaded:
                        overloaded = True
                        set_overloaded(True)
                        print(f"WARNING: memory usage {usage/1024/1024:.1f}MB approaching limit {args.max_memory_mb}MB", file=sys.stderr)
                    elif usage < limit_bytes * 0.8 and overloaded:
                        overloaded = False
                        set_overloaded(False)
                except Exception:
                    pass
        t = threading.Thread(target=_mem_monitor, daemon=True)
        t.start()

    # Graceful shutdown
    def _shutdown(signum, frame):
        print("\nShutting down gracefully...", file=sys.stderr)
        pool.stop()
        from vlambda.dbpool import close_all
        close_all()
        gc.collect()
        _server_done.set()
        os._exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"Starting vlambda on port {args.port} (db={args.sqlite_path}, coroutines={args.num_coroutines})")
    print(f"  max_body={args.max_body_size}, rate_limit={args.rate_limit}/s, timeout={args.invoke_timeout}s, max_mem={args.max_memory_mb}MB")
    print("Press Ctrl+C to stop.")
    # Note: Ctrl+C may not work due to V's blocking listen_and_serve().
    # Use 'kill <pid>' or the /shutdown endpoint to stop.
    import time
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
