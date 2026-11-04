"""Benchmark vlambda throughput."""

import urllib.request, time, json, sys, os

BASE = os.environ.get("VLAMBDA_URL", "http://localhost:8080")
N = int(os.environ.get("VLAMBDA_N", "2000"))
CONCURRENCY = int(os.environ.get("VLAMBDA_CONCURRENCY", "1"))

# Register + create lambda
try:
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/register", data=b"bench:pass", method="POST")).read()
except urllib.error.HTTPError:
    pass  # already exists
resp = urllib.request.urlopen(urllib.request.Request(f"{BASE}/login", data=b"bench:pass", method="POST"))
token = json.loads(resp.read())["token"]
req = urllib.request.Request(f"{BASE}/vlambda", data=b"def handler(request): return {'msg': 'hi'}",
                             headers={"Authorization": f"Bearer {token}"}, method="POST")
resp = urllib.request.urlopen(req)
slug = json.loads(resp.read())["slug"]
print(f"slug: {slug}", file=sys.stderr)

def worker(n, results):
    t0 = time.perf_counter()
    for _ in range(n):
        urllib.request.urlopen(f"{BASE}/f/{slug}").read()
    results.append(time.perf_counter() - t0)

if CONCURRENCY == 1:
    t0 = time.perf_counter()
    for _ in range(N):
        urllib.request.urlopen(f"{BASE}/f/{slug}").read()
    dt = time.perf_counter() - t0
else:
    import threading
    per = N // CONCURRENCY
    results = []
    threads = [threading.Thread(target=worker, args=(per, results)) for _ in range(CONCURRENCY)]
    t0 = time.perf_counter()
    for t in threads: t.start()
    for t in threads: t.join()
    dt = time.perf_counter() - t0

rps = N / dt
print(f"vlambda: {N} invocations in {dt*1000:.1f}ms = {dt/N*1000:.2f}ms/req = {rps:.0f} req/s (concurrency={CONCURRENCY})")
min_rps = float(os.environ.get("VLAMBDA_MIN_RPS", "0"))
if min_rps and rps < min_rps:
    print(f"vlambda: {rps:.0f} req/s is below VLAMBDA_MIN_RPS={min_rps:.0f}", file=sys.stderr)
    sys.exit(1)
