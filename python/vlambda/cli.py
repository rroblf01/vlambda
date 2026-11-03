"""Small CLI for invoking and managing vlambda over HTTP."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _default_url() -> str:
    return os.environ.get("VLAMBDA_URL", "http://127.0.0.1:8080")


def _default_token() -> str:
    return os.environ.get("VLAMBDA_TOKEN", "")


def _request(url: str, token: str, method: str = "GET", body: bytes | None = None, headers=None):
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=hdrs, method=method)
    try:
        resp = urllib.request.urlopen(request, timeout=60)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def _print_response(status: int, headers, body: bytes, raw: bool = False):
    if raw:
        sys.stdout.buffer.write(body)
        return
    text = body.decode("utf-8", "replace")
    try:
        print(json.dumps(json.loads(text), indent=2, ensure_ascii=False))
    except Exception:
        print(text)
    if status >= 400:
        raise SystemExit(1)


def cmd_invoke(args):
    url = args.url.rstrip("/") + f"/f/{args.slug}"
    if args.query:
        url += "?" + args.query.lstrip("?")
    body = args.data.encode() if args.data is not None else None
    headers = {"Content-Type": args.content_type}
    for item in args.header or []:
        if ":" in item:
            key, value = item.split(":", 1)
            headers[key.strip()] = value.strip()
    status, rh, rb = _request(url, args.token, args.method, body, headers)
    _print_response(status, rh, rb, args.raw)


def cmd_deploy(args):
    code = open(args.file, "r", encoding="utf-8").read()
    if args.slug:
        url = args.url.rstrip("/") + f"/vlambda/{args.slug}?method={args.method}&name={urllib.parse.quote(args.name or args.slug)}"
        status, _, rb = _request(url, args.token, "PUT", code.encode(), {"Content-Type": "text/plain"})
    else:
        url = args.url.rstrip("/") + f"/vlambda?method={args.method}&name={urllib.parse.quote(args.name or '')}"
        status, _, rb = _request(url, args.token, "POST", code.encode(), {"Content-Type": "text/plain"})
    _print_response(status, {}, rb)


def cmd_logs(args):
    url = args.url.rstrip("/") + f"/logs/history?days={args.days}"
    if args.slug:
        url += "&slug=" + urllib.parse.quote(args.slug)
    status, _, rb = _request(url, args.token)
    _print_response(status, {}, rb)


def cmd_env(args):
    base = args.url.rstrip("/") + f"/vlambda/{args.slug}/env"
    if args.set or args.secret:
        payload = {"vars": {}, "secrets": {}}
        for item in args.set or []:
            key, value = item.split("=", 1)
            payload["vars"][key] = value
        for item in args.secret or []:
            key, value = item.split("=", 1)
            payload["secrets"][key] = value
        status, _, rb = _request(base, args.token, "PUT", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    else:
        status, _, rb = _request(base, args.token)
    _print_response(status, {}, rb)


def cmd_task(args):
    status, _, rb = _request(args.url.rstrip("/") + f"/task/{args.task_id}", args.token)
    _print_response(status, {}, rb)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=_default_url())
    common.add_argument("--token", default=_default_token())
    parser = argparse.ArgumentParser(prog="vlambda", description="vlambda CLI", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    invoke = sub.add_parser("invoke", help="invoke a lambda", parents=[common])
    invoke.add_argument("slug")
    invoke.add_argument("--method", default="GET")
    invoke.add_argument("--data", default=None)
    invoke.add_argument("--query", default="")
    invoke.add_argument("--header", action="append")
    invoke.add_argument("--content-type", default="application/json")
    invoke.add_argument("--raw", action="store_true")
    invoke.set_defaults(func=cmd_invoke)

    deploy = sub.add_parser("deploy", help="create or update a code lambda from a file", parents=[common])
    deploy.add_argument("file")
    deploy.add_argument("--name", default="")
    deploy.add_argument("--slug", default="")
    deploy.add_argument("--method", default="GET")
    deploy.set_defaults(func=cmd_deploy)

    logs = sub.add_parser("logs", help="read persisted logs", parents=[common])
    logs.add_argument("--days", type=int, default=1)
    logs.add_argument("--slug", default="")
    logs.set_defaults(func=cmd_logs)

    env = sub.add_parser("env", help="read or update lambda environment", parents=[common])
    env.add_argument("slug")
    env.add_argument("--set", action="append")
    env.add_argument("--secret", action="append")
    env.set_defaults(func=cmd_env)

    task = sub.add_parser("task", help="read an async task", parents=[common])
    task.add_argument("task_id")
    task.set_defaults(func=cmd_task)

    args = parser.parse_args(argv)
    if not args.token:
        print("vlambda: --token or VLAMBDA_TOKEN is required", file=sys.stderr)
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
