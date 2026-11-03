# vlambda 0.4.0 roadmap

Status: Phase 1 in progress (capability broker + KV implemented)
Theme: safe capabilities, automation and operations
Baseline: 0.3.0 (`3fb9726`)

## Progress

- [x] Phase 0: migration scaffold, broker skeleton, ADR decisions.
- [x] Phase 1: capability broker, per-lambda KV, REST API, UI card, CLI and tests.
- [ ] Phase 2: controlled egress (`request.fetch`).
- [ ] Phase 3: schedules.
- [ ] Phase 4: OpenAPI.
- [ ] Phase 5: single-host HA.
- [ ] Phase 6: gVisor runtime mode.
- [ ] Phase 7: release.

## Goals

| Priority | Feature | Outcome |
|----------|---------|---------|
| P0 | Per-lambda KV | Persistent key/value state with TTL, quotas and atomic increments |
| P0 | Controlled egress | Allow-listed HTTPS calls from sandboxes without exposing raw sockets |
| P0 | Schedules | Cron-style invocation with owner scoping, run history and a single leader |
| P1 | OpenAPI | Machine-readable spec, owner-scoped `/openapi.json`, simple `/docs`, CLI export |
| P1 | HA (single host) | Multiple replicas on one host with shared SQLite volume, leader leases and graceful drain |
| P2 | gVisor | Documented and tested container runtime mode; internal fork sandbox remains |
| P2 | Hardening | Audit logs, quotas, failure injection, migration and release automation |

## Non-goals for 0.4.0

- Multi-host active/active SQLite. SQLite WAL is not safe on network filesystems.
- Transparent raw network access from user code. Egress goes through an explicit,
  audited capability API.
- Firecracker snapshots, per-invocation microVMs and VM image management.
- A full API gateway (rate limits per route, request transformation, etc.).
- Peer-to-peer state between replicas without an external database.

These can be revisited in 0.5.0 with PostgreSQL/Litestream and a microVM adapter.

## Principles

1. Deny by default: no egress, no KV access outside the lambda, no unowned reads.
2. Keep the Docker deployment simple: one image, no extra caps, no host sysctls.
3. Reuse the existing runner protocol and fork/Landlock/nsjail pools.
4. Additive DB migrations only; 0.3.0 data must keep working.
5. Standard library first. New runtime dependencies need a strong reason.
6. Every new endpoint is authenticated and owner-scoped; admin token sees all only
   where it already does (metrics, logs, tasks, DLQ).
7. Fail closed and explain the failure to the handler with a normal Python exception.

## Architecture

```text
                         HTTP
client / UI ─────────────────────────────▶ V routes
                                              │
                                              │ callbacks
                                              ▼
                                     Python server process
                                      ├─ pool (fork/landlock/nsjail)
                                      ├─ scheduler thread ── SQLite lease
                                      ├─ capability broker thread
                                      └─ metrics / logs / tasks

sandbox runner ── AF_UNIX JSON-RPC ──▶ capability broker
   (no network)                         ├─ KV (SQLite)
                                        └─ egress (HTTPS allow-list)
```

The broker is the only new trust boundary. It runs in the server process, holds
per-invocation capability tokens, and is reachable from the sandbox through a
Unix socket. User code never gets a raw socket to the internet.

## Capability broker

### Transport

- Unix domain socket `VLAMBDA_BROKER_SOCKET` (default
  `/data/tmp/vlambda-broker.sock`, local default `/tmp/opencode/...`), mode `0600`.
- One request/response per connection, newline-delimited JSON. Simpler than a
  framed multiplexed protocol and fast enough for a toy deployment.
- Request: `{"op": "...", ...}`. Response: `{"ok": true, "result": ...}` or
  `{"ok": false, "error": "code: message"}`.

### Tokens

- The server creates a random 32-byte token per invocation and stores
  `{token: {slug, owner_id, request_id, expires_at, counters}}` in memory.
- The token is passed to the runner as an internal field in `request_meta`; it is
  not part of `request.env` and is not logged.
- Tokens expire shortly after the invocation returns or is killed.
- The broker rejects tokens whose slug/owner do not match the operation.

### Sandbox changes

- seccomp: allow AF_UNIX sockets only. Permit `socket(AF_UNIX)`, `connect`,
  `sendto`, `recvfrom`, `shutdown`, `getsockname`, `getpeername`,
  `setsockopt`, `getsockopt`; keep `socket(AF_INET/AF_INET6)`, `bind`, `listen`,
  `accept`, `sendmsg` and `recvmsg` blocked.
