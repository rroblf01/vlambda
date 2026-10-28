import subprocess, time, json

# Create a lambda
import urllib.request
data = b'def handler(request): return {"msg": "hi"}'
req = urllib.request.Request("http://localhost:8143/vlambda", data=data, method="POST")
resp = urllib.request.urlopen(req)
slug = json.loads(resp.read())["slug"]

# Benchmark invocation
N = 1000
t0 = time.perf_counter()
for _ in range(N):
    urllib.request.urlopen(f"http://localhost:8143/f/{slug}").read()
dt = time.perf_counter() - t0
print(f"vlambda: {N} invocations in {dt*1000:.1f}ms = {dt/N*1000:.2f}ms/req = {N/dt:.0f} req/s")
