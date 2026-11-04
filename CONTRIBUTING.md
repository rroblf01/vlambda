# Contributing

## Requirements

- Python 3.14 or newer (3.15 and free-threaded builds are supported).
- The V compiler pinned by `vcraft` 1.7.2.
- Linux for the sandbox tests. The in-process development mode is more portable.

## Development setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install vcraft==1.7.2
VCRAFT_NO_MEMORY_LIMIT=1 vcraft develop
```

`vcraft develop` regenerates the native extension, the Python stubs and the
editable wheel. Set `VCRAFT_BIN` to use another vcraft binary.

## Layout

- `vlambda/src/` — V sources, including the native HTTP server.
- `vlambda/python/vlambda/` — Python package (server, sandbox, CLI, UI assets).
- `vlambda/python/vlambda/ui.html` — UI source.
- `vlambda/ui/` — editor/Chart.js bundle source; build with `npm run build` and
  regenerate `python/vlambda/static/app.js`.
- `tests/test_unit.py` — unit tests.
- `tests/test_vlambda.py` — integration harness (starts a real server).
- `docs/` — API, CLI, compatibility and upgrade references.

Run build commands from `vlambda/`, not from the repository root.

## Tests

```bash
# Unit
python -m pytest tests/test_unit.py -q

# Integration (self-starts a server on port 9799)
cd vlambda
python ../tests/test_vlambda.py
```

`VLAMBDA_PYTHON` overrides the interpreter used by the integration harness.
The harness uses a unique database and log per case.

For the release matrix, run the suite under the pinned manylinux image for
cp314, cp315, cp314t and cp315t. `test_packages` needs network access.

## Performance and soak

`bench.py` creates a temporary lambda and measures throughput. Set
`VLAMBDA_MIN_RPS` to turn it into a pass/fail gate:

```bash
VLAMBDA_MIN_RPS=50 python bench.py
```

`scripts/soak.py` runs sustained load and reports errors, p95 latency and,
optionally, RSS growth for a local server PID:

```bash
python scripts/soak.py --url http://127.0.0.1:8080 --token "$VLAMBDA_TOKEN" \
  --slug <slug> --duration 3600 --concurrency 8 --max-errors 0 \
  --pid "$(pgrep -f 'python -m vlambda' | head -1)" --max-rss-growth-mb 32
```

## Style

- Match the surrounding style; keep changes focused.
- Commits in English, without co-author trailers.
- Update `CHANGELOG.md` for user-visible changes.
- Regenerate `python/vlambda/assets.py` with `python scripts/embed_assets.py`
  after changing `ui.html`, `app.js`, `favicon.ico` or the docs asset.
- Do not commit build artifacts (`dist/`, `build/`, `.vcraft/`, `*.so`).

## Release process

1. Bump `version` in `pyproject.toml`, `vcraft.toml` and
   `python/vlambda/version.py` together. A unit test enforces the match.
2. Update `CHANGELOG.md`.
3. Run unit, integration and the four-image CI matrix.
4. Merge `main`, create the tag and publish the GitHub Release.
5. Wait for `release.yml` to publish wheels and sdist to PyPI.
6. Verify the wheel installs and contains the expected files.
7. Only then update the `deploy` branch. Never deploy a version that is not on
   PyPI: the production Dockerfile installs `vlambda==<version>` from PyPI.
8. The `image` workflow publishes `ghcr.io/rroblf01/vlambda:<version>` after
   PyPI is ready.

## Security

Do not open a public issue for a suspected vulnerability. See
[SECURITY.md](SECURITY.md).