- Landlock: allow the broker directory as read-only. The socket itself stays
  outside the project/vendor allow-list.
- Python code lambdas keep `socket` blocked at the builtins level; they use
  `request.kv` / `request.fetch`.
- Projects may import `socket`, but can only reach AF_UNIX; the broker still
  requires the invocation token.

### Limits

- KV: 256 keys, 128-byte keys, 64 KiB values, 1 MiB total per lambda.
- Egress: 20 calls per invocation, 10 s per call, 2 MiB response body.
- Broker: 200 operations per invocation, then `QuotaExceeded`.
- Audit denials through the existing log store; never log secret values or
  authorization headers.

## Per-lambda KV

### Schema

```sql
CREATE TABLE IF NOT EXISTS lambda_kv (
  slug       TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,          -- JSON envelope, see below
  expires_at REAL,
  updated_at REAL NOT NULL,
  PRIMARY KEY (slug, key)
);
```

Values are stored as typed JSON envelopes:
`{"type": "str|int|float|bool|json|bytes", "value": ...}`. Bytes use base64.
Expired rows are ignored on read and removed lazily by the scheduler/cleanup.

### Runtime API

```python
request.kv.get("counter", default=None)
request.kv.set("counter", 1, ttl=3600)
request.kv.delete("counter")
request.kv.list(prefix="")
request.kv.incr("counter", 1)
```

Projects use the same API through `vlambda.kv` (or `from vlambda import kv`).
The code-lambda sandbox exposes it through the `Request` object only.

### HTTP API

- `GET /vlambda/<slug>/kv?prefix=` — list keys and values (owner only).
- `GET /vlambda/<slug>/kv/<key>` — read one key.
- `PUT /vlambda/<slug>/kv/<key>` — write (`{"value": ..., "ttl": 3600}`).
- `DELETE /vlambda/<slug>/kv/<key>` — delete one key.
- `DELETE /vlambda/<slug>/kv` — delete all keys for the lambda.
- UI: KV card/table in the editor with edit, delete and TTL.

## Controlled egress

### Configuration

Per-lambda allow-list of hosts, stored in `lambda_egress(slug, pattern)`.
Patterns are exact hosts (`api.example.com`) or a single leading wildcard
(`*.example.com`). HTTPS only by default; HTTP can be enabled with
`VLAMBDA_EGRESS_ALLOW_HTTP=1` for local development.

API:

- `GET /vlambda/<slug>/egress`
- `PUT /vlambda/<slug>/egress` — replace the list.
- UI: "Egress allow-list" textarea under the environment card.

### Runtime API

```python
resp = request.fetch(
    "https://api.example.com/items",
    method="POST",
    headers={"Authorization": "Bearer ..."},
    json={"name": "demo"},
    timeout=5,
)
resp.status, resp.headers, resp.text, resp.body, resp.json()
```

Projects use `vlambda.fetch`. The helper validates the URL against the same
allow-list before calling the broker.

### SSRF and safety rules

- Resolve the host and reject loopback, private, link-local, multicast and
  unspecified addresses. `VLAMBDA_EGRESS_ALLOW_PRIVATE=1` exists only for tests.
- No automatic redirects. Callers can request one explicit follow, revalidated.
- Strip `Host`, `Content-Length` and hop-by-hop headers; cap header count/size.
- Reject non-HTTP(S) schemes, embedded credentials and fragments.
- Log denied hosts at `warn`; do not log query strings that may contain secrets.
- Document the DNS-rebinding limitation (resolve/check, then fetch by name).

## Schedules

### Schema

```sql
CREATE TABLE IF NOT EXISTS schedules (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  slug        TEXT NOT NULL,
  owner_id    INTEGER NOT NULL,
  name        TEXT NOT NULL,
  cron        TEXT NOT NULL,
  timezone    TEXT NOT NULL DEFAULT 'UTC',
  method      TEXT NOT NULL DEFAULT 'POST',
  path        TEXT NOT NULL DEFAULT '/schedule',
  payload     TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  running     INTEGER NOT NULL DEFAULT 0,
  next_run    REAL,
  last_run    REAL,
  last_status TEXT,
  created_at  REAL NOT NULL,
  UNIQUE(slug, name)
);

CREATE TABLE IF NOT EXISTS schedule_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  schedule_id INTEGER NOT NULL,
  slug        TEXT NOT NULL,
  request_id  TEXT,
  started     REAL NOT NULL,
  finished    REAL,
  status      TEXT NOT NULL,
  error       TEXT
);

CREATE TABLE IF NOT EXISTS scheduler_lease (
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  owner   TEXT NOT NULL,
  expires REAL NOT NULL
);
```

