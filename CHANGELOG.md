# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-10-31

### Added
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
- Project layout moved from `vlambda/` to the repository root.
- vcraft updated from 1.7.0 to 1.7.2.
- CI and release workflows now use the prebuilt manylinux container.
- Response cache stores serialized payloads for GET requests.
- Compiled handlers are cached and executed without per-request `exec`.
- Metrics are optional and sampled via environment variables.
- In-memory lambda metrics are optional.
- Async tasks no longer spawn one thread per task.
- Removed optional Nuitka compilation path.
- Release workflow now includes provenance attestation.

### Fixed
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

[Unreleased]: https://github.com/rroblf01/vlambda/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/rroblf01/vlambda/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rroblf01/vlambda/releases/tag/v0.1.0
