# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/rroblf01/vlambda/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rroblf01/vlambda/releases/tag/v0.1.0
[0.1.0]: https://github.com/rroblf01/vlambda/releases/tag/v0.1.0
