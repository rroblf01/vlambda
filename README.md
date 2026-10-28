# vlambda

An ASGI-like server for Python lambdas, written in V. Uses [vcraft](https://github.com/rroblf01/vcraft) for the V↔Python bridge.

**Live at:** https://vlambda.ricardorobles.es

## What is vlambda?

vlambda lets you write Python functions ("lambdas") and serve them as HTTP endpoints. It's like AWS Lambda, but self-hosted, with no cold starts, and ~0.3ms per invocation.

```python
def handler(request):
    return {"msg": "hello", "path": request.path}
```

POST that code to `/vlambda` and it becomes `GET /f/<slug>` instantly.

## Features

- **No cold starts** — lambdas are compiled once and cached in memory
- **Fast** — ~3,000 req/s (concurrent, free-threaded Python), ~0.3ms/req
- **Auth** — JWT-based authentication with per-user ownership
- **Web UI** — Dashboard for managing lambdas, code editor, metrics
- **Async invocation** — Fire-and-forget with task tracking
- **Sandbox** — Restricted Python builtins, blocked dangerous modules
- **Metrics** — Prometheus-compatible + Chart.js dashboard
- **Per-lambda method** — GET, POST, PUT, PATCH, DELETE
- **Naming** — Human-readable names for lambdas

## Installation

```bash
uv pip install vlambda
```

Or with pip:
```bash
pip install vlambda
```

## Usage

```bash
python -m vlambda --port 8080 --sqlite_path ./vlambda.db --num_coroutines 10
```

Or with environment variables:
```bash
VLAMBDA_PORT=8080 VLAMBDA_DB=./vlambda.db VLAMBDA_COROUTINES=10 python -m vlambda
```

Then open http://localhost:8080/ in your browser.

## CLI Options

| Flag | Env Var | Default | Description |
|------|---------|---------|-------------|
| `--port` | `VLAMBDA_PORT` | 8080 | Listen port |
| `--sqlite_path` | `VLAMBDA_DB` | ./vlambda.db | SQLite database path |
| `--num_coroutines` | `VLAMBDA_COROUTINES` | 10 | Worker pool size |
| `--max_body_size` | `VLAMBDA_MAX_BODY` | 1048576 | Max request body (bytes) |
| `--rate_limit` | `VLAMBDA_RATE_LIMIT` | 100 | Requests/sec per IP |
| `--invoke_timeout` | `VLAMBDA_TIMEOUT` | 30 | Lambda invocation timeout (s) |
| `--max_memory_mb` | `VLAMBDA_MAX_MEMORY_MB` | 0 | RAM limit (0=unlimited) |
| `--cors_origin` | `VLAMBDA_CORS` | * | CORS allowed origin |
| `--cert` | `VLAMBDA_CERT` | | TLS certificate path |
| `--key` | `VLAMBDA_KEY` | | TLS key path |

## API

### Authentication
- `POST /register` — Register user (body: `username:password`)
- `POST /login` — Login (body: `username:password`) → JWT token

### Lambdas (requires auth)
- `POST /vlambda?method=GET&name=my-lambda` — Create lambda (body: Python code)
- `GET /vlambda` — List lambdas
- `GET /vlambda/<slug>` — Get lambda code
- `PUT /vlambda/<slug>?method=POST&name=new-name` — Update lambda
- `DELETE /vlambda/<slug>` — Delete lambda

### Invocation
- `GET/POST/PUT/PATCH/DELETE /f/<slug>` — Invoke lambda (method must match)
- `POST /async/<slug>` — Async invoke (returns task_id)
- `GET /task/<id>` — Get async task result

### Monitoring
- `GET /health` — Health check
- `GET /metrics` — Prometheus metrics
- `GET /metrics/json` — JSON metrics (for charts)
- `POST /shutdown` — Graceful shutdown

## Lambda Format

Every lambda must define a `handler` function:

```python
def handler(request):
    return {"msg": "hello", "path": request.path}
```

The `request` object has: `method`, `path`, `query_string`, `headers`, `body`.

Safe builtins: `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `filter`, `float`, `int`, `len`, `list`, `map`, `max`, `min`, `range`, `reversed`, `round`, `set`, `sorted`, `str`, `sum`, `tuple`, `zip`, `print`, etc.

Blocked modules: `os`, `subprocess`, `sys`, `importlib`, `ctypes`, `socket`, `urllib`, `http`, `shutil`, `pathlib`, `io`, `pickle`, `marshal`, etc.

Safe modules: `json`, `math`, `re`, `datetime`, `collections`, `itertools`, `functools`, `hashlib`, `base64`, `string`, `random`, `statistics`, etc.

## Web UI

Open http://localhost:8080/ in your browser for the dashboard:
- Login/Register
- List, create, edit, delete lambdas
- Code editor with syntax highlighting
- Run lambdas in-browser
- Metrics with Chart.js charts

## Performance

| Metric | Value |
|--------|-------|
| Cold start | 0ms (persistent process) |
| Warm invocation | ~0.3ms |
| Throughput (concurrent, free-threaded) | ~3,000 req/s |
| Throughput (response cache hits) | ~100x faster |

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   Browser   │────▶│  V HTTP      │────▶│  Python     │
│   (UI)      │◀────│  Server      │◀────│  Pool       │
└─────────────┘     │  (net.http)  │     │  (workers)  │
                    │              │     └─────────────┘
                    │  ┌────────┐  │           │
                    │  │ SQLite │  │           │
                    │  │ (WAL)  │  │     ┌─────────────┐
                    │  └────────┘  │     │  Sandbox    │
                    └──────────────┘     │  (restricted│
                                         │   builtins) │
                                         └─────────────┘
```

## License

MIT License - see [LICENSE](LICENSE) for details.

## Author

Ricardo Robles Fernández - https://github.com/rroblf01/vlambda
