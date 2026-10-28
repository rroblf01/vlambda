# vlambda

An ASGI-like server for Python lambdas, written in V. Uses vcraft for the V↔Python bridge.

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
- `POST /vlambda` — create lambda (body: Python code)
- `GET /vlambda` — list lambdas
- `DELETE /vlambda/<id>` — delete lambda
- `GET/POST/PUT/PATCH/DELETE /f/<slug>` — invoke lambda

## Status

Iteration 1: HTTP server + SQLite + basic routing. Python execution works via bridge.