### Scheduler

- One thread per server process; only the lease leader executes runs.
- Lease: `UPDATE ... WHERE expires < now OR owner = me`, renew every 5 s,
  TTL 15 s. On shutdown, release the lease.
- Tick every second. Claim due schedules atomically; skip overlapping runs.
- Missed runs older than `VLAMBDA_SCHEDULE_GRACE` (default 60 s) are recorded as
  `missed` and coalesced to the next slot.
- Cron: five fields (`minute hour day-of-month month day-of-week`), `*`, lists,
  ranges and steps. `zoneinfo` for timezones. Document DST gaps/overlaps.
- Invoke through the existing pool with synthetic metadata:
  `request_id=sched-<uuid>`, `X-Vlambda-Trigger: schedule`,
  `X-Vlambda-Schedule: <name>`, and `request.trigger = "schedule"`.
- Record a `schedule_runs` row, metrics and logs. Retention: 7 days or 1000 runs.
- No automatic retries in 0.4.0; expose `last_status` and a manual run button.

### API / UI / CLI

- `GET /vlambda/<slug>/schedules`
- `POST /vlambda/<slug>/schedules`
- `PUT /vlambda/<slug>/schedules/<id>`
- `DELETE /vlambda/<slug>/schedules/<id>`
- `POST /vlambda/<slug>/schedules/<id>/run`
- `GET /vlambda/<slug>/schedules/<id>/runs`
- UI: schedules card with next/last run, enable toggle and "Run now".
- CLI: `vlambda schedule list|create|delete|run`.

## OpenAPI

- Add `summary`, `description` and `tags` metadata columns to `lambdas`.
- Generator in `python/vlambda/openapi.py`, built from the authenticated user's
  lambdas (admin token sees all).
- One operation per lambda at `/f/<slug>` using the lambda's method. Project
  static paths are described as `GET /f/<slug>/{path}`.
- Generic schemas: JSON, text and binary responses; request body for write
  methods; `X-Request-Id` header; bearer auth for management endpoints.
- `GET /openapi.json` (auth required by default; `VLAMBDA_OPENAPI_PUBLIC=1`
  exposes a sanitized public spec without management endpoints).
- `GET /docs` serves a lightweight bundled page that fetches the spec. No CDN.
- CLI: `vlambda openapi --out openapi.json`.
- UI: summary/description/tags fields in the lambda editor.

## HA (single host)

### Scope

Active/active replicas on one host, sharing a local volume and an optional Redis
cache. This is real HA for process crashes and rolling restarts, not for host
loss. Multi-host HA needs an external database and is deferred to 0.5.0.

### Work

- Add `VLAMBDA_INSTANCE_ID` (default hostname + pid) and expose it in
  `/pool/stats`.
- Leader leases for the scheduler, orphan GC and any periodic cleanup.
- Task queue claiming: add `claimed_by` and `lease_until`; workers claim pending
  or expired tasks, and reclaim interrupted tasks on startup.
- Graceful drain: stop accepting new requests, finish in-flight work, stop the
  scheduler, release leases, close the pool and DB. `/shutdown` uses the same
  path with a short grace period.
- Readiness: `/ready` stays false until the executor, DB and leases are ready.
- Compose example with two replicas behind a proxy, shared volume, Redis cache
  and `stop_grace_period`. Document that the volume must be local (not NFS).
- Metrics: scheduler leader, task claims, drain duration.

### Exit criteria

- Kill the leader mid-run: the second replica takes the lease and continues.
- No duplicate scheduled runs or task executions across replicas.
- Rolling restart with zero failed requests in a simple load test.
- Documented limitations and a clear path to multi-host.

## gVisor

- Support gVisor as a container runtime, not as a new per-invocation sandbox:
  `docker run --runtime=runsc ...` or Kubernetes `runtimeClassName: gvisor`.
- Keep the internal fork/Landlock/seccomp sandbox; gVisor adds a second boundary.
- Add `docker/compose.gvisor.yml` and deployment docs.
- Add a CI job that runs the image under `runsc` when the runtime is available;
  skip with a clear message otherwise.
- Optional `VLAMBDA_SANDBOX=gvisor` per-invocation adapter is a stretch goal and
  must fail closed if `runsc` is not configured. Firecracker is 0.5.0.

