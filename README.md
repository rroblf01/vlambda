# vlambda

An ASGI-like server for Python lambdas, written in V. Uses vcraft for the V↔Python bridge.

Published at: https://vlambda.ricardorobles.es

## Usage

```bash
python -m vlambda --port 8080 --sqlite_path ./vlambda.db --num_coroutines 10
```

Or with env vars:
```bash
VLAMBDA_PORT=8080 VLAMBDA_DB=./vlambda.db VLAMBDA_COROUTINES=10 python -m vlambda
```

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

## Endpoints

- `GET /` — health check
- `GET /health` — JSON health check
- `GET /metrics` — Prometheus metrics
- `POST /register` — register user (body: `username:password`)
- `POST /login` — login (body: `username:password`) → JWT token
- `POST /vlambda` — create lambda (body: Python code, requires auth)
- `GET /vlambda` — list lambdas (requires auth)
- `PUT /vlambda/<slug>` — update lambda code (requires auth)
- `DELETE /vlambda/<slug>` — delete lambda (requires auth)
- `GET/POST/PUT/PATCH/DELETE /f/<slug>` — invoke lambda

## Authentication

All `/vlambda` endpoints require a `Authorization: Bearer <token>` header.
Obtain a token via `POST /login`.

## Scalability

- **Worker pool** with configurable size (auto-scales via bounded queue + backpressure).
- **LRU code cache** (1024 entries) — compiles once, reuses.
- **Rate limiting** per IP (configurable req/s).
- **Body size limit** to prevent DoS.
- **Invocation timeout** (configurable).
- **RAM monitoring** with warning at 90% of limit.
- **Graceful shutdown** (SIGTERM/SIGINT).
- **CORS** support for browser clients.
- **TLS/HTTPS** support.
- **Prometheus metrics** endpoint.

## Status

Iteration 5: Full scalability + auth + CRUD + caching + sandbox + pool + tests.

## Findings

See `FINDINGS.md` for vcraft findings and recommendations.
