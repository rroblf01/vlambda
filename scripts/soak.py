#!/usr/bin/env python3
"""Run a sustained load against a deployed vlambda lambda.

This is a manual soak tool, not part of CI. It reports throughput, errors,
p95 latency and, when --pid is given, resident memory growth.

Example:
    python scripts/soak.py --url http://127.0.0.1:8080 \
        --token "$VLAMBDA_TOKEN" --slug <slug> --duration 3600 \
        --concurrency 8 --pid "$(pgrep -f 'python -m vlambda' | head -1)"
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request


def _request(url: str, token: str):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = urllib.request.Request(url, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response.read()
            return response.status, time.perf_counter() - started, ""
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.perf_counter() - started, str(exc.code)
    except Exception as exc:
        return 0, time.perf_counter() - started, type(exc).__name__


def _rss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="soak a vlambda lambda")
    parser.add_argument("--url", default=os.environ.get("VLAMBDA_URL", "http://127.0.0.1:8080"))
    parser.add_argument("--token", default=os.environ.get("VLAMBDA_TOKEN", ""))
    parser.add_argument("--slug", required=True)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--max-errors", type=int, default=0)
    parser.add_argument("--pid", type=int, default=0, help="server PID for RSS tracking")
    parser.add_argument("--max-rss-growth-mb", type=float, default=0.0)
    args = parser.parse_args()

    url = args.url.rstrip("/") + f"/f/{args.slug}"
    deadline = time.monotonic() + max(1.0, args.duration)
    lock = threading.Lock()
    latencies: list[float] = []
    errors: list[str] = []
    count = 0

    def worker():
        nonlocal count
        while time.monotonic() < deadline:
            status, latency, error = _request(url, args.token)
            with lock:
                latencies.append(latency)
                count += 1
                if status < 200 or status >= 300 or error:
                    errors.append(error or str(status))

    rss_start = _rss_kb(args.pid) if args.pid else None
    threads = [threading.Thread(target=worker, daemon=True) for _ in range(max(1, args.concurrency))]
    started = time.monotonic()
    for thread in threads:
        thread.start()
    while any(thread.is_alive() for thread in threads):
        time.sleep(0.2)
    elapsed = max(0.001, time.monotonic() - started)
    rss_end = _rss_kb(args.pid) if args.pid else None
    rss_growth = ((rss_end - rss_start) / 1024.0) if rss_start and rss_end else None
    p95 = statistics.quantiles(latencies, n=20)[-1] * 1000 if len(latencies) > 1 else 0.0

    report = {
        "requests": count,
        "errors": len(errors),
        "error_sample": errors[:10],
        "rps": round(count / elapsed, 2),
        "p95_ms": round(p95, 2),
        "rss_start_mb": round(rss_start / 1024.0, 2) if rss_start else None,
        "rss_end_mb": round(rss_end / 1024.0, 2) if rss_end else None,
        "rss_growth_mb": round(rss_growth, 2) if rss_growth is not None else None,
    }
    print(json.dumps(report, indent=2))
    if len(errors) > args.max_errors:
        return 1
    if args.max_rss_growth_mb and rss_growth is not None and rss_growth > args.max_rss_growth_mb:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
