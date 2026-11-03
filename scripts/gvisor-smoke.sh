#!/bin/sh
# Smoke-test the Docker image under gVisor. Skips when runsc is unavailable.
set -eu

if ! command -v runsc >/dev/null 2>&1; then
    echo "gvisor: runsc is not installed; skipping"
    exit 0
fi

if ! docker info --format '{{json .Runtimes}}' 2>/dev/null | grep -q runsc; then
    echo "gvisor: runsc is installed but not registered with Docker; skipping"
    exit 0
fi

IMAGE="${VLAMBDA_GVISOR_IMAGE:-vlambda:gvisor-test}"
NAME="${VLAMBDA_GVISOR_NAME:-vlambda-gvisor-test}"
PORT="${VLAMBDA_GVISOR_PORT:-8099}"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

cleanup() {
    docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker build -f "$ROOT/Dockerfile.dev" -t "$IMAGE" "$ROOT"
docker run -d --name "$NAME" --runtime=runsc -p "$PORT:8080" \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --tmpfs /tmp:size=64m \
    -e VLAMBDA_SECRET=test-secret -e VLAMBDA_ADMIN_TOKEN=test-admin \
    "$IMAGE" >/dev/null

for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        echo "gvisor: smoke test passed"
        exit 0
    fi
    sleep 1
done

echo "gvisor: smoke test failed" >&2
docker logs "$NAME" >&2 || true
exit 1
