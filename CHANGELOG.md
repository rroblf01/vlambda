# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-11-09

### Added
- Username validation (1-64 characters, letters, digits and `_ . @ + -`).
- `VLAMBDA_TRUST_PROXY` so deployments behind a reverse proxy use `X-Forwarded-For` for rate limits, login throttling and `request.remote_ip`.
- `VLAMBDA_MAX_RESPONSE` (10 MiB default) caps lambda response bodies; responses with more than 100 headers are rejected.
- `VLAMBDA_MAX_TASKS` and `VLAMBDA_MAX_TASKS_PER_OWNER` make the async queue limits configurable; new tasks are rejected instead of growing the queue past the limit.

### Changed
- The example Compose deployments default to `VLAMBDA_REGISTRATION=0` and build `VLAMBDA_VERSION=1.0.0`, matching the production image.
- Documentation describes 1.0 as the current line, opt-in CORS, the `--online-only` reset mode, `VLAMBDA_TRUST_PROXY`, response and task limits, and JSON password changes.
- `request.path` no longer includes the query string; `request.query_string` and `request.query` carry it separately.
- The web UI markup, JavaScript and CSS are separate files embedded at build time, and V routes use shared `json_response`/`text_response` helpers.
- `PUT /vlambda/<slug>` and the rollback/alias flows preserve the lambda's timeout and memory settings when the query omits them.
- A token with the `admin` scope now also grants write access; admin tokens can still create and revoke tokens.
- `POST /password` accepts `{"old": "...", "new": "..."}` in addition to `old:new`, so passwords may contain colons.

### Fixed
- `PUT /vlambda/<slug>` returns `404` for a missing lambda and `403` for another user's lambda instead of a misleading `200`; `DELETE` follows the same rules.
- Alias creation rejects versions that do not exist, and invalid `version`/`days` query values return `400` instead of being silently coerced.
- `/metrics/history` honors the `slug` filter.
- Invalid or wrongly typed token-creation bodies return `400` instead of silently creating a read/write token.
- Project uploads reject absolute paths, path traversal, URLs and local paths in `requirements.txt`/`pyproject.toml`; dependencies are installed with `--only-binary :all:` and never by building the uploaded project.
- Project dependencies are copied into each project instead of being symlinked to a shared vendor tree, so the fork/Landlock allow-list can read them and one tenant cannot modify another tenant's packages.
- Package installs for projects materialize a private vendor tree, failed installs no longer leave a poisoned requirements file, and the package cache no longer deletes per-lambda package directories or vendor trees in use.
- Deleting a lambda eventually removes its package directory, and dependency installs are serialized so pruning cannot race a copy.
- Backup restore checks every destination before moving anything, so a conflict cannot leave a new database next to old metrics or project files.
- The Docker images include `uv`, without which package and project dependency installs failed, and keep uv/pip caches and `HOME` on the writable `/data` volume when the root filesystem is read-only.
- `POST /admin/reset` returns `500` when the reset fails instead of reporting success.
- `/task/<id>` returns `404` for unknown tasks and `500` for configuration errors.
- Schedule `enabled` must be a boolean instead of coercing arbitrary strings to true.
- Environment variables reject nested objects and lists instead of stringifying them.
- Egress host patterns must be strings, and OpenAPI metadata rejects wrong types.
- A stray `vlambda_draining = false` in `/admin/reset` could cancel a concurrent graceful shutdown; it has been removed.
- CSV metric exports escape cells that start with spreadsheet formula characters.
- Handler `print()` output no longer corrupts the subprocess runner protocol; stdout/stderr are redirected while user code runs.

