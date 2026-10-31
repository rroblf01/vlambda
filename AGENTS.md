# AGENTS.md

## Layout
- Repo root holds tests/, bench.py, docs. Buildable project is vlambda/ (inner); run commands from there.
- V source: vlambda/src/vlambda_native.v. Python package: vlambda/python/vlambda/.
- Generated (gitignored): src/_vcraft_generated.v, python/vlambda_native.pyi, .vcraft/, dist/, build/, *.so.

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
vlambda/pyproject.toml configures pyright, mypy, ty with python/ as extra import path. Run from vlambda/.

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
- Integration only: cd vlambda && . .venv/bin/activate && python ../tests/test_vlambda.py
- Harness self-starts server (port 9799, DB /tmp/test_vlambda.db, max body 100).
- No pytest suite. Set VLAMBDA_BASE if project root not auto-detected.
- To run one case, call start_server() then test function manually, or trim test list.

## CI
- .github/workflows/ci.yml runs in manylinux container (preinstalled V/vcraft) for Python 3.14 and 3.15: create venv -> vcraft develop -> integration tests.

## Docker
- `Dockerfile` installs vlambda from PyPI; `Dockerfile.dev` builds the local wheel.
- Both use uv to create a free-threaded venv and include gofly + nsjail.
- Reset: `python -m vlambda reset --yes --url http://127.0.0.1:8081 --token $VLAMBDA_ADMIN_TOKEN`.
- See `docker/README.md`.

## Quirks
- `VCRAFT_NO_MEMORY_LIMIT=1` for constrained builds.
- Metrics are persisted to a separate SQLite DB (`VLAMBDA_METRICS_DB`, default `<db>_metrics.db`); retention `VLAMBDA_METRICS_RETENTION` days.
- SQLite runs in WAL mode; sidecar files are .db, -wal, -shm.
- Regenerate ignored artifacts after changing V or Python code.
