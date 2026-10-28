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
    from vlambda_native import start_server, db_init, register_pool, register_auth_verifier, register_auth_register, register_auth_login, configure, register_validate_code

    if not db_init(args.sqlite_path):
        print("Failed to initialize database", file=sys.stderr)
        sys.exit(1)

    from vlambda.dbpool import init as pool_init
    pool_init(args.sqlite_path)

    # GC tuning: disable during request handling, collect periodically
    import gc
    gc.set_threshold(0, 0, 0)  # disable automatic GC during requests

    configure(
        args.max_body_size, args.rate_limit, args.invoke_timeout,
        args.max_memory_mb, args.cors_origin, args.cert, args.key,
    )

    from vlambda.pool import Pool
    pool = Pool(args.num_coroutines)

    def _pool_execute(code, req_dict):
        try:
            import orjson
            _dumps = orjson.dumps
        except ImportError:
            import json
            _dumps = lambda x: json.dumps(x).encode()
        result = pool.execute(
            code,
            req_dict.get("method", "GET"),
            req_dict.get("path", "/"),
            req_dict.get("query_string", ""),
            req_dict.get("body", b""),
        )
        if isinstance(result, str):
            return result
        if isinstance(result, bytes):
            return result.decode()
        dumped = _dumps(result)
        return dumped.decode() if isinstance(dumped, bytes) else dumped

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

    import atexit
    atexit.register(pool.stop)

    # Memory monitoring thread (if max_memory_mb > 0)
    if args.max_memory_mb > 0:
        import threading, time, resource
        def _mem_monitor():
            limit_bytes = args.max_memory_mb * 1024 * 1024
            while True:
                time.sleep(5)
                try:
                    import gc
                    gc.collect()
                    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
                    if usage > limit_bytes * 0.9:
                        print(f"WARNING: memory usage {usage/1024/1024:.1f}MB approaching limit {args.max_memory_mb}MB", file=sys.stderr)
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
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"Starting vlambda on port {args.port} (db={args.sqlite_path}, coroutines={args.num_coroutines})")
    print(f"  max_body={args.max_body_size}, rate_limit={args.rate_limit}/s, timeout={args.invoke_timeout}s, max_mem={args.max_memory_mb}MB")
    rc = start_server(args.port, args.sqlite_path, args.num_coroutines)
    sys.exit(rc)


if __name__ == "__main__":
    main()
