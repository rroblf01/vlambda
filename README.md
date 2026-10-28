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

## Endpoints

- `GET /` — health check
- `GET /health` — JSON health check
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

## Status

Iteration 4: Auth (JWT) + CRUD + caching + sandbox + pool. ~1000 req/s.

## Findings

See `FINDINGS.md` for vcraft findings and recommendations.
