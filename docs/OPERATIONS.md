# Operations

## Startup

`python -m vlambda` or the container entrypoint performs these checks before
serving:

- the main and metrics SQLite schemas exist and are not newer than the build;
- the project, package and cache directories are writable;
- when `VLAMBDA_SANDBOX=fork`/`landlock` is selected, Landlock and
  `libseccomp` are available.

A failure exits with a non-zero status and a clear message instead of running
unsandboxed.

## Health and readiness

- `GET /health` is always available and includes the running version.
- `GET /ready` returns `503` while draining or before the executor is ready.
- `GET /metrics` is the public Prometheus surface.
- Owner-scoped metrics/logs live under `/metrics/*` and `/logs/history`.

The Docker healthcheck uses `/health`.

## Backups

Create an archive while the server is running:

```bash
python -m vlambda backup --out /data/backups/vlambda-$(date +%F).tar.gz
```

The archive contains a consistent copy of the main database, the metrics/logs
database, the project tree and, with `--with-packages`, the package vendors.
Restores are offline; stop the server first:

```bash
python -m vlambda restore --from /data/backups/vlambda-2026-11-04.tar.gz --force
```

The restore verifies `PRAGMA integrity_check`, schema versions and the
encryption-key fingerprint. Keep the same `VLAMBDA_SECRET` or
`VLAMBDA_ENCRYPTION_KEY`, or pass `--allow-key-mismatch` if encrypted secrets
are not needed.

Test a restore into a disposable directory or host regularly. A backup that has
never been restored is not a backup.

## Upgrades and rollback

1. Back up.
2. Replace the package/image with the new version.
3. Start the server and check `/health` for the expected version.
4. Run a smoke invocation.
5. If startup fails, restore the backup. Do not downgrade the database in place.

Migrations are forward-only. The newer schema is refused by older builds.

## Secret rotation

Run offline, with the server stopped:

```bash
python -m vlambda backup --out before-rotate.tar.gz
python -m vlambda secrets rotate --old-key "$OLD" --new-key "$NEW"
```

Restart with the new `VLAMBDA_ENCRYPTION_KEY`. Rotating `VLAMBDA_SECRET`
invalidates existing JWTs and API tokens.

## Soak and performance

`bench.py` creates a temporary lambda and reports throughput:

```bash
VLAMBDA_MIN_RPS=50 python bench.py
```

`scripts/soak.py` runs sustained traffic and reports errors, p95 and RSS growth:

```bash
python scripts/soak.py --url http://127.0.0.1:8080 --token "$VLAMBDA_TOKEN" \
  --slug <slug> --duration 3600 --concurrency 8 --max-errors 0 \
  --pid "$(pgrep -f 'python -m vlambda' | head -1)" --max-rss-growth-mb 64
```

A 5-minute local run (631k invocations, concurrency 4) finished with 0 errors,
~2.1k req/s, p95 2.95 ms and ~12 MB of RSS growth.

## Chaos checklist

Run these against a staging host before a major release:

- lock the main database with a write transaction; management calls should fail
  cleanly and recover when the lock is released;
- stop the capability broker; KV/egress calls should raise a normal Python
  exception and healthy lambdas should keep working;
- fill the data volume; writes should fail closed without corrupting SQLite;
- kill a runner child; the next invocation should work;
- expire a scheduler lease; exactly one replica should run the schedule;
- restore a backup over a wiped volume and invoke a lambda.

## Capacity

- SQLite stays on a local filesystem. Do not put it on NFS.
- The Docker image limits the container to 512 MB, 1 CPU and 256 PIDs in the
  compose examples; vlambda keeps 4 workers, a 5 s timeout and a 20 req/s
  per-IP rate limit by default.
- Raise `VLAMBDA_COROUTINES` for in-process throughput and keep the fork
  sandbox for untrusted code.
- Metrics/logs retention is `VLAMBDA_METRICS_RETENTION` days (default 7).

## Troubleshooting

- `Failed to initialize database`: the database is from a pre-1.0 schema or a
  newer build. Use `vlambda reset --yes --wipe` or restore a matching backup.
- `Startup check failed`: a data directory is not writable or Landlock/seccomp
  is unavailable; fix the mount or choose a sandbox mode explicitly.
- 401 after a password change or reset: JWTs are invalidated by an auth epoch
  change; log in again.
- 413: raise `VLAMBDA_MAX_BODY` or send a smaller body.
- 429: the per-IP rate limiter tripped; raise `VLAMBDA_RATE_LIMIT` or back off.
