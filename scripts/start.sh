#!/usr/bin/env bash
# Run the backend locally for development (no systemd needed).
#
#   scripts/start.sh            # backend on :8080, serving frontend/dist if built
#
# Uses ./storage for videos/data and launches its own MediaMTX (process
# mode) with deployment/mediamtx.yml, unless RTSPM_MEDIAMTX_CONTROL is set.
# Authentication is disabled unless RTSPM_AUTH_ENABLED=true.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"

export RTSPM_ENV_FILE="${RTSPM_ENV_FILE:-/dev/null}"
export RTSPM_DATA_DIR="${RTSPM_DATA_DIR:-$ROOT/storage/data}"
export RTSPM_VIDEO_DIR="${RTSPM_VIDEO_DIR:-$ROOT/storage/videos}"
export RTSPM_LOG_DIR="${RTSPM_LOG_DIR:-$ROOT/storage/logs}"
export RTSPM_THUMBNAIL_DIR="${RTSPM_THUMBNAIL_DIR:-$ROOT/storage/thumbnails}"
export RTSPM_FRONTEND_DIST="${RTSPM_FRONTEND_DIST:-$ROOT/frontend/dist}"
export RTSPM_PUBLIC_RTSP_HOST="${RTSPM_PUBLIC_RTSP_HOST:-$(hostname)}"
export RTSPM_MEDIAMTX_CONTROL="${RTSPM_MEDIAMTX_CONTROL:-process}"
export RTSPM_MEDIAMTX_CONFIG="${RTSPM_MEDIAMTX_CONFIG:-$ROOT/deployment/mediamtx.yml}"
export RTSPM_AUTH_ENABLED="${RTSPM_AUTH_ENABLED:-false}"
export RTSPM_SETTLE_SECONDS="${RTSPM_SETTLE_SECONDS:-5}"

PY="$ROOT/backend/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  python3 -m venv "$ROOT/backend/.venv"
  "$ROOT/backend/.venv/bin/pip" install -q -r "$ROOT/backend/requirements.txt"
fi
exec "$PY" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" --workers 1
