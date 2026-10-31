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
- **Multi-file projects** — ZIP uploads, file editor, `requirements.txt`/`pyproject.toml`
- **Versions and aliases** — rollback and `prod`/`staging` aliases
- **PyPI packages** — install per lambda/project from the UI
- **Persistent metrics and logs** — survive restarts, queryable by slug/name
- **Free-threaded builds** — `cp314t`/`cp315t` wheels

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

## Docker + Dokploy

The repository includes a production image (`Dockerfile`, installs from PyPI) and
a development image (`Dockerfile.dev`, builds the local wheel). Both run gofly +
vlambda + nsjail inside one container and expose plain HTTP on port `8080`.

```bash
export VLAMBDA_SECRET="$(openssl rand -hex 32)"
export VLAMBDA_ADMIN_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker/compose.yml up -d --build
```

With Dokploy, create an application from the image, map the domain to container
port `8080`, and set `VLAMBDA_SECRET` and `VLAMBDA_ADMIN_TOKEN`. Dokploy terminates
TLS, so the container does not manage certificates.

See `docker/README.md` for details, limits and the weekly reset command.

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

### Projects
- `POST /vlambda?project=1&entry=main.py` — Upload a ZIP project (base64 body)
- `GET /project/<slug>` — Read project files
- `PUT /project/<slug>` — Save project files

### Versions and aliases
- `GET /vlambda/<slug>/versions` — List versions
- `GET /vlambda/<slug>/version/<n>` — Read a version
- `POST /vlambda/<slug>/rollback?version=<n>` — Roll back
- `GET /vlambda/<slug>/aliases` — List aliases
- `POST /vlambda/<slug>/alias?name=prod&version=<n>` — Create alias
- `GET /f/<slug>?alias=prod` — Invoke an alias

### Packages
- `GET /vlambda/<slug>/packages` — List requirements
- `POST /vlambda/<slug>/packages` — Install PyPI packages into the vendor dir

### Monitoring (requires auth)
- `GET /health` — Health check
- `GET /ready` — Readiness
- `GET /metrics` — Prometheus metrics
- `GET /metrics/json` — Live JSON metrics
- `GET /metrics/history?days=N&slug=` — Raw persisted metrics
- `GET /metrics/stats?days=N&slug=` — p50/p95/p99
- `GET /metrics/aggregate?days=N&slug=` — Per-lambda aggregates
- `GET /metrics/summary?days=N&slug=` — Daily rollups
- `GET /metrics/export?days=N&slug=` — CSV export
- `GET /logs/history?days=N&slug=` — Persisted lambda logs
- `GET /pool/stats`, `GET /dlq`
- `POST /shutdown` — Graceful shutdown

## Logging

Lambdas can persist structured logs with `request.log()`:

```python
def handler(request):
    request.log("processing " + request.path)
    request.log("debug details", "debug")
    return {"ok": True}
```

Logs are stored in the metrics database and can be queried with
`GET /logs/history?days=1&slug=<slug>`. The UI Logs view shows them alongside
invocations. The "Save logs" template in the editor is a ready-to-run example.

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
- Lambda dashboard with search, filter and pagination
- CodeMirror editor with Python highlighting
- Multi-file project editor and ZIP upload
- Versions, rollback and aliases
- PyPI package installation
- API playground
- Persistent metrics with charts and filters
- Logs viewer with `request.log()`
- System status and frontend log drawer

## Reset

The demo can be wiped with:

```bash
python -m vlambda reset --yes --url http://127.0.0.1:8081 --token "$VLAMBDA_ADMIN_TOKEN"
```

For the weekly demo reset, run it from cron every Monday:

```cron
0 0 * * 1 docker exec -e VLAMBDA_ADMIN_TOKEN="..." <container>   /opt/venv/bin/python -m vlambda reset --yes   --url http://127.0.0.1:8081
```

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
