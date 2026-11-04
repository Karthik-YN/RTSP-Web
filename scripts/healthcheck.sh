#!/usr/bin/env bash
# Server-side health check for RIG-02. Run on the server (any user).
#
#   scripts/healthcheck.sh [--rtsp]     # --rtsp also reads 2 s from every RUNNING stream
#
# Exit code 0 = healthy, 1 = problems found.
set -uo pipefail
API="${RTSPM_URL:-http://127.0.0.1:${RTSPM_LISTEN_PORT:-8080}}"
MTX_API="${RTSPM_MEDIAMTX_API_URL:-http://127.0.0.1:9997}"
RTSP_LOCAL="${RTSPM_PUBLISH_RTSP_BASE:-rtsp://127.0.0.1:8554}"
CHECK_RTSP=0; [[ "${1:-}" == "--rtsp" ]] && CHECK_RTSP=1
FAIL=0
ok()   { printf '  \033[32mOK\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=1; }
info() { printf '  ..   %s\n' "$*"; }

echo "RIG-02 health check"
if command -v systemctl >/dev/null; then
  for u in mediamtx.service rtsp-manager.service; do
    if systemctl is-active --quiet "$u"; then ok "$u active"; else bad "$u is $(systemctl is-active "$u")"; fi
    systemctl is-enabled --quiet "$u" 2>/dev/null && ok "$u enabled at boot" || bad "$u not enabled at boot"
  done
fi

if curl -fs "$MTX_API/v3/paths/list" >/dev/null; then ok "MediaMTX Control API answers on $MTX_API"; else bad "MediaMTX Control API not reachable on $MTX_API"; fi

H=$(curl -fs "$API/api/health" 2>/dev/null) || { bad "backend not reachable on $API"; exit 1; }
echo "$H" | grep -q '"status":"ok"' && ok "backend health: $H" || bad "backend health: $H"

if command -v ss >/dev/null; then
  EXPOSED=$(ss -Hltn 2>/dev/null | awk '{print $4}' | grep -vE '^(127\.0\.0\.1|\[::1\]):' | sed -E 's/.*:([0-9]+)$/\1/' | sort -un | tr '\n' ' ')
  info "TCP ports listening on non-loopback addresses: $EXPOSED"
  for p in 9997 9998 9999 1935 8888 8889 8890 8892; do
    echo " $EXPOSED " | grep -q " $p " && bad "port $p is exposed to the network (should be loopback-only or disabled)"
  done
  echo " $EXPOSED " | grep -q " 8554 " && ok "RTSP 8554 listening" || bad "RTSP 8554 not listening"
fi

if command -v ffprobe >/dev/null && [[ $CHECK_RTSP -eq 1 ]]; then
  AUTH=(); [[ -n "${RTSPM_USER:-}" ]] && AUTH=(-u "$RTSPM_USER:${RTSPM_PASSWORD:-}")
  NAMES=$(curl -fs "${AUTH[@]}" "$API/api/streams" | python3 -c 'import json,sys; print(" ".join(s["name"] for s in json.load(sys.stdin) if s["status"]=="RUNNING"))' 2>/dev/null) \
    || { bad "cannot list streams (set RTSPM_USER / RTSPM_PASSWORD)"; NAMES=""; }
  for n in $NAMES; do
    c=$(ffprobe -v error -rtsp_transport tcp -read_intervals %+2 -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 "$RTSP_LOCAL/$n" 2>/dev/null | wc -l)
    [[ "$c" -gt 10 ]] && ok "rtsp $n: $c packets in 2 s" || bad "rtsp $n: no media"
  done
fi
exit $FAIL