### Security
- A data reset now deletes `api_tokens`, `auth_throttle` and `audit_log`; previously an API token issued before a reset could authenticate as the recreated user with the same numeric ID, and old login blocks survived.
- The reset CLI no longer falls back to direct SQLite deletes after an HTTP error (for example a rejected admin token). It falls back only when the server is unreachable, and `--online-only` disables the fallback entirely.
- Fork sandbox children no longer inherit a Landlock rule for the whole `/data/projects` and `/data/packages` trees, so one tenant cannot read another tenant's project files.
- seccomp now allows `prlimit64` only for the calling process and blocks `fcntl(F_SETOWN|F_SETSIG|F_SETOWN_EX)`, closing cross-process signal and limit manipulation paths.
- Project and package installs no longer accept direct URLs, git references, local paths or pip options, preventing server-side code execution during upload.
- Client IP parsing handles IPv6 addresses instead of collapsing them to an empty bucket.
- Log messages are capped at 64 KiB to limit disk growth.

## [1.0.0rc1] - 2026-11-04

### Added
- `vlambda backup` and `vlambda restore`: consistent offline archives of the main database, metrics/logs database, projects and optional package vendors.
- Database schema versions (`PRAGMA user_version`) with startup checks and backup compatibility validation.
- `vlambda reset --wipe` removes the database files for a fresh 1.0 start.
- `vlambda secrets rotate` re-encrypts stored secrets between encryption keys; ciphertexts carry a key identifier and rotation is idempotent.
- Login throttling per username and source IP (`VLAMBDA_LOGIN_MAX_FAILURES`, `VLAMBDA_LOGIN_WINDOW`, `VLAMBDA_LOGIN_BLOCK`).
- Startup checks for writable project/package/cache directories and for Landlock + libseccomp when a confined sandbox is requested.
- `SECURITY.md` with the disclosure process and the 1.0 threat model.
- CycloneDX SBOM generation in CI and a GitHub Container Registry image workflow for `linux/amd64` and `linux/arm64`.
- `vlambda --version` and the running version in `/health` and `/ready`.
- API, CLI, compatibility, upgrade and operations references, plus `CONTRIBUTING.md` and the 1.0 roadmap.
- API tokens accept an optional `expires_in` (seconds); expired tokens are rejected and listed as `expired`.
- Administrative actions are recorded in `audit_log` and exposed read-only at `GET /admin/audit` with the admin token.
- Per-user rate limiting (`VLAMBDA_USER_RATE_LIMIT`) gives each authenticated account its own budget.
- `POST /password` changes the current password and revokes every existing session; `POST /admin/password` lets an administrator reset a user password.
- Audit entries expire after `VLAMBDA_AUDIT_RETENTION_DAYS` (default 90).
- HSTS is sent when vlambda terminates TLS itself.
- Backups can be encrypted with `vlambda backup --encrypt` (chunked, authenticated, key never stored).
- The runner uses a seccomp allow-list by default, with a denylist fallback.
- Login and registration throttles are shared through SQLite instead of process memory.
- CORS is disabled by default; set `VLAMBDA_CORS` explicitly to enable it.
- `VLAMBDA_SANDBOX=auto` picks the fork sandbox when Landlock and seccomp are available.
- A 5-minute sandboxed soak (156,603 requests, concurrency 8) completed with 0 errors, 522 req/s, p95 19.4 ms and flat RSS.
- The UI documents the password policy and includes a change-password card.

### Changed
- 1.0 targets single-host deployments on Linux x86_64/aarch64. Multi-host active/active is out of scope.
- The 1.0 schema starts fresh; databases created before 1.0 are not migrated.
- The production Docker image sets `VLAMBDA_REGISTRATION=0`; create users with the admin token or opt in to public registration.
- Container images use the latest Python 3.15 free-threaded runtime and are published only after the release workflow and PyPI publish succeed.
- CI runs a 55% unit-coverage floor, an aarch64 integration job, failure-isolation and fuzz smoke tests; manual soak and throughput gates are documented.
- Percentile stats are cached (`VLAMBDA_METRICS_STATS_TTL`, default 15s) and slug/time queries use a composite index.
- Code-lambda invocations skip the per-request project lookup, and DB helpers use a lightweight connection instead of re-running schema DDL.
- Compiled code is keyed by `slug:version` instead of hashing the full source on every invocation; project metadata is cached like code metadata.
- Release images only move the `latest` tag for final versions, and manual release dispatches must match `pyproject.toml`.

