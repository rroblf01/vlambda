# vlambda — findings about vcraft

ASGI-like server for Python lambdas, written in V. Uses vcraft 1.7.0-dev for the
V↔Python bridge.

## What works (Iteration 1)

- HTTP server (V stdlib `net.http`) with routing.
- SQLite with WAL mode (V stdlib `db.sqlite`).
- CRUD: POST /vlambda (create), GET /vlambda (list), DELETE /vlambda/<id>.
- Lambda invocation: GET /f/<slug> looks up code in SQLite, executes in Python.
- GIL control: V releases GIL before blocking in `listen_and_serve`, HTTP handler
  threads acquire GIL via `with_gil` to call Python.
- Zero-copy: `bytes_view` for request bodies, `to_py_bytes` for responses.

## vcraft features exercised

- `vcraft.call` — calling Python from V HTTP handler threads.
- `vcraft.with_gil` — acquiring GIL from non-Python threads.
- `vcraft.allow_threads` / `end_allow_threads` — releasing GIL for blocking server.
- `vcraft.to_py_bytes` / `vcraft.from_py_string` / `vcraft.from_py_bytes` — marshalling.
- `vcraft.to_py_obj_dict` — building request dict.
- `vcraft.PyObj` first-class — for executor callable.

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

### 3. `req.data` is a string, not bytes
V's `http.Request.data` is a `string`. For binary bodies, this requires
encoding/decoding. vcraft's `bytes_arg`/`bytes_view` work on Python objects,
not V strings. A `bytes_view`-equivalent for V strings would help.

### 4. No request/response streaming
V's `net.http` buffers the entire request/response. For large bodies, this
is a memory issue. vcraft's `bytes_view` helps for Python-side processing,
but the HTTP layer itself buffers.

## Performance

Not yet benchmarked. Expected bottlenecks:
- SQLite connection per request (create/close overhead).
- Python exec per request (compile cached, but exec still runs).
- JSON serialization (Python json vs orjson/vjson).

## Next iterations

- Iteration 2: Python pool + sandbox (replace exec-per-request with pool).
- Iteration 3: Invocation caching (compile code once).
- Iteration 4: Auth + CRUD complete.
- Iteration 5: Performance + security.
