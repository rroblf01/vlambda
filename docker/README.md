# vlambda deployment

Two images are provided:

- `Dockerfile`: installs `vlambda` from PyPI (production).
- `Dockerfile.dev`: builds a wheel from this checkout with vcraft (development).

Both use a free-threaded Python 3.15 venv created with uv, a pre-warmed fork
supervisor and a fresh Landlock + seccomp child per invocation, and expose plain
HTTP on port `8080`. TLS and the public reverse proxy are handled by Dokploy or
the platform in front of the container.

## Official image

Images are published from the PyPI wheel and attested by the release workflow:

```bash
docker pull ghcr.io/rroblf01/vlambda:1.0.0
```

Pin the image by digest in production so a rebuild cannot change what you run.
The GitHub Actions `image` workflow waits for the version to appear on PyPI,
then publishes `linux/amd64` and `linux/arm64` images tagged with the version
and `latest`.

## Build

PyPI image:

```bash
docker build --build-arg VLAMBDA_VERSION=1.0.0 -t vlambda:latest .
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

The sandbox runs unprivileged inside the container and needs no special Docker
flags or host sysctl changes. It requires a Linux kernel with Landlock (5.13+;
Ubuntu 22.04+, Debian 12+). If Landlock is unavailable, invocations fail closed
with a `sandbox:` error instead of running unsandboxed.

Public registration is closed by default in the production image. Create
users with the admin token:

```bash
curl -X POST https://your-domain/admin/users \
  -H "X-Admin-Token: $VLAMBDA_ADMIN_TOKEN" \
  -d "alice:change-me"
```

A daily reset removes users and lambdas. Keep only disposable data and secrets
in this deployment, and rotate `VLAMBDA_SECRET` and `VLAMBDA_ADMIN_TOKEN` if you
want a compromise to expire with the reset.

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
lambdas, projects, packages, metrics, versions, aliases and caches. It also
invalidates existing JWT sessions and API tokens. If the server is not reachable,
it falls back to deleting the data directly.

## Verified

- `GET /health` and `/ready` on port `8080`.
- Code and project lambdas run inside Landlock + seccomp.
- Project code cannot read or list `/data`, cannot read `/proc/1/environ`, cannot signal
  other processes and cannot open network connections.
- The sandbox can only read `/opt`, `/usr`, `/lib`, `/etc`, the current
  project/vendor and one private temporary directory; it gets no `VLAMBDA_*`
  environment variables.
- Infinite-loop lambda killed at the time limit; the next request succeeds.
- Online reset via `/admin/reset` empties lambdas and users.
- PyPI package installation into the per-lambda vendor directory.

## Limits

- Docker: 512 MB RAM, 1 CPU, 256 PIDs.
- vlambda: 4 workers, 400 MB soft limit, 5 s timeout, 20 req/s per IP.
- Max lambdas per user: `VLAMBDA_MAX_LAMBDAS` (default 20).
- Lambdas run in per-invocation Landlock + seccomp processes; the container
  remains the outer security and resource boundary. Expect a small cold start
  per invocation (fresh Python process).

## gVisor

Use the gVisor runtime for an outer isolation layer:

```bash
docker compose -f docker/compose.yml -f docker/compose.gvisor.yml up -d
# or: docker run --runtime=runsc ...
```

`scripts/gvisor-smoke.sh` skips when `runsc` is not installed/registered. The
internal per-invocation fork + Landlock + seccomp sandbox is unchanged.

## Single-host HA

`docker/compose.ha.yml` runs two replicas behind nginx with a shared local
volume. Run it with `docker compose -f docker/compose.ha.yml up -d --build
--scale vlambda=2`. The scheduler uses a database lease, async tasks are claimed
with a lease, project GC only runs on the leader and `/shutdown` drains in-flight
work. SQLite must stay on a local volume, not NFS. Multi-host HA requires an
external database and is deferred.

## Security

Each invocation runs in a fresh process confined with Landlock and seccomp. The
filesystem is limited to the Python runtime, the current project/vendor and a
private temporary directory; `/data`, the server's `/proc`, TCP/UDP networking
and cross-process signals are blocked. Only `AF_UNIX` client sockets to the
capability broker are allowed, and each invocation gets a scoped token. The
server and database stay outside the sandbox.

This is suitable for a disposable toy deployment, but it
is not a hypervisor. Keep the host and image updated, keep only disposable
secrets, and use gVisor, Firecracker or one container per tenant if you need a
stronger boundary.