### Security
- The server refuses to start without `VLAMBDA_SECRET`; `VLAMBDA_DEV=1` is the explicit development escape hatch.
- Lambda code no longer receives `Authorization`, `Proxy-Authorization`, `Cookie`, `X-Api-Key` or `X-Admin-Token` headers.
- Egress pins the validated IP with the original Host/SNI, closing the DNS-rebinding window.
- Passwords require 12 characters by default, PBKDF2 uses 600k iterations, and registration is limited per source IP.
- The UI sends `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` and a CSP.
- `SECURITY.md` documents the secret encryption construction, backup encryption guidance and the per-user rate limit.

### Fixed
- `SIGINT`/`SIGTERM` handlers are reinstalled in the run loop, so `Ctrl+C` and `docker stop` follow the graceful drain path.
- The Docker smoke test and the release wheel smoke test use a password that satisfies the 1.0 policy.
- Concurrent invocations no longer share the native result buffer; under load a request could receive `not started` or another invocation's response.
- Native callback results are copied under an explicit GIL scope instead of a closure, and helper queries open a lightweight connection. A 2-minute in-process soak (156k requests) stays under 10 MB of RSS growth.
- Every native callback (invocation, auth, projects, KV, env, egress, schedules, logs, metrics, tasks and assets) uses an explicit GIL scope; no `with_gil` closures remain.

### Removed
- The bundled `/docs` viewer. `/openapi.json` and `vlambda openapi` remain the supported interface for OpenAPI clients.

## [0.4.0] - 2026-11-04

### Added
- Async handlers: lambdas and projects may define `async def handler(request)`; the coroutine is awaited before serialization. The editor ships an "Async handler" template.
- Per-lambda KV store with typed values, TTL, atomic increments, quotas, REST API, UI card and CLI.
- Capability broker over a Unix socket: per-invocation tokens, scoped KV access and automatic release for sync and async invocations.
- Sandboxes now allow only `AF_UNIX` client sockets; `AF_INET`/`AF_INET6` remain blocked.
- Per-lambda egress allow-lists, `request.fetch`/`vlambda.fetch`, SSRF protections, response limits and an egress CLI.
- Egress audit logs for denied hosts and quota failures.
- Persistent five-field cron schedules with timezones, per-schedule method/path/payload, run history, manual runs and a UI card.
- Single-leader scheduler lease, overlap skipping and missed-run recording.
- OpenAPI metadata for lambdas, owner-scoped `/openapi.json`, a bundled `/docs` viewer and `vlambda openapi` export.
- gVisor container-runtime mode, compose override and a skip-aware smoke script.
- Single-host HA building blocks: persistent task payloads, cross-replica task claiming with leases, leader-only project GC, graceful drain and a compose/nginx example.

### Security
- The broker token is passed as internal request metadata, not through the lambda environment.
- KV operations are owner-checked at the HTTP layer and scoped to the token's slug at runtime.
- Egress rejects private, loopback, link-local and reserved addresses, URL credentials, fragments and redirects.
- Schedules are owner-only and only the lease leader executes automatic runs.
- OpenAPI is owner-scoped unless `VLAMBDA_OPENAPI_PUBLIC=1` is explicitly set.
- `/ready` reports draining and `/shutdown` waits for in-flight work before exit.
- Async task metadata is persisted without decrypted environment values; secrets are read only at execution time.