## Database migrations

- Keep the current idempotent `CREATE TABLE IF NOT EXISTS` / `ALTER TABLE ... or {}`
  approach in `init_db`.
- Add only nullable/defaulted columns and new tables.
- New tables: `lambda_kv`, `lambda_egress`, `schedules`, `schedule_runs`,
  `scheduler_lease`.
- New columns: `lambdas.summary`, `lambdas.description`, `lambdas.tags`,
  `tasks.claimed_by`, `tasks.lease_until`.
- Add a `schema_version` row for diagnostics; never rewrite existing data.
- Test upgrade from a 0.3.0 database with users, lambdas, env and tasks.

## Observability

New metrics (Prometheus + `/metrics/json`):

- `vlambda_kv_ops_total{op,result}`
- `vlambda_egress_requests_total{result}`
- `vlambda_schedules_due_total`
- `vlambda_schedule_runs_total{status}`
- `vlambda_scheduler_leader` (0/1)
- `vlambda_task_claims_total{result}`

New log events: capability denied, quota exceeded, schedule started/finished,
leader acquired/lost. Reuse the existing log sink and retention.

## Test plan

- Unit: cron parser (ranges, steps, timezones, DST), KV typed values/TTL/incr,
  egress allow-list and SSRF checks, capability token scope/expiry, OpenAPI shape.
- Integration: KV CRUD from a code lambda and a project; TTL expiry; quota.
- Integration: egress allowed/denied, private IP blocked, redirect blocked,
  response size/time limits, no raw AF_INET socket.
- Integration: schedule create/run/enable/disable, run history, leader takeover,
  no duplicate runs, missed-run policy.
- Integration: OpenAPI auth and owner scoping, `/docs` loads without CDN.
- HA: two replicas sharing a volume, kill leader, task recovery, rolling restart.
- Upgrade: 0.3.0 DB to 0.4.0.
- Docker: full flow under fork, read-only rootfs, no caps; gVisor job when available.
- Security regression: existing sandbox escapes (`/data`, `/proc`, network,
  signals, fork/exec) must still fail.

## Phases and estimates

| Phase | Deliverable | Estimate |
|-------|-------------|----------|
| 0 | ADRs, migration scaffold, broker skeleton, feature flags | 1-2 days |
| 1 | Capability broker + KV (runtime, API, UI, tests) | 3-4 days |
| 2 | Egress allow-list + `request.fetch` + SSRF tests | 2-3 days |
| 3 | Schedules, leader lease, history, UI/CLI | 3-4 days |
| 4 | OpenAPI metadata, generator, `/docs`, CLI | 2 days |
| 5 | HA single host: leases, task claims, drain, compose, chaos tests | 4-5 days |
| 6 | gVisor runtime docs/CI, hardening, quotas | 2 days |
| 7 | Docs, changelog, release build, Docker, PyPI | 2 days |

Estimates assume one developer and no upstream changes in V/vcraft.

## Security invariants

- Sandboxes never get AF_INET/AF_INET6 sockets.
- Broker tokens are per invocation, expire quickly and are scoped to one lambda.
- KV and schedule endpoints are owner-only; admin access only where documented.
- Egress deny-list wins over allow-list; private/reserved IPs are blocked.
- OpenAPI is owner-scoped by default.
- Schedule and task execution is single-leader on a shared database.
- Secrets are never included in logs, OpenAPI output or generated docs.
- New failures are explicit exceptions, not silent fallbacks.

## Open questions / decisions needed

1. HA scope: single-host multi-process for 0.4.0 (recommended) vs multi-host
   PostgreSQL in 0.4.0. Multi-host is a substantially larger change.
2. Egress API: explicit `request.fetch` (recommended, auditable) vs a transparent
   HTTP proxy for `requests`/`httpx`. The latter needs more sandbox work.
3. Schedules: five-field cron (recommended) vs six-field with seconds.
4. gVisor: container runtime mode (recommended) vs per-invocation OCI bundles.

## Release checklist

- [ ] All new endpoints documented in README and OpenAPI.
- [ ] 0.3.0 database upgrade test passes.
- [ ] Unit + integration + Docker suites pass.
- [ ] Release build (`vcraft build --release`) and free-threaded wheel pass.
- [ ] gVisor CI job passes or skips cleanly.
- [ ] CHANGELOG, README, docker/README and compose examples updated.
- [ ] `v0.4.0` tag, GitHub Release and PyPI publish.
- [ ] Production Docker build uses the published 0.4.0 wheel.
