#!/usr/bin/env bash
# End-to-end checks against the RUNNING rig (docker compose deployment).
#
#   scripts/e2e_live.sh [pytest args]          e.g. scripts/e2e_live.sh -k frames
#   scripts/e2e_live.sh --browser              the dashboard in Chromium (Playwright image, screenshots)
#
# 1. Docker: the container exists, is running and healthy, restarts on reboot, keeps its data volume.
# 2. tests/e2e (inside the rig's own image, host network): health, encoders, upload/download, metadata,
#    analytics sidebar, streams over RTSP (frames, delays, first frames, viewers, stop/start), 3 streams
#    at once incl. live transcoding and a 4:3 clip made 16:9, browser preview, LAN + Tailscale addresses,
#    events, capacity, page speed. Creates only zz-e2e test items and removes them afterwards.
#
# Env: RIG02_E2E_URL (default http://127.0.0.1:${RIG02_HTTP_PORT:-8081}), RIG02_E2E_USER/PASS when login
# is on, RIG02_E2E_REPORT (default reports/e2e-<time>.json).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PORT="${RIG02_HTTP_PORT:-$(sed -n 's/^RIG02_HTTP_PORT=//p' .env 2>/dev/null | tail -1)}"
PORT="${PORT:-8081}"
URL="${RIG02_E2E_URL:-http://127.0.0.1:$PORT}"
NAME=rig02
IMAGE=rig02-rtsp-manager:latest
PLAYWRIGHT_IMAGE=mcr.microsoft.com/playwright/python:v1.49.1-noble
mkdir -p reports
REPORT="${RIG02_E2E_REPORT:-reports/e2e-$(date +%Y%m%d-%H%M%S).json}"
fail=0
check() { if eval "$2"; then echo "  PASS  $1"; else echo "  FAIL  $1"; fail=1; fi; }

echo "== Docker"
st=$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} {{.HostConfig.RestartPolicy.Name}}' "$NAME" 2>/dev/null || echo "missing")
echo "  container: $st"
check "container '$NAME' exists and is running"  '[[ $st == running* ]]'
check "container health check is healthy"         '[[ $st == *healthy* && $st != *unhealthy* ]]'
check "restarts automatically (reboot/crash)"     '[[ $st == *always || $st == *unless-stopped ]]'
check "state + videos on the named volume"        'docker inspect -f "{{range .Mounts}}{{.Name}} {{end}}" $NAME | grep -q rig02_rig02-data'
check "dashboard port $PORT answers"              'curl -fsS -m 5 "$URL/api/health" >/dev/null'

if [[ "${1:-}" == "--browser" ]]; then
  shift
  echo "== Browser tests ($URL): Chromium at phone/tablet/laptop/desktop size"
  mkdir -p reports/ui
  docker run --rm --network host --ipc host --user "$(id -u):$(id -g)" -e HOME=/tmp \
    -e RIG02_E2E_URL="$URL" -e RIG02_E2E_SHOTS=/out/ui -e RIG02_E2E_UI_REPORT="/out/ui/ui-$(date +%Y%m%d-%H%M%S).json" \
    -v "$ROOT:/src:ro" -v "$ROOT/reports:/out" -w /src "$PLAYWRIGHT_IMAGE" sh -c \
    'pip install -q --user --break-system-packages --disable-pip-version-check playwright==1.49.1 pytest==8.3.4 \
       pytest-timeout==2.3.1 2>&1 | grep -v -i warning;
     python -m pytest -p no:cacheprovider --noconftest -rs -o addopts="" -c /dev/null --rootdir /src -v --timeout 300 tests/e2e/test_browser_ui.py "$@"' sh "$@" || fail=1
  echo "== screenshots: reports/ui/"
  exit $fail
fi

echo "== End-to-end tests ($URL)"
docker run --rm --network host --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -e RIG02_E2E_URL="$URL" -e RIG02_E2E_USER="${RIG02_E2E_USER:-}" -e RIG02_E2E_PASS="${RIG02_E2E_PASS:-}" \
  -e RIG02_E2E_REPORT="/out/$(basename "$REPORT")" \
  -v "$ROOT:/src:ro" -v "$ROOT/reports:/out" -w /src --entrypoint sh "$IMAGE" -c \
  'pip install -q --user --disable-pip-version-check pytest==8.3.4 pytest-timeout==2.3.1 2>/dev/null;
   python -m pytest -p no:cacheprovider -o addopts="" -v --timeout 600 tests/e2e --ignore tests/e2e/test_browser_ui.py "$@"' sh "$@" || fail=1
echo "== measurements: $REPORT"
exit $fail
