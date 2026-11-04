# vlambda

An ASGI-like server for Python lambdas, written in V. Uses [vcraft](https://github.com/rroblf01/vcraft) for the V↔Python bridge.

**Live at:** https://vlambda.ricardorobles.es

## What is vlambda?

vlambda lets you write Python functions ("lambdas") and serve them as HTTP endpoints. It's like AWS Lambda, but self-hosted, with no cold start in persistent mode and a fresh sandbox per invocation in Docker (~10-20 ms p50).

```python
def handler(request):
    return {"msg": "hello", "path": request.path}
```

POST that code to `/vlambda` and it becomes `GET /f/<slug>` instantly.

## Features

- **No cold starts in persistent mode** — Docker uses a fresh Landlock process per invocation, which adds a small startup cost
- **Fast** — ~3,000 req/s in-process; the Docker fork sandbox adds a few milliseconds per invocation
- **Auth** — JWT-based authentication with per-user ownership
- **Web UI** — Dashboard for managing lambdas, code editor, metrics
- **Async invocation** — Fire-and-forget with task tracking
- **KV store** — per-lambda typed key/value state with TTL and atomic increments
- **Controlled egress** — allow-listed HTTPS calls from the sandbox via `request.fetch`
- **Schedules** — persistent five-field cron jobs with timezones, run history and a single leader
- **OpenAPI** — owner-scoped spec, metadata editor, `/docs` viewer and CLI export
- **Sandbox** — Landlock + seccomp in Docker, plus restricted Python builtins and blocked modules
- **Metrics** — Prometheus-compatible + Chart.js dashboard
- **Per-lambda method** — GET, POST, PUT, PATCH, DELETE
- **Naming** — Human-readable names for lambdas
- **Multi-file projects** — ZIP uploads, file editor, `requirements.txt`/`pyproject.toml`
- **Versions and aliases** — rollback and `prod`/`staging` aliases
- **PyPI packages** — install per lambda/project from the UI
- **Persistent metrics and logs** — survive restarts, queryable by slug/name
- **CORS** — applied to every `/f/` response, with `OPTIONS` preflight
- **Static files** — projects serve `index.html`, CSS, JS and assets at `/f/<slug>/<path>`
- **API tokens** — long-lived tokens with scopes and revocation
- **Export / import / duplicate** — move lambdas and projects as JSON
- **POC templates** — HTML + `fetch`, webhook, JSON API, static site
- **Free-threaded builds** — `cp314t`/`cp315t` wheels

## Roadmap

0.4.0 is in planning: per-lambda KV, allow-listed egress, cron schedules,
OpenAPI, single-host HA and an optional gVisor runtime. See
[docs/ROADMAP-0.4.0.md](docs/ROADMAP-0.4.0.md) for architecture, API contracts,
phases and release criteria.

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

## Build a POC in five minutes

1. Open the UI and log in.
2. Pick a template in **New lambda** (`JSON API + CORS`, `Webhook receiver`,
   `Form receiver`, `HTML page`…) and create it.
3. Open the invoke URL `https://your-host/f/<slug>`.
4. Need files? Upload a ZIP in **Upload project**; anything in the project is
   served at `/f/<slug>/<path>` and any unknown path runs `handler(request)`.

More complete examples live in [`examples/`](examples/): a static site, an
HTML + `fetch` POC, a webhook receiver and a JSON API.

## API tokens

Create a token in **System → API tokens**, or with the API:

```bash
curl -X POST "$VLAMBDA_URL/tokens" \
  -H "Authorization: Bearer $JWT" \
  -H "Content-Type: application/json" \
  -d '{"name":"ci","scopes":["read","write"]}'
```

The response contains the token (`vl_…`) once. Use it as
`Authorization: Bearer vl_…`. Scopes are `read`, `write` and `admin`; revoke a
token with `DELETE /tokens/<id>`. Tokens do not expire.

## Docker + Dokploy

The repository includes a production image (`Dockerfile`, installs from PyPI) and
a development image (`Dockerfile.dev`, builds the local wheel). Both use a
free-threaded Python 3.14 venv, a pre-warmed fork supervisor and a fresh
Landlock + seccomp child per invocation, and expose plain HTTP on port `8080`;
Dokploy (or another proxy) handles TLS and the public domain.

The sandbox needs no extra Docker flags, capabilities or host sysctl changes.
It requires a Linux kernel with Landlock (5.13+; Ubuntu 22.04+, Debian 12+).

```bash
export VLAMBDA_SECRET="$(openssl rand -hex 32)"
export VLAMBDA_ADMIN_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker/compose.yml up -d --build
```

With Dokploy, create an application from the image, map the domain to container
port `8080`, and set `VLAMBDA_SECRET` and `VLAMBDA_ADMIN_TOKEN`. Dokploy terminates
TLS, so the container does not manage certificates.

See `docker/README.md` for details, limits and the reset command.

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
| | `VLAMBDA_REGISTRATION` | 1 | Allow public registration (`0` disables it) |
| | `VLAMBDA_SQLITE_JOURNAL` | auto | `wal` or `delete`; auto falls back to `delete` when CPython and system SQLite versions differ |
| | `VLAMBDA_RESP_CACHE_TTL` | 0 | GET response cache TTL in seconds (0 disables it) |
| | `VLAMBDA_CACHE_BACKEND` | memory | `memory`, `file` or `redis` for the optional response cache |

vlambda normally uses SQLite WAL. When CPython bundles a SQLite version
different from the system `libsqlite3` used by the V extension (for example on
Python 3.15), it automatically falls back to the rollback journal to avoid
cross-library WAL corruption. Set `VLAMBDA_SQLITE_JOURNAL=wal` or `delete` to
override it.

The GET response cache is disabled by default because a handler can depend on
headers, cookies, `request.env`, time or other request state. Enable it only for
stateless, idempotent GET lambdas; handlers that read request context, log,
`random` or `time.time` are skipped automatically.

## API

### Authentication
- `GET /config` — Public runtime flags (whether registration is open)
- `POST /register` — Register user (body: `username:password`); returns `403` when registration is disabled
- `POST /login` — Login (body: `username:password`) → JWT token
- `POST /admin/users` — Create a user with `X-Admin-Token` and body `username:password` (for closed registration)

### Lambdas (requires auth)
- `POST /vlambda?method=GET&name=my-lambda` — Create lambda (body: Python code)
- `GET /vlambda` — List lambdas (pipe-delimited)
- `GET /vlambda?format=json` — List lambdas as JSON
- `GET /vlambda/<slug>` — Get lambda code
- `PUT /vlambda/<slug>?method=POST&name=new-name` — Update lambda
- `DELETE /vlambda/<slug>` — Delete lambda
- `GET /vlambda/<slug>/env` — List environment variables (secrets masked)
- `PUT /vlambda/<slug>/env` — Replace vars/secrets: `{"vars": {...}, "secrets": {...}}`

### API tokens (requires auth)
- `POST /tokens` — Create a token: `{"name":"ci","scopes":["read","write"]}`
- `GET /tokens` — List tokens (never returns the secret)
- `DELETE /tokens/<id>` — Revoke a token

Tokens are accepted anywhere a JWT is accepted. `read` allows listing and
reading, `write` allows mutating lambdas/projects, `admin` allows token
management and admin routes.

### Export / import
- `GET /vlambda/<slug>/export` — Export code or project files as JSON
- `POST /vlambda/import` — Create from an exported JSON body
- `POST /vlambda/<slug>/duplicate` — Copy a lambda or project

Project exports include text files. Binary files are listed in
`skipped_files` and are not copied.

### Invocation
- `GET/POST/PUT/PATCH/DELETE /f/<slug>` — Invoke lambda (method must match)
- `OPTIONS /f/<slug>` — CORS preflight (returns `204`)
- `GET /f/<slug>/<path>` — Serve a project file, or invoke the handler if the
  file does not exist
- `POST /async/<slug>` — Async invoke (returns task_id)
- `GET /task/<id>` — Get async task result

### Projects
- `POST /vlambda?project=1&entry=main.py` — Upload a ZIP project (base64 body)
- `GET /project/<slug>` — Read project files
- `PUT /project/<slug>` — Save project files
- `GET /f/<slug>/` — Serve `index.html`
- `GET /f/<slug>/<path>` — Serve a static file (`vendor/`, `.git/` and `..` blocked)

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

### KV, egress and schedules
- `GET/PUT/DELETE /vlambda/<slug>/kv[/<key>]` — Read, write or delete KV entries
- `GET/PUT /vlambda/<slug>/egress` — Read or replace the egress allow-list
- `GET/POST /vlambda/<slug>/schedules` — List or create schedules
- `PUT/DELETE /vlambda/<slug>/schedules/<id>` — Update or delete a schedule
- `POST /vlambda/<slug>/schedules/<id>/run` — Run a schedule now
- `GET /vlambda/<slug>/schedules/<id>/runs` — Run history

### Metadata and OpenAPI
- `GET/PUT /vlambda/<slug>/meta` — Summary, tags and description
- `GET /openapi.json` — Owner-scoped OpenAPI 3.1 document
- `GET /docs` — Bundled OpenAPI viewer

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
- `DELETE /metrics/history?days=N&slug=` — Delete metrics older than N days (`days=0` deletes all)
- `GET /logs/history?days=N&slug=` — Persisted lambda logs
- `DELETE /logs/history?days=N&slug=` — Delete logs older than N days (`days=0` deletes all)
- `GET /pool/stats`, `GET /dlq`
- `POST /shutdown` — Graceful shutdown (requires `X-Admin-Token`)

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

Metrics and logs are scoped to the authenticated user. The admin token
(`X-Admin-Token`) can query all users through the API/CLI.

Metrics and logs are pruned after `VLAMBDA_METRICS_RETENTION` days (default 7).
From the UI you can also prune data older than the selected period, or delete
all matching rows, with the current lambda filter applied.

## Lambda Format

Every lambda must define a `handler` function. The canonical contract returns a
`Response`; returning a `dict`, `str` or `bytes` is still accepted as shorthand:

```python
from vlambda import Response

def handler(request):
    return Response.json(
        {"msg": "hello", "path": request.path},
        status=201,
        headers={"X-Example": "yes"},
    )
```

The `request` object has: `method`, `path`, `query_string`, `query`, `headers`,
`cookies`, `body`, `remote_ip`, `host`, `scheme`, `request_id`, `slug`, `env`,
and helpers `header()`, `json()`, `form()` and `log()`.

`Response` supports `json()`, `text()`, `html()`, `redirect()`, `no_content()`,
status codes, headers, cookies and binary bodies.

Safe builtins: `abs`, `all`, `any`, `bool`, `dict`, `enumerate`, `filter`, `float`, `int`, `len`, `list`, `map`, `max`, `min`, `range`, `reversed`, `round`, `set`, `sorted`, `str`, `sum`, `tuple`, `zip`, `print`, etc.

Blocked modules: `os`, `posix`, `subprocess`, `sys`, `importlib`, `ctypes`, `socket`, `_socket`, `urllib`, `http`, `shutil`, `pathlib`, `io`, `pickle`, `marshal`, `resource`, `fcntl`, `mmap`, `sqlite3`, etc.

Safe modules: `json`, `math`, `re`, `datetime`, `collections`, `itertools`, `functools`, `hashlib`, `base64`, `string`, `random`, `statistics`, etc.

### Security

With the Docker default (`VLAMBDA_SANDBOX=fork`), user code runs in a fresh
runner process confined with Landlock and seccomp.
Landlock limits the filesystem to `/opt`, `/usr`, `/lib`, `/etc`, the current
project/vendor and a private temporary directory; `/data`, `/proc`, `/sys` and
the rest of the server cannot be read or listed. seccomp blocks TCP/UDP and every
non-Unix socket, cross-process signals, `ptrace`, `mount`, namespace changes and
similar syscalls. Only `AF_UNIX` client sockets to the local capability broker
are allowed. Do not mount the Docker socket, cloud metadata proxies or other
privileged Unix sockets into the container: the sandbox needs Unix sockets for
the broker and cannot distinguish them from other local sockets. Resource
limits are applied too, and each invocation gets a fresh process.

The plain `python -m vlambda` command without `VLAMBDA_SANDBOX` uses the
in-process pool and is a development mode: lambdas run inside the server process
with only Python-level restrictions. Python 3.15 defaults to the `process` pool
instead because handler threads can crash that interpreter. Use
`VLAMBDA_SANDBOX=fork` (Docker default), `landlock`, `process` or `nsjail` for any
untrusted code.

The Docker image keeps public registration open by default for toy deployments.
Set `VLAMBDA_REGISTRATION=0` to close it and create users with
`POST /admin/users` instead. A daily data reset removes state, but it does not
undo a kernel-level escape. Keep this deployment disposable and do not store
valuable secrets in it; for stronger isolation use gVisor, Firecracker or one
container per tenant.

## Environment and secrets

Set per-lambda variables with `PUT /vlambda/<slug>/env`:

```json
{
  "vars": {"PLAIN": "hello"},
  "secrets": {"TOKEN": "s3cret"}
}
```

Secrets are encrypted at rest, masked by the API and decrypted only for that
lambda's runner. Code lambdas read them from `request.env`; projects also see
them in `os.environ`. Use `VLAMBDA_ENCRYPTION_KEY` if you want to rotate
`VLAMBDA_SECRET` without losing existing secrets.

## KV store

Every lambda has a small persistent key/value store, isolated by lambda and
accessible through an allow-listed capability broker rather than a database
connection in the sandbox.

```python
def handler(request):
    hits = request.kv.incr("hits")
    request.kv.set("last", {"path": request.path}, ttl=3600)
    return {"hits": hits, "last": request.kv.get("last")}
```

Projects use the same API with `from vlambda import kv`. Values can be strings,
numbers, booleans, null, JSON documents or bytes; `incr()` is atomic and `ttl`
is optional. Limits are 256 keys, 64 KiB per value and 1 MiB per lambda.

Management API (owner only, `X-Admin-Token` not needed):

- `GET /vlambda/<slug>/kv?prefix=` — list
- `GET /vlambda/<slug>/kv/<key>` — read
- `PUT /vlambda/<slug>/kv/<key>` — write `{"value": ..., "ttl": 3600}`
- `DELETE /vlambda/<slug>/kv/<key>` — delete one key
- `DELETE /vlambda/<slug>/kv` — clear the store

The UI exposes a KV card in the code and project editors.

## Egress

Lambdas cannot open TCP sockets. They can call allow-listed hostnames through
the capability broker:

```python
def handler(request):
    response = request.fetch("https://api.example.com/items", timeout=5)
    return {"status": response.status, "data": response.json()}
```

Projects use `from vlambda import fetch`. Supported methods are `GET`, `HEAD`,
`POST`, `PUT`, `PATCH` and `DELETE`; pass `data=` for raw bodies or
`json_payload=` for JSON. The response exposes `status`, `headers`, `body`,
`text` and `json()`.

Configure the allow-list per lambda (`GET/PUT /vlambda/<slug>/egress`):

```bash
python -m vlambda egress <slug> --set api.example.com --set "*.example.org"
```

Matching is exact, or `*.example.org` for the apex and subdomains. HTTPS is
required by default; set `VLAMBDA_EGRESS_ALLOW_HTTP=1` for local development.
Private, loopback, link-local and reserved addresses are rejected; set
`VLAMBDA_EGRESS_ALLOW_PRIVATE=1` only for tests. Redirects are not followed.
Each invocation is limited to 20 calls, 1 MiB request bodies and 2 MiB
responses, with a 10 s maximum timeout. Denied hosts are logged without query
strings.

## Schedules

Create persistent cron jobs per lambda:

```bash
python -m vlambda schedule create <slug> --name nightly --cron "0 3 * * *" \
  --method POST --path /schedule --payload '{"job": "cleanup"}'
python -m vlambda schedule list <slug>
python -m vlambda schedule run <slug> <id>
python -m vlambda schedule runs <slug> <id>
python -m vlambda schedule delete <slug> <id>
```

Supported expressions use five fields (`minute hour day-of-month month weekday`),
with lists, ranges and steps, plus month/day names and IANA timezones. The
scheduler runs one leader per database lease; overlapping runs are skipped and
windows missed by more than `VLAMBDA_SCHEDULE_GRACE` seconds are recorded as
`missed`. Run history is retained for `VLAMBDA_SCHEDULE_RETENTION` days (7 by
default, max 1000 runs per schedule).

API:

- `GET/POST /vlambda/<slug>/schedules`
- `PUT/DELETE /vlambda/<slug>/schedules/<id>`
- `POST /vlambda/<slug>/schedules/<id>/run`
- `GET /vlambda/<slug>/schedules/<id>/runs`

The UI exposes a schedules card in the code and project editors.

## gVisor

For a second isolation boundary, run the container with the gVisor runtime:

```bash
docker run --runtime=runsc ... ghcr.io/rroblf01/vlambda:latest
# or merge the compose override:
docker compose -f docker/compose.yml -f docker/compose.gvisor.yml up -d
```

Kubernetes uses `runtimeClassName: gvisor` on the pod. The internal fork +
Landlock + seccomp sandbox remains active. `scripts/gvisor-smoke.sh` builds the
image, runs the health check under `runsc` and skips cleanly when gVisor is not
installed. Firecracker/microVM-per-invocation is deferred to 0.5.0.

## High availability (single host)

0.4.0 supports multiple replicas on one host sharing a local `/data` volume.
SQLite WAL, a scheduler lease, task claiming and graceful drain keep scheduled
runs and tasks single-execution across replicas.

```bash
docker compose -f docker/compose.ha.yml up -d --build --scale vlambda=2
```

The sample puts nginx in front of two replicas. Do not place SQLite on NFS; use
a local volume on the same host. Multi-host HA needs an external database and is
deferred to 0.5.0.

Relevant settings:

- `VLAMBDA_INSTANCE_ID` — unique replica name (defaults to hostname:pid)
- `VLAMBDA_DRAIN_TIMEOUT` — graceful shutdown wait (seconds, default 20)
- `VLAMBDA_TASK_LEASE` — task claim lease (seconds, default 600)
- `VLAMBDA_SCHEDULE_GRACE` / `VLAMBDA_SCHEDULE_RETENTION` — cron behavior
- `/ready` returns `503` while a replica is draining

## OpenAPI

Every lambda can carry `summary`, `tags` and `description` metadata, editable
from the code/project editor or `PUT /vlambda/<slug>/meta`. The generated spec is
available at `/openapi.json` (authenticated and owner-scoped by default; set
`VLAMBDA_OPENAPI_PUBLIC=1` for a public spec) and a lightweight viewer is served
at `/docs` without external assets.

```bash
python -m vlambda openapi --out openapi.json
```

The spec describes `/f/<slug>` for every owned lambda, its method, request body
for write methods, generic JSON/text/binary responses and the `X-Request-Id`
header. Project static files are described under `/f/<slug>/{path}`.

## CLI

The CLI talks to a running server and uses `VLAMBDA_URL`/`VLAMBDA_TOKEN` or
`--url`/`--token`:

```bash
python -m vlambda deploy main.py --name my-lambda --method GET
python -m vlambda invoke <slug>
python -m vlambda logs --slug <slug> --days 1
python -m vlambda env <slug> --set PLAIN=hello --secret TOKEN=s3cret
python -m vlambda kv <slug> --set hits=0 --ttl 3600
python -m vlambda kv <slug> --get hits
python -m vlambda egress <slug> --set api.example.com
python -m vlambda schedule list <slug>
python -m vlambda openapi --out openapi.json
python -m vlambda task <task-id>
```

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
python -m vlambda reset --yes --url http://127.0.0.1:8080 --token "$VLAMBDA_ADMIN_TOKEN"
```

A reset also invalidates existing JWT sessions and API tokens.

For a disposable demo, run it from cron every day:

```cron
0 3 * * * docker exec -e VLAMBDA_ADMIN_TOKEN="..." <container> /opt/venv/bin/python -m vlambda reset --yes --url http://127.0.0.1:8080
```

## Performance

| Metric | Value |
|--------|-------|
| Cold start | 0ms in persistent mode; fresh sandbox process in Docker |
| Warm invocation (in-process) | ~0.3ms |
| Warm invocation (Docker fork sandbox) | ~10-20ms p50 |
| Throughput (concurrent, free-threaded) | ~3,000 req/s in-process |
| Response cache | Opt-in via `VLAMBDA_RESP_CACHE_TTL`; ~100x faster for stateless GETs |

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
