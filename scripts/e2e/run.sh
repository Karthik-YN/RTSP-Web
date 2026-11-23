#!/usr/bin/env bash
# RTSP Studio end-to-end simulation: portal (browser) + edges (RTSP) + frames + clock.
#   scripts/e2e/run.sh http://192.168.29.105:8081 readonly   # live: reads only, creates nothing
#   scripts/e2e/run.sh http://192.168.29.105:8082 full       # preview: also creates/controls/deletes e2e-* streams
# Results: scripts/e2e/results/<time>-{portal,edge}.json ; exit 1 if anything FAILED.
set -u
BASE=${1:?base url}; MODE=${2:-readonly}; SECS=${SECS:-25}; EXTRA=${EXTRA:-1}
HERE=$(cd "$(dirname "$0")" && pwd); OUT="$HERE/results"; mkdir -p "$OUT"; chmod 777 "$OUT"
TS=$(date +%Y%m%d-%H%M%S)
IMG=$(docker inspect rig02 --format '{{.Config.Image}}')
docker run --rm --network host -v "$HERE":/e2e -w /e2e mcr.microsoft.com/playwright/python:v1.49.1-noble \
  bash -c "pip -q install playwright==1.49.1 >/dev/null 2>&1; python portal_suite.py --base $BASE --mode $MODE --json results/$TS-portal.json"
P=$?
# --entrypoint: the rig image would otherwise start the app itself on the host ports
docker run --rm --network host --entrypoint python3 -v "$HERE":/e2e -w /e2e "$IMG" \
  edge_suite.py --base "$BASE" --mode "$MODE" --secs "$SECS" --extra "$EXTRA" --json "results/$TS-edge.json"
E=$?
echo "portal exit $P, edge exit $E -> $OUT/$TS-*.json"
[ $P -eq 0 ] && [ $E -eq 0 ]
