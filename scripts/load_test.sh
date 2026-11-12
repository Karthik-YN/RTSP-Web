#!/usr/bin/env bash
# Capacity / load testing.
#
#   scripts/load_test.sh isolated
#       Runs tests/performance/test_capacity.py: 1/4/8/16 copy streams with
#       0-2 RTSP readers each, plus 1/2/4 transcoded streams, against a
#       private MediaMTX on random ports (does NOT touch the running
#       services). Run it ON THE PROXMOX VM to measure the VM. Writes
#       docs/reports/performance-<host>-<date>.json.
#
#   scripts/load_test.sh live --url http://rig02:8080 --video-id 3 --streams 16 --readers 1 --minutes 30
#       Load test of the real deployment through its API (see scripts/load_test.py).
#
#   scripts/load_test.sh live --url http://rig02:8080 --video-id 3 --ramp 10,20,40,60,80,100 --readers 2 --apply
#       Capacity ramp: finds where streams degrade and saves 80% of that as the limit.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-}"; shift || true
case "$MODE" in
  isolated)
    cd "$ROOT"
    "$ROOT/backend/.venv/bin/pip" install -q -r backend/requirements.txt
    exec "$ROOT/backend/.venv/bin/python" -m pytest -m performance -s tests/performance/test_capacity.py "$@" ;;
  live)
    exec python3 "$ROOT/scripts/load_test.py" "$@" ;;
  *) sed -n '2,19p' "$0"; exit 2 ;;
esac
