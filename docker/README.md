# vlambda deployment

Two images are provided:

- `Dockerfile`: installs `vlambda` from PyPI (production).
- `Dockerfile.dev`: builds a wheel from this checkout with vcraft (development).

Both use a free-threaded Python 3.14 venv created with uv and expose plain HTTP
on port `8080`. TLS and the public reverse proxy are handled by Dokploy or the
platform in front of the container.

## Build

PyPI image:

```bash
docker build --build-arg VLAMBDA_VERSION=0.2.1 -t vlambda:latest .
```

Local development image:

```bash
docker build -f Dockerfile.dev -t vlambda:dev .
```

## Run

Production (Dokploy or another reverse proxy forwards HTTP to port 8080):

```bash
export VLAMBDA_SECRET="$(openssl rand -hex 32)"
export VLAMBDA_ADMIN_TOKEN="$(openssl rand -hex 32)"
docker compose -f docker/compose.yml up -d --build
```

Development (HTTP on :8080):

```bash
docker compose -f docker/compose.dev.yml up --build
```

With Dokploy, create an application from the image, point the domain to the
container port `8080`, and set `VLAMBDA_SECRET` and `VLAMBDA_ADMIN_TOKEN` in the
environment. Dokploy handles TLS certificates.

Public registration is open by default for toy deployments. To close it, set
`VLAMBDA_REGISTRATION=0` and create users with the admin token:

```bash
curl -X POST https://your-domain/admin/users \
  -H "X-Admin-Token: $VLAMBDA_ADMIN_TOKEN" \
  -d "alice:change-me"
```

A daily reset removes users and lambdas, but it does not make arbitrary code
execution safe. Keep only disposable data and secrets in this deployment.

## Reset

Run the reset command manually or from cron. For a toy deployment, daily is a
common choice:

```bash
docker exec -e VLAMBDA_ADMIN_TOKEN="$VLAMBDA_ADMIN_TOKEN" <container> \
  /opt/venv/bin/python -m vlambda reset --yes \
  --url http://127.0.0.1:8080
```

Or add it to the host crontab:

```cron
0 3 * * * docker exec -e VLAMBDA_ADMIN_TOKEN="..." <container> \
  /opt/venv/bin/python -m vlambda reset --yes \
  --url http://127.0.0.1:8080
```

The command calls the running server's `/admin/reset` endpoint, which wipes users,
lambdas, projects, packages, metrics, versions, aliases and caches. If the server
is not reachable, it falls back to deleting the data directly.

## Verified

- `GET /health` and `/ready` on port `8080`.
- Lambda invocations run in isolated runner processes.
- Infinite-loop lambda killed after the configured timeout; the next request succeeds.
- Memory-hog lambda returns `MemoryError`; the next request succeeds.
- Online reset via `/admin/reset` empties lambdas and users.
- PyPI package installation into the per-lambda vendor directory.

## Limits

- Docker: 512 MB RAM, 1 CPU, 256 PIDs.
- vlambda: 4 workers, 400 MB soft limit, 5 s timeout, 20 req/s per IP.
- Max lambdas per user: `VLAMBDA_MAX_LAMBDAS` (default 20).
- Lambdas run in separate runner processes; the container remains the outer
  security and resource boundary.

## Security

This image is intended for a disposable toy deployment. Registration is open by
default, and runner processes no longer inherit `VLAMBDA_*` configuration or
secrets. Code lambdas and projects still share the container filesystem and
network, so a daily reset only removes data; it does not prevent code execution
during the day. Keep disposable secrets and no valuable data in this
deployment, or add an OS-level sandbox (nsjail, gVisor, Firecracker or one
container per tenant).
