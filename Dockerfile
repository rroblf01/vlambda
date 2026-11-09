# syntax=docker/dockerfile:1.7
# Production image: installs vlambda from PyPI and listens on port 8080.
# TLS and the public reverse proxy are handled by Dokploy.
# The release workflow overrides this with the version being published.
ARG VLAMBDA_VERSION=1.0.0
ARG PYTHON_VERSION=3.15t

FROM debian:bookworm-slim AS python-env
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python
ENV UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG VLAMBDA_VERSION
ARG PYTHON_VERSION
RUN uv python install ${PYTHON_VERSION} \
    && uv venv /opt/venv --python ${PYTHON_VERSION} \
    && uv pip install --python /opt/venv/bin/python --no-cache \
        "vlambda==${VLAMBDA_VERSION}" \
    && find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + \
    && find /opt/venv -name '*.pyc' -delete \
    && /opt/venv/bin/python -m compileall -q /opt/venv/lib

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini libsqlite3-dev libssl3 libffi8 \
    libbz2-1.0 liblzma5 zlib1g libseccomp2 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=python-env /opt/python /opt/python
COPY --from=python-env /opt/venv /opt/venv
COPY --from=python-env /bin/uv /usr/local/bin/uv
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/data/tmp \
    LD_LIBRARY_PATH=/opt/python/lib \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VLAMBDA_PORT=8080 \
    VLAMBDA_DB=/data/db/vlambda.db \
    VLAMBDA_METRICS_DB=/data/metrics/metrics.db \
    VLAMBDA_PROJECTS_DIR=/data/projects \
    VLAMBDA_PACKAGES_DIR=/data/packages \
    VLAMBDA_CACHE_DIR=/data/cache \
    UV_CACHE_DIR=/data/cache/uv \
    PIP_CACHE_DIR=/data/cache/pip \
    VLAMBDA_SANDBOX=fork \
    VLAMBDA_REGISTRATION=0 \
    VLAMBDA_COROUTINES=4 \
    VLAMBDA_MAX_BODY=1048576 \
    VLAMBDA_RATE_LIMIT=20 \
    VLAMBDA_TIMEOUT=5 \
    VLAMBDA_MAX_MEMORY_MB=400
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && useradd -r -u 10001 -m vlambda \
    && mkdir -p /data \
    && chown -R vlambda:vlambda /data
USER vlambda
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
