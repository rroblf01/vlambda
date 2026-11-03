#!/bin/sh
set -eu
umask 077

: "${VLAMBDA_SECRET:?VLAMBDA_SECRET is required}"
: "${VLAMBDA_ADMIN_TOKEN:?VLAMBDA_ADMIN_TOKEN is required}"

mkdir -p /data/db /data/projects /data/packages /data/metrics /data/tmp /data/cache

exec /opt/venv/bin/python -m vlambda
