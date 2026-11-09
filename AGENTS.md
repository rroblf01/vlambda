# AGENTS.md

## Layout
- Repository root is the buildable project: `src/`, `python/vlambda/`, `tests/`, `docs/`.
- V source: `src/vlambda_native.v`. Python package: `python/vlambda/`.
- Generated (gitignored): `src/_vcraft_generated.v`, `python/vlambda_native.pyi`, `.vcraft/`, `dist/`, `build/`, `*.so`.

## Requirements
- Python >=3.14; V compiler pinned to commit 36be92642c49a8fe9213ea4b96c6bf56b67b668c; vcraft 1.7.2.
- CI uses prebuilt image ghcr.io/rroblf01/vcraft-manylinux:1.7.2.

## Build (dev)
python -m venv .venv
. .venv/bin/activate
pip install vcraft==1.7.2
VCRAFT_NO_MEMORY_LIMIT=1 vcraft develop

vcraft develop regenerates native extension and installs editable wheel. Set VCRAFT_BIN to override vcraft binary.

Release build: vcraft build --release (and --free-threading for t builds). Release uses V -prod, stricter than develop; unchecked !void results fail compilation.

## Type checking
pyproject.toml configures pyright, mypy, ty with python/ as extra import path. Run from the repository root.

## UI
- Source: `python/vlambda/ui.html`.
- Bundled editor/Chart.js: `python/vlambda/static/app.js` (built from `ui/` with `npm run build`).
- After changing `ui.html` or `app.js`, regenerate the packaged assets: `python scripts/embed_assets.py`.
- Assets served locally via `/assets/<name>`; no CDN.

## Projects
- Upload a ZIP: `POST /vlambda?project=1&entry=main.py` with base64 body.
- Supports multi-file projects and `requirements.txt` (installed at upload time).

## Run
python -m vlambda --port 8080 --sqlite_path ./vlambda.db --num_coroutines 10
Env vars mirror flags (VLAMBDA_PORT, VLAMBDA_DB, ...). Server runs V in daemon thread; main thread sleeps. Ctrl+C unreliable; use /shutdown or kill.

## Tests
- Unit: `python -m pytest tests/test_unit.py -q`
- Integration: `python tests/test_vlambda.py`
- The integration harness self-starts the server (port 9799, temporary DB, max body 100).
- Set `VLAMBDA_BASE` if the repository root is not auto-detected.
- To run one case, call `start_server()` then the test function manually, or trim the test list.

## CI
- .github/workflows/ci.yml runs in manylinux container (preinstalled V/vcraft) for Python 3.14 and 3.15: create venv -> vcraft develop -> integration tests.

## Packages
- Per-lambda/project PyPI packages are installed into a vendor directory via `POST /vlambda/<slug>/packages`.
- Code lambdas use `<VLAMBDA_PACKAGES_DIR>/lambdas/<slug>`; projects use `<project_dir>/vendor`.
- UI: Packages card in the editor and "Install deps" in the project view.

## Docker
- `Dockerfile` installs vlambda from PyPI; `Dockerfile.dev` builds the local wheel.
- Both use uv to create a free-threaded venv and run vlambda directly on port 8080 (no gofly; Dokploy handles TLS and the proxy).
- Each invocation runs in a fresh Landlock + seccomp child forked from a pre-warmed supervisor (`VLAMBDA_SANDBOX=fork`): filesystem allow-list, hidden `/data` and `/proc`, no network or cross-process signals, rlimits. No Docker security flags or host sysctl changes are required (kernel >= 5.13).
- The container speaks HTTP on port 8080; TLS is terminated by Dokploy or another proxy.
- Registration is closed by default in the production image; set `VLAMBDA_REGISTRATION=1` to open it, or create users with `POST /admin/users` and `X-Admin-Token`.
- `VLAMBDA_SANDBOX=fork` (Docker default) forks a fresh Landlock + seccomp child from a pre-warmed supervisor. `landlock`, `nsjail` and `process` are optional modes.
- Reset: `python -m vlambda reset --yes --url http://127.0.0.1:8080 --token $VLAMBDA_ADMIN_TOKEN`.
- See `docker/README.md`.

## Quirks
- `VCRAFT_NO_MEMORY_LIMIT=1` for constrained builds.
- Metrics are persisted to a separate SQLite DB (`VLAMBDA_METRICS_DB`, default `<db>_metrics.db`); retention `VLAMBDA_METRICS_RETENTION` days.
- SQLite runs in WAL mode; sidecar files are .db, -wal, -shm.
- Regenerate ignored artifacts after changing V or Python code.
