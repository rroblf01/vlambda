"""CLI entry point: python -m vlambda."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="vlambda", description="vlambda server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("VLAMBDA_PORT", "8080")))
    parser.add_argument("--sqlite_path", default=os.environ.get("VLAMBDA_DB", "./vlambda.db"))
    parser.add_argument("--num_coroutines", type=int, default=int(os.environ.get("VLAMBDA_COROUTINES", "10")))
    args = parser.parse_args()

    from vlambda.sandbox import execute_lambda
    from vlambda.auth import verify_token, create_token, hash_password, check_password
    import sqlite3
    from vlambda.request import Request
    from vlambda_native import start_server, db_init, register_pool, register_auth_verifier, register_auth_register, register_auth_login

    if not db_init(args.sqlite_path):
        print("Failed to initialize database", file=sys.stderr)
        sys.exit(1)

    from vlambda.pool import Pool
    pool = Pool(args.num_coroutines)

    def _pool_execute(code, req_dict):
        import json
        result = pool.execute(
            code,
            req_dict.get("method", "GET"),
            req_dict.get("path", "/"),
            req_dict.get("query_string", ""),
            req_dict.get("body", b""),
        )
        return json.dumps(result) if not isinstance(result, str) else result

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
        import sqlite3, os
        db_path = args.sqlite_path
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            conn.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                         (username, hash_password(password)))
            conn.commit()
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
            return uid
        except sqlite3.IntegrityError:
            return -1
        finally:
            conn.close()

    # Auth: login
    def _auth_login(username, password):
        import sqlite3
        db_path = args.sqlite_path
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("SELECT id, password_hash FROM users WHERE username = ?", (username,)).fetchone()
            if row is None:
                return ""
            uid, stored = row
            if not check_password(password, stored):
                return ""
            return create_token(uid, username)
        finally:
            conn.close()

    # Register V bridges
    from vlambda_native import register_auth_register, register_auth_login
    register_auth_register(_auth_register)
    register_auth_login(_auth_login)

    import atexit
    atexit.register(pool.stop)

    print(f"Starting vlambda on port {args.port} (db={args.sqlite_path}, coroutines={args.num_coroutines})")
    rc = start_server(args.port, args.sqlite_path, args.num_coroutines)
    sys.exit(rc)


if __name__ == "__main__":
    main()
