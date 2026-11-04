#!/usr/bin/env bash
# Nightly backup of RIG-02 state (run by rtsp-manager-backup.timer, or by hand):
# the SQLite database (online, consistent copy), users file, env file and
# MediaMTX config. Videos are user data and are NOT included.
#   sudo /opt/rtsp-manager/scripts/backup.sh [/var/backups/rtsp-manager] [retention_days]
set -euo pipefail
DEST_ROOT="${1:-/var/backups/rtsp-manager}"
KEEP_DAYS="${2:-14}"
DATA="${RTSPM_DATA_DIR:-/var/lib/rtsp-manager}"
PY="${RTSPM_PYTHON:-/opt/rtsp-manager/backend/.venv/bin/python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3
STAMP=$(date +%Y%m%d-%H%M%S)
DEST="$DEST_ROOT/$STAMP"
install -d -m 0700 "$DEST"
# sqlite3 online backup API: consistent even while the backend is writing
"$PY" - "$DATA/rtsp-manager.db" "$DEST/rtsp-manager.db" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
for f in "$DATA/users" /etc/rtsp-manager/rtsp-manager.env /etc/mediamtx/mediamtx.yml; do
  [[ -f "$f" ]] && cp -p "$f" "$DEST/"
done
chmod -R go-rwx "$DEST"
find "$DEST_ROOT" -mindepth 1 -maxdepth 1 -type d -mtime "+$KEEP_DAYS" -exec rm -rf {} +
echo "backup written to $DEST ($(du -sh "$DEST" | cut -f1)); keeping $KEEP_DAYS days"