### Fixed
- Recovered async tasks are claimed after a replica crash instead of remaining stuck in `running`.
- Landlock and nsjail pools now forward request metadata, so KV/env/egress work in every sandbox mode.
- The scheduler lease prevents duplicate automatic runs across replicas.
- Deleting a lambda also removes its KV, egress, schedules, runs and pending tasks.
- Python 3.15 uses a rollback journal when CPython and system SQLite versions differ, avoiding WAL files being unlinked across libraries.
- Native responses and callback results are copied out of CPython memory before the Python object is released, fixing truncated responses and intermittent crashes (notably on Python 3.15).
- The compiled-code disk cache is scoped per interpreter and published atomically, so a shared cache can no longer feed the server invalid code objects.

## [0.3.0] - 2026-11-03

### Added
- Pre-warmed fork supervisor pool: each supervisor imports Python once and forks a fresh, Landlock + seccomp confined child per invocation.
- Canonical `Response` contract with status codes, headers, cookies and binary bodies; `dict`/`str`/`bytes` returns remain supported.
- Full request metadata: real headers, cookies, parsed query, remote IP, host, scheme and `request.env`.
- Per-lambda environment variables and encrypted secrets (`/vlambda/<slug>/env`); code lambdas use `request.env` and projects also get `os.environ`.
- Owner-scoped metrics and logs, plus `X-Admin-Token` access for the admin view.
- Persistent async task queue and dead-letter queue in SQLite.
- `vlambda` CLI with `deploy`, `invoke`, `logs`, `env` and `task` commands.

### Changed
- Docker uses `VLAMBDA_SANDBOX=fork` by default. The per-invocation process is still fresh, but Python startup is no longer paid on every request.
- Latency target for a trivial sandboxed invocation is in the low-millisecond range (~10-20 ms in the Docker test image).
- The GET response cache is opt-in (`VLAMBDA_RESP_CACHE_TTL`, default 0) and skips handlers that read request context, log, use randomness or time.

### Security
- `/async/<slug>`, `/task/<id>` and `/dlq` now require authentication; tasks and DLQ entries are scoped to their owner.
- `Response` rejects invalid statuses, header names and CRLF injection, and drops hop-by-hop headers.
- `request.headers` no longer exposes `X-Admin-Token` to lambda code.
- Per-invocation environment values are cleared from persistent workers, so one lambda cannot read another's secrets; reserved `VLAMBDA_`/`VLRUN_` names are rejected.
- Encrypted secrets fail closed per key instead of dropping plain variables when the master key is missing or rotated.
- The web UI escapes log levels and project file names.
- Startup prewarm and import/duplicate no longer execute lambda or project code inside the server process; project code only runs in the selected sandbox.
- Passwords use PBKDF2-HMAC-SHA256 with a per-user salt; legacy hashes remain valid until a reset or password change.
- Deleting a lambda also removes its environment, versions and aliases, and project slugs are unique per upload.
- Project files, versions, aliases and version code are owner-only; public static serving excludes Python sources, dotfiles and dependency manifests.
- In-memory metrics no longer expose other users' lambda slugs: `/metrics` is aggregate-only and `/metrics/json` is owner-scoped.
- `/shutdown` and the admin routes require `X-Admin-Token`.
- Runner processes get an environment allow-list instead of the whole server environment (cloud credentials and database URLs no longer leak).

### Fixed
- Async tasks keep the caller's slug, headers and decrypted environment, and their logs are attributed correctly.
- A handler returning a dict with `status`/`body` keys is serialized as data instead of being mistaken for the internal response envelope.
- Environment saves keep unspecified categories and masked secrets; the CLI merges individual keys, and the response cache is invalidated after a change.

## [0.2.4] - 2026-11-03

### Fixed
- Logs emitted with `request.log()` by code and project lambdas now travel back from the Landlock runner and are persisted by the server.
- Project invocations now carry `request_id` and `slug`, so their logs are attributed to the right lambda and invocation.
- Code lambdas that call `request.log()` bypass the GET response cache, so their logs are emitted on every invocation.

### Changed
- The image precompiles Python bytecode and sandbox runners load only the modules they need, cutting the per-invocation startup cost roughly in half. Latency metrics include this fresh-process startup, so they remain higher than the persistent in-process mode.

