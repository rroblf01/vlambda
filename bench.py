import urllib.request, time, json

BASE = "http://localhost:8146"
N = 2000

# Use existing slug
slug = "l46a75218059e5978"

t0 = time.perf_counter()
for _ in range(N):
    urllib.request.urlopen(f"{BASE}/f/{slug}").read()
dt = time.perf_counter() - t0
print(f"vlambda (pool): {N} invocations in {dt*1000:.1f}ms = {dt/N*1000:.2f}ms/req = {N/dt:.0f} req/s")
