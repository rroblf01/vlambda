# syntax=docker/dockerfile:1.7
# Production image: installs vlambda from PyPI.
ARG VLAMBDA_VERSION=0.2.0
ARG PYTHON_VERSION=3.14t

FROM debian:bookworm-slim AS python-env
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python
ENV UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl && rm -rf /var/lib/apt/lists/*
RUN uv python install ${PYTHON_VERSION}
RUN uv venv /opt/venv --python ${PYTHON_VERSION}
ARG VLAMBDA_VERSION
RUN uv pip install --python /opt/venv/bin/python --no-cache \
    "vlambda==${VLAMBDA_VERSION}"
RUN find /opt/venv -name '__pycache__' -type d -prune -exec rm -rf {} + && \
    find /opt/venv -name '*.pyc' -delete

FROM debian:bookworm-slim AS nsjail
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential bison flex libprotobuf-dev protobuf-compiler \
    libnl-3-dev libnl-genl-3-dev libcap-dev pkg-config git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/google/nsjail /src/nsjail
RUN make -C /src/nsjail -j"$(nproc)"

FROM ghcr.io/rroblf01/gofly:latest AS gofly

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tini libprotobuf32 libnl-3-200 libnl-genl-3-200 \
    libnl-route-3-200 libcap2 libsqlite3-0 libsqlite3-dev libssl3 libffi8 libbz2-1.0 \
    liblzma5 zlib1g \
    && rm -rf /var/lib/apt/lists/*
COPY --from=gofly /gofly /usr/local/bin/gofly
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
COPY --from=nsjail /src/nsjail/nsjail /usr/local/bin/nsjail
COPY --from=python-env /opt/python /opt/python
COPY --from=python-env /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
ENV LD_LIBRARY_PATH=/opt/python/lib
ENV VLAMBDA_PYTHON=/opt/venv/bin/python
COPY docker/gofly.json /etc/gofly/config.json
COPY docker/gofly.dev.json /etc/gofly/gofly.dev.json
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && \
    useradd -r -u 10001 -m vlambda && \
    mkdir -p /data && chown -R vlambda:vlambda /data
USER vlambda
VOLUME ["/data"]
EXPOSE 8080 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/health')"]
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