## [0.2.3] - 2026-11-03

### Fixed
- JWT sessions are invalidated when the database is reset or a user is deleted; old tokens can no longer create lambdas for a missing or recreated user.
- API tokens now require their owning user to still exist.

### Security
- Added a random per-user `auth_epoch` to the users table and JWT payload. Tokens from a previous database generation are rejected even if SQLite reuses the same user ID.

## [0.2.2] - 2026-11-03

### Added
- `VLAMBDA_SANDBOX=landlock` runs each invocation in an unprivileged Landlock + seccomp sandbox without Docker security flags or host sysctl changes.

### Changed
- Docker images use the Landlock sandbox by default; nsjail, `seccomp=unconfined`, `VLAMBDA_NSJAIL` and `VLAMBDA_ISOLATE` are no longer required for the deployment.
- The runner only sees `/opt`, `/usr`, `/lib`, `/etc`, the current project/vendor and one writable temporary directory.

### Security
- Landlock blocks access to `/data`, `/proc`, `/sys`, `/home` and other server paths.
- seccomp blocks network, cross-process signals, `ptrace`, `mount`, namespace changes, `bpf`, `io_uring` and similar syscalls.
- Each invocation gets a fresh runner process and temporary directory; no state is shared between requests.

## [0.2.1] - 2026-11-03

### Added
- `VLAMBDA_REGISTRATION` (default `1`) to close public signup; `GET /config` exposes the flag and the UI hides the registration button when closed.
- `POST /admin/users` for creating accounts with `X-Admin-Token` when registration is disabled.

### Changed
- Docker images now run vlambda directly on port 8080; gofly was removed because Dokploy terminates TLS and proxies requests.
- Project handlers now run in the same runner pool as code lambdas, including separate processes when `VLAMBDA_ISOLATE=1`.
- Docker images keep public registration open by default; set `VLAMBDA_REGISTRATION=0` to close it.

### Fixed
- Project invocations no longer corrupt Python interpreter state and crash the server; request fields are passed to the project runner as plain strings/bytes and handlers run outside the V HTTP thread.

### Security
- Lambda runner processes no longer inherit `VLAMBDA_SECRET` or `VLAMBDA_ADMIN_TOKEN`.
- Blocked additional escape-prone modules, including `posix`, `_socket`, `resource`, `fcntl` and `sqlite3`.
- Container processes drop all capabilities and create data files with `umask 077`.
- Project code no longer runs inside the server process.
- Each invocation runs in nsjail with separate user, PID, mount and network namespaces, a read-only view without `/data` or the server `/proc`, no network, rlimits and a seccomp deny-list.

## [0.2.0] - 2026-11-03

