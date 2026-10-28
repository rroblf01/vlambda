# vlambda — findings about vcraft

ASGI-like server for Python lambdas, written in V. Uses vcraft 1.7.0-dev for the V↔Python bridge.

## What works (Iteration 1)

- HTTP server (V stdlib `net.http`) with routing.
- SQLite with WAL mode (V stdlib `db.sqlite`).
- CRUD: create (POST), list (GET), delete (DELETE).
- Lambda invocation: GET /f/<slug> looks up code in SQLite, executes in Python.
- GIL control: V releases GIL before blocking in `listen_and_serve`, HTTP handler
  threads acquire GIL via `with_gil` to call Python.
- Zero-copy: `bytes_view` for request bodies, `to_py_bytes` for responses.
- Sandbox: restricted builtins, blocked modules (os, subprocess, sys, etc.).
- End-to-end: POST code → GET /f/<slug> executes Python and returns JSON.

## vcraft features exercised

- `vcraft.call` — calling Python from V HTTP handler threads.
- `vcraft.with_gil` — acquiring GIL from non-Python threads.
- `vcraft.allow_threads` / `end_allow_threads` — releasing GIL for blocking server.
- `vcraft.to_py_bytes` / `vcraft.from_py_string` / `vcraft.from_py_bytes` — marshalling.
- `vcraft.to_py_obj_dict` — building request dict.
- `vcraft.PyObj` first-class — for executor callable.
- `vcraft.bytes_view` — zero-copy for request bodies (not yet tested under load).

## vcraft findings

### 1. GIL must be released before blocking in V
V's HTTP server runs in V threads that don't hold the GIL. Before calling
`listen_and_serve()` (which blocks), the main Python thread must release the GIL
via `allow_threads()`. Otherwise, HTTP handler threads can't acquire it via
`with_gil`, causing a deadlock. This is documented in vcraft but the pattern
(release before blocking, acquire in handlers) should be emphasized.

### 2. SQLite connections from V threads
V's `db.sqlite` connections are not thread-safe by default. Each handler
creates a new connection (`init_db` + `db.close()`). This works but is slow.
Consider connection pooling or `busy_timeout`.

### 3. `http.Request.data` is a string, not bytes
V's `http.Request.data` is a `string`. For binary bodies, this requires
encoding/decoding. vcraft's `bytes_arg`/`bytes_view` work on Python objects,
not V strings. A `bytes_view`-equivalent for V strings would help.

### 4. No request/response streaming
V's `net.http` buffers the entire request/response. For large bodies, this
is a memory issue. vcraft's `bytes_view` helps for Python-side processing,
but the HTTP layer itself buffers.

### 5. V's `split` is a method, not a function
V's string `.split()` method takes a single separator string. This is fine
but differs from Python's `str.split()` which takes maxsplit. Minor.

## Performance

| Build | ms/req | req/s |
|-------|--------|-------|
| 3.14 (GIL) | 1.04 | 964 |
| 3.15 (GIL) | 0.87 | 1150 |
| 3.15t (no GIL) | 1.03 | 973 |

Bottleneck: SQLite connection per request + Python exec per request.
Free-threading doesn't help (GIL isn't the bottleneck).

## Next iterations

- Iteration 2: Python pool + sandbox (replace exec-per-request with pool). ✅ Done
- Iteration 3: Invocation caching (compile code once).
- Iteration 4: Auth + CRUD complete.
- Iteration 5: Performance + security.

## Recommendations for vcraft

1. Add `bytes_view`-equivalent for V strings (zero-copy V string → Python bytes).
2. Add connection pooling helper for SQLite (or document the pattern).
3. Add streaming request/response support (or document the limitation).
