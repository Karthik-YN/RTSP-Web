# RIG-02 RTSP Stream Manager: one portable image (dashboard + API + FFmpeg + MediaMTX).
#   docker compose up -d --build          (see docker-compose.yml and docs/DEPLOYMENT.md)
# The backend starts and supervises MediaMTX itself (process mode), so a single
# container is the whole rig. State lives in the /data volume.

# ---- 1. build the dashboard ------------------------------------------------
FROM node:20-bookworm-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. runtime --------------------------------------------------------------
FROM python:3.12-slim-bookworm
ARG MEDIAMTX_VERSION=v1.21.2
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends ffmpeg fonts-dejavu-core curl ca-certificates tini >/dev/null \
 && rm -rf /var/lib/apt/lists/*
# MediaMTX: pinned release, verified against the release's own checksum file.
RUN set -eu; arch=$(dpkg --print-architecture); \
    case "$arch" in amd64|arm64) ;; *) echo "unsupported arch $arch"; exit 1;; esac; \
    base="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}"; \
    tar="mediamtx_${MEDIAMTX_VERSION}_linux_${arch}.tar.gz"; \
    cd /tmp && curl -fsSL -O "$base/$tar" && curl -fsSL -O "$base/checksums.sha256" \
 && grep -E " \*?$tar\$" checksums.sha256 | sha256sum -c - \
 && tar -xzf "$tar" -C /usr/local/bin mediamtx && rm -f "$tar" checksums.sha256

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN grep -v -i '^pytest' backend/requirements.txt > /tmp/req.txt && pip install -q -r /tmp/req.txt && rm /tmp/req.txt
COPY backend/app backend/app
COPY deployment/mediamtx.yml deployment/mediamtx.yml
COPY deployment/docker/entrypoint.sh /usr/local/bin/rig02-entrypoint
# capacity ramp, runnable inside the container:  docker compose exec rig02 rig02-loadtest --help
COPY scripts/load_test.py /app/scripts/load_test.py
COPY --from=web /web/dist frontend/dist

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin rig02 \
 && mkdir -p /data/state /data/videos && chown -R rig02:rig02 /data \
 && chmod 755 /usr/local/bin/rig02-entrypoint \
 && printf '#!/bin/sh\nexec python /app/scripts/load_test.py --url "http://127.0.0.1:${PORT:-8080}" --rtsp-base rtsp://127.0.0.1:8554 "$@"\n' > /usr/local/bin/rig02-loadtest \
 && chmod 755 /usr/local/bin/rig02-loadtest

ENV RTSPM_ENV_FILE=/dev/null \
    RTSPM_DATA_DIR=/data/state \
    RTSPM_VIDEO_DIR=/data/videos \
    RTSPM_FRONTEND_DIST=/app/frontend/dist \
    RTSPM_MEDIAMTX_CONTROL=process \
    RTSPM_MEDIAMTX_CONFIG=/app/deployment/mediamtx.yml \
    RTSPM_MEDIAMTX_BIN=/usr/local/bin/mediamtx \
    RIG02_REPORT_DIR=/data/state/reports \
    PORT=8080
USER rig02
VOLUME ["/data"]
EXPOSE 8080 8554
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=4 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health?strict=true" >/dev/null || exit 1
STOPSIGNAL SIGTERM
ENTRYPOINT ["tini", "--", "rig02-entrypoint"]
