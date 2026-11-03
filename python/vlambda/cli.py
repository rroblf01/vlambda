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


def _env_pairs(items, flag: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"vlambda: {flag} expects KEY=value, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise SystemExit(f"vlambda: {flag} has an empty key in {item!r}")
        pairs[key] = value
    return pairs


def cmd_deploy(args):
    with open(args.file, "r", encoding="utf-8") as handle:
        code = handle.read()
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


def _env_items(base: str, token: str):
    status, _, body = _request(base, token)
    if status >= 400:
        _print_response(status, {}, body)
        raise SystemExit(1)
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        raise SystemExit("vlambda: could not read the current environment")
    if isinstance(data, dict) and data.get("error"):
        print(f"vlambda: {data['error']}", file=sys.stderr)
        raise SystemExit(1)
    return data.get("env", []) if isinstance(data, dict) else []


def cmd_env(args):
    base = args.url.rstrip("/") + f"/vlambda/{args.slug}/env"
    if args.set or args.secret:
        payload = {}
        items = _env_items(base, args.token)
        if args.set:
            merged = {item["key"]: item["value"] for item in items if not item.get("secret")}
            merged.update(_env_pairs(args.set, "--set"))
            payload["vars"] = merged
        if args.secret:
            merged = {item["key"]: "********" for item in items if item.get("secret")}
            merged.update(_env_pairs(args.secret, "--secret"))
            payload["secrets"] = merged
        status, _, rb = _request(base, args.token, "PUT", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    else:
        status, _, rb = _request(base, args.token)
    _print_response(status, {}, rb)


def cmd_kv(args):
    base = args.url.rstrip("/") + f"/vlambda/{args.slug}/kv"
    if args.clear:
        status, _, rb = _request(base, args.token, "DELETE")
    elif args.delete:
        status, _, rb = _request(base + "/" + urllib.parse.quote(args.delete, safe=""), args.token, "DELETE")
    elif args.get:
        status, _, rb = _request(base + "/" + urllib.parse.quote(args.get, safe=""), args.token)
    elif args.set:
        if "=" not in args.set:
            raise SystemExit("vlambda: --set expects KEY=value")
        key, raw = args.set.split("=", 1)
        try:
            value = json.loads(raw)
        except Exception:
            value = raw
        payload = {"value": value}
        if args.ttl is not None:
            payload["ttl"] = args.ttl
        status, _, rb = _request(base + "/" + urllib.parse.quote(key.strip(), safe=""), args.token,
                                 "PUT", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    else:
        url = base
        if args.prefix:
            url += "?prefix=" + urllib.parse.quote(args.prefix, safe="")
        status, _, rb = _request(url, args.token)
    _print_response(status, {}, rb)


def cmd_egress(args):
    base = args.url.rstrip("/") + f"/vlambda/{args.slug}/egress"
    if args.clear:
        hosts = []
    else:
        hosts = args.set
    if hosts is not None:
        status, _, rb = _request(base, args.token, "PUT",
                                 json.dumps({"hosts": hosts}).encode(),
                                 {"Content-Type": "application/json"})
    else:
        status, _, rb = _request(base, args.token)
    _print_response(status, {}, rb)


def cmd_schedule(args):
    if args.action in ("delete", "run", "runs") and not args.schedule_id:
        raise SystemExit(f"vlambda: schedule {args.action} requires an id")
    base = args.url.rstrip("/") + f"/vlambda/{args.slug}/schedules"
    if args.action == "list":
        status, _, rb = _request(base, args.token)
    elif args.action == "create":
        if not args.name or not args.cron:
            raise SystemExit("vlambda: schedule create requires --name and --cron")
        payload = {
            "name": args.name,
            "cron": args.cron,
            "timezone": args.timezone,
            "method": args.method,
            "path": args.path,
            "payload": args.payload,
            "enabled": not args.disabled,
        }
        status, _, rb = _request(base, args.token, "POST", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    elif args.action == "delete":
        status, _, rb = _request(base + f"/{args.schedule_id}", args.token, "DELETE")
    elif args.action == "run":
        status, _, rb = _request(base + f"/{args.schedule_id}/run", args.token, "POST")
    else:
        status, _, rb = _request(base + f"/{args.schedule_id}/runs", args.token)
    _print_response(status, {}, rb)


def cmd_openapi(args):
    status, _, body = _request(args.url.rstrip("/") + "/openapi.json", args.token)
    if status >= 400:
        _print_response(status, {}, body)
        raise SystemExit(1)
    if args.out:
        with open(args.out, "wb") as handle:
            handle.write(body)
        print(args.out)
    else:
        _print_response(status, {}, body)


def cmd_task(args):
    status, _, rb = _request(args.url.rstrip("/") + f"/task/{args.task_id}", args.token)
    _print_response(status, {}, rb)


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--url", default=argparse.SUPPRESS)
    common.add_argument("--token", default=argparse.SUPPRESS)
    parser = argparse.ArgumentParser(prog="vlambda", description="vlambda CLI")
    parser.add_argument("--url", default=_default_url())
    parser.add_argument("--token", default=_default_token())
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

    kv = sub.add_parser("kv", help="read or write per-lambda KV", parents=[common])
    kv.add_argument("slug")
    kv.add_argument("--prefix", default="")
    kv.add_argument("--get", default="")
    kv.add_argument("--set", default="")
    kv.add_argument("--delete", default="")
    kv.add_argument("--clear", action="store_true")
    kv.add_argument("--ttl", type=float, default=None)
    kv.set_defaults(func=cmd_kv)

    egress = sub.add_parser("egress", help="read or replace the egress allow-list", parents=[common])
    egress.add_argument("slug")
    egress.add_argument("--set", action="append", help="allowed host (repeatable)")
    egress.add_argument("--clear", action="store_true", help="remove every allowed host")
    egress.set_defaults(func=cmd_egress)

    schedule = sub.add_parser("schedule", help="manage cron schedules", parents=[common])
    schedule.add_argument("action", choices=["list", "create", "delete", "run", "runs"])
    schedule.add_argument("slug")
    schedule.add_argument("schedule_id", nargs="?")
    schedule.add_argument("--name", default="")
    schedule.add_argument("--cron", default="")
    schedule.add_argument("--timezone", default="UTC")
    schedule.add_argument("--method", default="POST")
    schedule.add_argument("--path", default="/schedule")
    schedule.add_argument("--payload", default="")
    schedule.add_argument("--disabled", action="store_true")
    schedule.set_defaults(func=cmd_schedule)

    openapi = sub.add_parser("openapi", help="export the OpenAPI document", parents=[common])
    openapi.add_argument("--out", default="")
    openapi.set_defaults(func=cmd_openapi)

    args = parser.parse_args(argv)
    if not args.token:
        print("vlambda: --token or VLAMBDA_TOKEN is required", file=sys.stderr)
        return 2
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
