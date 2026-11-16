#!/bin/sh
# Container entrypoint: check the volume, then run the backend (which runs MediaMTX).
set -eu
for d in "${RTSPM_DATA_DIR:-/data/state}" "${RTSPM_VIDEO_DIR:-/data/videos}"; do
  mkdir -p "$d" 2>/dev/null || true
  if [ ! -w "$d" ]; then
    echo "rig02: $d is not writable by uid $(id -u). Fix the volume's owner:" >&2
    echo "  sudo chown -R 10001:10001 <host folder>    (or run the container with --user)" >&2
    exit 1
  fi
done
case "${RTSPM_PUBLIC_RTSP_HOST:-localhost}" in
  localhost|127.0.0.1) echo "rig02: RTSPM_PUBLIC_RTSP_HOST is not set; RTSP URLs will show 'localhost'. Set it in .env." >&2 ;;
esac
cd /app/backend
exec python -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8080}" --workers 1