### Added
- Real CORS on `/f/<slug>` responses (`--cors_origin` / `VLAMBDA_CORS`) with `OPTIONS` preflight.
- Project static files: `/f/<slug>/<path>` serves `index.html`, CSS, JS and assets with content types and path-traversal protection; unknown paths fall through to the handler.
- Long-lived API tokens (PAT) with hashed storage, names, `read`/`write`/`admin` scopes and revocation (`/tokens`).
- `GET /vlambda?format=json` for clean JSON listings, while keeping the pipe-delimited format for older clients.
- Export, import and duplicate endpoints for lambdas and projects (`/vlambda/<slug>/export`, `/vlambda/import`, `/vlambda/<slug>/duplicate`).
- UI actions for export/import/duplicate and an API tokens manager in the System view.
- POC templates for JSON API + CORS, webhooks and form receivers, plus `examples/` with runnable projects.
- Docker deployment with a uv-managed free-threaded Python 3.14 venv, gofly, nsjail and HTTP-only operation behind Dokploy.
- `vlambda reset` CLI and `POST /admin/reset` for weekly demo cleanup.
- Install PyPI packages per lambda/project from the web UI.
- Persistent lambda logs via `request.log()` and a "Save logs" template.
- Metrics filter by lambda slug or name.
- Delete metrics and logs from the UI or with `DELETE /metrics/history` and `DELETE /logs/history`, optionally pruned by age and lambda.
- Lambda invoke URLs in the dashboard, editor and project view, with copy/open actions.
- HTML page template built with `str.format()`; HTML responses are served as `text/html`.
- Redesigned IDE: theme-aware CodeMirror, file toolbar, project tabs and a cursor/status bar.
- Full web UI revamp: local CodeMirror 6 editor, lambdas dashboard with search/filter/pagination, API playground, live metrics charts, logs viewer, system status and frontend log drawer.
- Multi-file project editor with file tree, tabs, entrypoint and requirements editing.
- Version viewer/rollback, aliases management, templates, theme toggle, keyboard shortcuts and live metrics polling.
- Project file read/save endpoints (`GET/PUT /project/<slug>`), project versions and aliases listing.
- Per-lambda timeout quotas and alias/version support.
- Request tracing IDs and aggregated metrics endpoints (summary, stats, CSV export).
- Pool stats endpoint and optional shared cache backend.
- Optional shared response cache backend (memory/file/Redis) and `/ready` endpoint for horizontal scaling.
- Multi-file project uploads as ZIP archives with configurable entrypoint.
- Per-project dependencies from `requirements.txt` and `pyproject.toml`, installed into an isolated `vendor/` directory.
- Shared dependency store keyed by dependency set, with size-based pruning.
- Token-bucket rate limiting per IP with configurable burst.
- Persistent invocation metrics and logs in a separate SQLite database.
- `/metrics/history` and `/metrics/stats` endpoints with retention and percentiles.
- Dead-letter queue and retries for asynchronous tasks.
- Ownership enforcement for lambda get/delete operations.
- Memory pressure handling with `503` responses and configurable hard limits.
- Atomic pool round-robin, configurable worker cap, queue size and backpressure.
- Free-threaded (`cp314t`, `cp315t`) Linux wheels.
- Unit and integration test suites, including concurrency, projects, rate limiting, metrics persistence and sandbox checks.
- Project cache prewarming and async task thread pool.

### Changed
- The lambda and project forms keep their submit buttons disabled until the required fields are filled, and mark the name as required.
- Docker deployment is HTTP-only on port 8080; Dokploy terminates TLS.
- Project layout moved from `vlambda/` to the repository root.
- vcraft updated from 1.7.0 to 1.7.2.
- CI and release workflows now use the prebuilt manylinux container.
- Response cache stores serialized payloads for GET requests.
- Compiled handlers are cached and executed without per-request `exec`.
- Metrics are optional and sampled via environment variables.
- Logs now follow the same retention period as metrics.
- In-memory lambda metrics are optional.
- Async tasks no longer spawn one thread per task.
- Removed optional Nuitka compilation path.
- Release workflow now includes provenance attestation.

### Fixed
- Lambda and project names are URL-decoded (spaces and accents included), updates keep the existing name when none is supplied, and the UI requires a name with a clear message instead of silently falling back to the slug.
- Project cleanup no longer deletes a project uploaded while the orphan scan is running.
- The UI no longer flashes an "unauthorized" alert right after login when a request made with an expired token finishes late.
- Lambda invocation pages no longer serve the UI favicon; `/favicon.ico` returns 404 when requested from `/f/<slug>`.
- Metrics, logs, pool stats and DLQ endpoints now require authentication; the UI redirects to login.
- Metrics charts now register all Chart.js controllers, reuse canvases and keep a fixed 260px height.
- Lambda results containing `bytes` (for example `request.body`) are serialized safely instead of failing with "execution failed".
- The JSON API template decodes the request body before returning it.
- Unchecked `db.close()` results that broke release (`-prod`) builds.
- Worker threads terminating on response-cache hits.
- Platform tags for Linux wheels (`manylinux_2_28`).
- Source distribution format converted to PAX for PyPI (handled by vcraft 1.7.2).
- Ownership checks for lambda deletion.
- Rate limiter burst sizing to avoid blocking normal setup requests.

