# API reference

Base URL: the host running vlambda. TLS termination is expected in front of the
server. JSON errors use `{"error": "message"}`.

## Conventions

- **User auth:** `Authorization: Bearer <JWT or API token>`.
- **Admin auth:** `X-Admin-Token: <VLAMBDA_ADMIN_TOKEN>`.
- **Ownership:** management endpoints and OpenAPI only expose the caller's
  lambdas unless the admin token is used.
- **Auth scope:** endpoints outside the Public section require a Bearer token;
  admin endpoints require `X-Admin-Token`.
- **Methods:** a lambda is invoked only with its configured HTTP method; other
  methods return `405`.
- **Request id:** responses include `X-Request-Id`.
- **Health version:** `/health` and `/ready` include the running version.

## Public

| Method | Path | Description |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/ui` | Web UI |
| GET | `/assets/<name>` | Bundled assets |
| GET | `/favicon.ico` | Favicon |
| GET | `/health` | `{"status":"ok","version":"..."}` |
| GET | `/ready` | `200` ready, `503` draining/not ready |
| GET | `/config` | `{"registration": true|false}` |
| GET | `/metrics` | Prometheus text metrics |

## Authentication

| Method | Path | Body | Description |
|---|---|---|---|
| POST | `/register` | `username:password` | Create a user when registration is enabled |
| POST | `/login` | `username:password` | Returns `{"token": "..."}` |
| POST | `/admin/users` | `username:password` | Create a user with `X-Admin-Token` |

Login attempts are throttled per username and source IP.

## Lambdas

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda` | List lambdas (pipe-delimited text) |
| GET | `/vlambda?format=json&limit=N&offset=N` | List lambdas as JSON |
| POST | `/vlambda?name=NAME&method=GET` | Create a code lambda; body is Python source |
| POST | `/vlambda?project=1&entry=main.py&name=NAME` | Create a project; body is base64 ZIP |
| GET | `/vlambda/<slug>` | Read code |
| PUT | `/vlambda/<slug>?name=NAME&method=POST` | Update code/name/method |
| DELETE | `/vlambda/<slug>` | Delete the lambda and its state |

Optional create/update query parameters: `timeout`, `memory_mb`.

## Invocation

| Method | Path | Description |
|---|---|---|
| `GET/POST/PUT/PATCH/DELETE` | `/f/<slug>` | Invoke the lambda |
| OPTIONS | `/f/<slug>` | CORS preflight (`204`) |
| `GET/...` | `/f/<slug>/<path>` | Project static file, or handler fallback |
| POST | `/async/<slug>` | Queue an async invocation; returns `task_id` |
| GET | `/task/<id>` | Read an async task |
| GET | `/dlq` | Dead-letter queue for the caller |

Use `?alias=prod` to invoke a version alias.

## Projects

| Method | Path | Description |
|---|---|---|
| GET | `/project/<slug>` | Read project files and entrypoint |
| PUT | `/project/<slug>` | Save `{"files": {...}, "entrypoint": "main.py"}` |

Static serving excludes Python sources, dotfiles, dependency manifests,
`vendor/`, `__pycache__/` and path traversal.

## Versions and aliases

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/versions` | List versions |
| GET | `/vlambda/<slug>/version/<n>` | Read version source |
| POST | `/vlambda/<slug>/rollback?version=<n>` | Restore a version |
| GET | `/vlambda/<slug>/aliases` | List aliases |
| POST | `/vlambda/<slug>/alias?name=prod&version=<n>` | Create/update an alias |

## Environment and secrets

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/env` | List variables, secrets masked |
| PUT | `/vlambda/<slug>/env` | Replace `{"vars": {...}, "secrets": {...}}` |

Secrets are encrypted at rest and decrypted only for the invocation. Use
`vlambda secrets rotate` to change the encryption key offline.

## KV

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/kv?prefix=` | List entries |
| GET | `/vlambda/<slug>/kv/<key>` | Read one entry |
| PUT | `/vlambda/<slug>/kv/<key>` | Write `{"value": ..., "ttl": 3600}` |
| DELETE | `/vlambda/<slug>/kv/<key>` | Delete one entry |
| DELETE | `/vlambda/<slug>/kv` | Clear the store |

## Egress

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/egress` | Read the allow-list |
| PUT | `/vlambda/<slug>/egress` | Replace `{"hosts": ["api.example.com"]}` |

Only HTTPS through `request.fetch` is allowed by default. Private/reserved
addresses are blocked.

## Schedules

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/schedules` | List schedules |
| POST | `/vlambda/<slug>/schedules` | Create a five-field cron schedule |
| PUT | `/vlambda/<slug>/schedules/<id>` | Update a schedule |
| DELETE | `/vlambda/<slug>/schedules/<id>` | Delete a schedule |
| POST | `/vlambda/<slug>/schedules/<id>/run` | Run now |
| GET | `/vlambda/<slug>/schedules/<id>/runs` | Run history |

## Packages

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/packages` | List installed packages |
| POST | `/vlambda/<slug>/packages` | Install `{"packages": ["six"]}` |

## Export / import / duplicate

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/export` | Export code or project as JSON |
| POST | `/vlambda/import` | Import an exported document |
| POST | `/vlambda/<slug>/duplicate` | Duplicate a lambda or project |

## Metadata and OpenAPI

| Method | Path | Description |
|---|---|---|
| GET | `/vlambda/<slug>/meta` | Read summary, tags and description |
| PUT | `/vlambda/<slug>/meta` | Update metadata |
| GET | `/openapi.json` | Owner-scoped OpenAPI 3.1 document |

## Tokens

| Method | Path | Description |
|---|---|---|
| POST | `/tokens` | Create `{"name": "ci", "scopes": ["read","write"]}` |
| GET | `/tokens` | List tokens; secrets are never returned |
| DELETE | `/tokens/<id>` | Revoke a token |

## Metrics and logs

| Method | Path | Description |
|---|---|---|
| GET | `/metrics/json` | Live JSON metrics |
| GET | `/metrics/history?days=N&slug=` | Raw persisted metrics |
| GET | `/metrics/stats?days=N&slug=` | p50/p95/p99 |
| GET | `/metrics/aggregate?days=N&slug=` | Per-lambda aggregates |
| GET | `/metrics/summary?days=N&slug=` | Daily rollups |
| GET | `/metrics/export?days=N&slug=` | CSV export |
| DELETE | `/metrics/history?days=N&slug=` | Delete metrics |
| GET | `/logs/history?days=N&slug=` | Persisted lambda logs |
| DELETE | `/logs/history?days=N&slug=` | Delete logs |
| GET | `/pool/stats` | Worker pool stats |

## Admin

| Method | Path | Description |
|---|---|---|
| POST | `/admin/users` | Create a user |
| POST | `/admin/reset` | Reset all data (admin token) |
| POST | `/shutdown` | Graceful shutdown (admin token) |

## Errors

| Status | Meaning |
|---|---|
| 400 | Invalid input |
| 401 | Missing/invalid credentials |
| 403 | Not the owner, or a missing scope |
| 404 | Lambda, task or resource not found |
| 405 | Method does not match the lambda |
| 409 | Duplicate name/slug |
| 413 | Body too large |
| 429 | Rate limit exceeded |
| 500 | Handler or server error |
