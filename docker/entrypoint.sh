#!/bin/bash
set -euo pipefail

: "${VLAMBDA_SECRET:?VLAMBDA_SECRET is required}"
: "${VLAMBDA_ADMIN_TOKEN:?VLAMBDA_ADMIN_TOKEN is required}"

mkdir -p /data/db /data/projects /data/packages /data/metrics /data/tmp /data/cache

export VLAMBDA_DB="${VLAMBDA_DB:-/data/db/vlambda.db}"
export VLAMBDA_METRICS_DB="${VLAMBDA_METRICS_DB:-/data/metrics/metrics.db}"
export VLAMBDA_PROJECTS_DIR="${VLAMBDA_PROJECTS_DIR:-/data/projects}"
export VLAMBDA_PACKAGES_DIR="${VLAMBDA_PACKAGES_DIR:-/data/packages}"
export VLAMBDA_CACHE_DIR="${VLAMBDA_CACHE_DIR:-/data/cache}"
export VLAMBDA_ISOLATE="${VLAMBDA_ISOLATE:-1}"
export VLAMBDA_NSJAIL="${VLAMBDA_NSJAIL:-1}"
export VLAMBDA_ADMIN_TOKEN

/opt/venv/bin/python -m vlambda \
  --port 8081 \
  --sqlite_path "$VLAMBDA_DB" \
  --num_coroutines "${VLAMBDA_COROUTINES:-4}" \
  --max_body_size "${VLAMBDA_MAX_BODY:-1048576}" \
  --rate_limit "${VLAMBDA_RATE_LIMIT:-20}" \
  --invoke_timeout "${VLAMBDA_TIMEOUT:-5}" \
  --max_memory_mb "${VLAMBDA_MAX_MEMORY_MB:-400}" &
VLAMBDA_PID=$!

# Wait for the upstream before gofly starts so it does not mark it unhealthy.
for _ in $(seq 1 100); do
  if /opt/venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health', timeout=1)" 2>/dev/null; then
    break
  fi
  sleep 0.1
done

gofly -config "${GOFLY_CONFIG:-/etc/gofly/config.json}" &
GOFLY_PID=$!

trap 'kill -TERM "$VLAMBDA_PID" "$GOFLY_PID" 2>/dev/null || true' TERM INT

wait -n "$VLAMBDA_PID" "$GOFLY_PID" || true
kill -TERM "$VLAMBDA_PID" "$GOFLY_PID" 2>/dev/null || true
wait || true