### Performance
- Response cache returns cached serialized payloads.
- Handler compilation is cached across requests and warm starts.
- Atomic pool selection reduces lock contention.
- Benchmarks after optimizations: ~2293-3019 req/s at concurrency 10.

## [0.1.0] - 2026-10-28

### Added
- Async lambda invocation (`/async/<slug>`, `/task/<id>`)
- Web UI dashboard (login, lambda management, code editor, metrics)
- Per-lambda metrics with Chart.js visualization
- Prometheus-compatible `/metrics` endpoint
- `/metrics/json` endpoint for UI charts
- Input validation (ast.parse) before storing code
- Pagination in lambda list (`?limit=N&offset=M`)
- Human-readable names for lambdas
- HTTP method selector (GET, POST, PUT, PATCH, DELETE) per lambda
- SQLite connection pool for reduced per-request overhead
- GC tuning (disabled during requests, periodic collection)
- Arena allocator for per-request memory
- LRU code cache (compiled Python code objects)
- Disk cache (marshal .pyc for faster restarts)
- Response cache for idempotent GET requests
- Single-flight (request coalescing for identical in-flight requests)
- `/shutdown` endpoint for graceful stop
- `/favicon.ico` endpoint
- Free-threaded Python support (3.14t, 3.15t)
- Nuitka compilation (opt-in via `VLAMBDA_NUITKA=1`)
- CORS support
- Rate limiting per IP
- Body size limit
- TLS/HTTPS support (via V stdlib)

### Changed
- Latency measurement now uses microseconds for better precision
- Metrics exclude API/navigation requests from counter
- UI: slug links to invoke URL in new tab
- UI: pre-filled code example in editor
- UI: edit button in lambda list
- UI: metrics nav fixed (was not triggering view switch)
- UI: table cell alignment fixed (vertical-align: middle)
- UI: delete modal hidden by default (!important)
- `db_list_lambdas` now returns `slug|name|method` format
- `db_create_lambda` and `db_update_lambda` accept `name` parameter

### Fixed
- GIL safety: all Python callbacks wrapped in `with_gil`
- Segfault in `auth_verify_token` (missing GIL acquisition)
- Segfault in `prewarm_lambda` (missing GIL acquisition)
- Slug collisions between users (added counter to hash)
- Body size check ordering (now before validation)
- `__slots__` in Request for reduced memory overhead

## [0.1.0] - 2026-10-28

### Added
- Initial release
- HTTP server (V stdlib `net.http`) with routing
- SQLite storage (WAL mode, busy_timeout)
- CRUD: create (POST), list (GET), delete (DELETE)
- Lambda invocation: GET /f/<slug>
- Python bridge via vcraft (V↔Python)
- Sandbox: restricted builtins, blocked modules (os, subprocess, sys, etc.)
- JWT authentication (HS256)
- User registration and login
- Python worker pool with LRU code cache
- Web UI with pure CSS and vanilla JS
- Prometheus-compatible metrics
- Health check endpoint
- CLI with environment variable support
- MIT License

### Performance
- ~3,000 req/s (concurrent, free-threaded Python)
- ~0.3ms per invocation (warm)
- No cold starts (persistent process)

[1.0.0]: https://github.com/rroblf01/vlambda/compare/v0.4.0...v1.0.0
[1.0.0rc1]: https://github.com/rroblf01/vlambda/compare/v0.4.0...v1.0.0rc1
[0.3.0]: https://github.com/rroblf01/vlambda/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/rroblf01/vlambda/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/rroblf01/vlambda/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/rroblf01/vlambda/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/rroblf01/vlambda/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/rroblf01/vlambda/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rroblf01/vlambda/releases/tag/v0.1.0
