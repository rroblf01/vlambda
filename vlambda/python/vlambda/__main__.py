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
    from vlambda.request import Request
    from vlambda_native import start_server, db_init, register_executor

    if not db_init(args.sqlite_path):
        print("Failed to initialize database", file=sys.stderr)
        sys.exit(1)

    def _executor(code, req_dict):
        import json
        req = Request(
            method=req_dict.get("method", "GET"),
            path=req_dict.get("path", "/"),
            query_string=req_dict.get("query_string", ""),
            headers=req_dict.get("headers", {}),
            body=req_dict.get("body", b""),
        )
        result = execute_lambda(code, req)
        return json.dumps(result)

    register_executor(_executor)

    print(f"Starting vlambda on port {args.port} (db={args.sqlite_path}, coroutines={args.num_coroutines})")
    rc = start_server(args.port, args.sqlite_path, args.num_coroutines)
    sys.exit(rc)


if __name__ == "__main__":
    main()
