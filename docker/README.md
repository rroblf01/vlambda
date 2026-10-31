# vlambda deployment

Two images are provided:

- `Dockerfile`: installs `vlambda` from PyPI (production).
- `Dockerfile.dev`: builds a wheel from this checkout with vcraft (development).

Both include gofly, nsjail, a free-threaded Python 3.14 venv created with uv, and
the weekly reset CLI. TLS is terminated by the platform (Dokploy), so the
container only exposes plain HTTP on port 8080.

## Build

PyPI image:

```bash
docker build --build-arg VLAMBDA_VERSION=0.2.0 -t vlambda:latest .
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

## Weekly reset

Run the reset command manually every Monday:

```bash
docker exec -e VLAMBDA_ADMIN_TOKEN="$VLAMBDA_ADMIN_TOKEN" <container> \
  /opt/venv/bin/python -m vlambda reset --yes \
  --url http://127.0.0.1:8081
```

Or add it to the host crontab:

```cron
0 0 * * 1 docker exec -e VLAMBDA_ADMIN_TOKEN="..." <container> \
  /opt/venv/bin/python -m vlambda reset --yes \
  --url http://127.0.0.1:8081
```

The command calls the running server's `/admin/reset` endpoint, which wipes users,
lambdas, projects, packages, metrics, versions, aliases and caches. If the server
is not reachable, it falls back to deleting the data directly.

## Verified

The development image was built and run with nsjail enabled:

- `GET /health` and `/ready` through gofly.
- lambda invocation inside nsjail.
- infinite-loop lambda killed after the configured timeout; the next request succeeds.
- memory-hog lambda returns `MemoryError`; the next request succeeds.
- online reset via `/admin/reset` empties lambdas and users.
- PyPI package installation into the per-lambda vendor directory.

## Limits

- Docker: 512 MB RAM, 1 CPU, 256 PIDs.
- vlambda: 4 workers, 400 MB soft limit, 5 s timeout, 20 req/s per IP.
- Max lambdas per user: `VLAMBDA_MAX_LAMBDAS` (default 20).
- nsjail per worker: 80 MB address space, 5 s CPU, no network.
- If Docker blocks user namespaces, set `VLAMBDA_NSJAIL=0` and rely on RLIMITs.
