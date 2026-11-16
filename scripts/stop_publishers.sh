#!/usr/bin/env bash
# Stop every RIG-02 publisher (runner + FFmpeg) gracefully, e.g. before
# maintenance. Desired state is NOT changed: the backend republishes the
# streams that should be running when it starts (or restart rtsp-manager).
#   sudo -u rtspmgr /opt/rtsp-manager/scripts/stop_publishers.sh
set -uo pipefail
RUN_DIR="${RTSPM_RUN_DIR:-${RTSPM_DATA_DIR:-/var/lib/rtsp-manager}/run}/streams"
PIDS=$(pgrep -u "$(id -u)" -f "app.services.publisher_runner --state $RUN_DIR" || true)
[[ -z "$PIDS" ]] && { echo "no publishers running"; exit 0; }
echo "stopping publishers: $PIDS"
kill -TERM $PIDS
for _ in $(seq 10); do sleep 1; pgrep -f "app.services.publisher_runner --state $RUN_DIR" >/dev/null || { echo done; exit 0; }; done
echo "forcing remaining publishers"; pkill -KILL -f "$RUN_DIR" || true
