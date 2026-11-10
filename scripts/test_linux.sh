#!/usr/bin/env bash
# Linux validation in Docker (no Proxmox needed):
#   1. backend test suite on Debian 12 (FFmpeg 5.1) and Ubuntu 24.04 (FFmpeg 6.1)
#   2. scripts/install.sh inside a systemd container, then health check + a real stream
#
#   scripts/test_linux.sh [tests|install|tls|launcher|spike|all]     (default: all)
# Requires Docker. Test fixtures are generated on the host first.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
WHAT="${1:-all}"
[[ -f tests/.fixtures/long_2h.mp4 ]] || scripts/generate_test_videos.sh tests/.fixtures
[[ -f frontend/dist/index.html ]] || { echo "build the frontend first: (cd frontend && npm ci && npm run build)"; exit 1; }

run_tests() {
  local base="$1" tag="$2"
  echo "=== backend tests on $base"
  docker build -q -f deployment/test/Dockerfile.tests --build-arg BASE="$base" -t "rig02-test:$tag" . >/dev/null
  docker run --rm "rig02-test:$tag"
}

run_install() {
  echo "=== install.sh in a systemd container (Debian 12)"
  docker build -q -f deployment/test/Dockerfile.systemd -t rig02-systemd . >/dev/null
  docker rm -f rig02-sd >/dev/null 2>&1 || true
  docker run -d --name rig02-sd --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw rig02-systemd >/dev/null
  [[ -n "${KEEP:-}" ]] || trap "docker rm -f rig02-sd >/dev/null 2>&1 || true" EXIT
  for _ in $(seq 30); do docker exec rig02-sd systemctl is-system-running 2>/dev/null | grep -qE "running|degraded" && break; sleep 1; done
  docker exec rig02-sd bash -c "cd /src && scripts/install.sh --host rig02.test" | tee /tmp/rig02-install.log
  docker exec rig02-sd cp /src/tests/.fixtures/h264_2997.mp4 /data/rtsp-manager/videos/
  docker exec rig02-sd chown rtspmgr:rtspmgr /data/rtsp-manager/videos/h264_2997.mp4
  docker exec rig02-sd bash -c '
    set -e
    A=""; H=http://127.0.0.1:8080
    curl -fs $A -X POST $H/api/videos/refresh >/dev/null   # first scan sees a fresh copy: "settling"
    sleep 11
    curl -fs $A -X POST $H/api/videos/refresh >/dev/null
    VID=$(curl -fs $A $H/api/videos | python3 -c "import json,sys; print(json.load(sys.stdin)[0][\"id\"])")
    curl -fs $A -H "Content-Type: application/json" -X POST "$H/api/streams?wait=true" \
      -d "{\"name\":\"linux_check\",\"video_id\":$VID,\"start\":\"00:00:02\",\"end\":\"00:00:10\"}" | python3 -c "import json,sys; s=json.load(sys.stdin); print(\"stream:\", s[\"name\"], s[\"status\"]); assert s[\"status\"]==\"RUNNING\""
    /opt/rtsp-manager/scripts/healthcheck.sh --rtsp
    echo "--- backend restart: publisher keeps running and is re-adopted"
    PID1=$(curl -fs $A $H/api/streams | python3 -c "import json,sys; print(json.load(sys.stdin)[0][\"pid\"])")
    systemctl restart rtsp-manager; sleep 6
    curl -fs $A $H/api/streams | PID1=$PID1 python3 -c "import json,os,sys; s=json.load(sys.stdin)[0]; print(\"after restart:\", s[\"status\"], \"pid\", s[\"pid\"], \"(before\", os.environ[\"PID1\"] + \")\"); assert s[\"status\"]==\"RUNNING\" and str(s[\"pid\"])==os.environ[\"PID1\"]"
    echo "--- MediaMTX kill -> republish"
    systemctl kill --kill-whom=main -s SIGKILL mediamtx; sleep 10
    curl -fs $A $H/api/streams | python3 -c "import json,sys; s=json.load(sys.stdin)[0]; print(\"after mediamtx kill:\", s[\"status\"]); assert s[\"status\"]==\"RUNNING\""
    echo "--- lead-in: MediaMTX (its own user) plays the slate from /var/lib/rtsp-manager/slates"
    stat -c "%a %U" /var/lib/rtsp-manager /var/lib/rtsp-manager/slates
    SID=$(curl -fs $A -H "Content-Type: application/json" -X POST "$H/api/streams?wait=true"       -d "{\"name\":\"lead_check\",\"video_id\":$VID,\"lead_in_mode\":\"hold\",\"lead_in_s\":2}" | python3 -c "import json,sys; s=json.load(sys.stdin); print(s[\"id\"]); assert s[\"status\"]==\"WAITING_FOR_VIEWER\", s[\"status\"]")
    ls -l /var/lib/rtsp-manager/slates/
    ffprobe -v error -rtsp_transport tcp -select_streams v:0 -show_entries stream=codec_name,width,height -of csv=p=0 rtsp://127.0.0.1:8554/lead_check
    sleep 5
    curl -fs $A $H/api/streams/$SID | python3 -c "import json,sys; s=json.load(sys.stdin); print(\"after a viewer connected:\", s[\"status\"]); assert s[\"status\"] in (\"LEAD_IN\", \"RUNNING\")"
    curl -fs $A -X DELETE $H/api/streams/$SID >/dev/null
    echo "--- polkit: backend may stop/start mediamtx"
    curl -fs $A -X POST $H/api/mediamtx/restart | python3 -c "import json,sys; print(json.load(sys.stdin)[\"message\"])"
    echo "--- nightly backup timer + manual backup"
    systemctl is-enabled rtsp-manager-backup.timer
    /opt/rtsp-manager/scripts/backup.sh /var/backups/rtsp-manager 14
    ls /var/backups/rtsp-manager/*/rtsp-manager.db >/dev/null
    python3 -c "import sqlite3,glob; c=sqlite3.connect(sorted(glob.glob(\"/var/backups/rtsp-manager/*/rtsp-manager.db\"))[-1]); print(\"backup has\", c.execute(\"select count(*) from streams\").fetchone()[0], \"stream(s)\")"
    echo INSTALL-TEST-PASSED'
}

run_install_tls() {
  echo "=== install.sh --with-nginx --tls self-signed (Debian 12, systemd)"
  docker build -q -f deployment/test/Dockerfile.systemd -t rig02-systemd . >/dev/null
  docker rm -f rig02-tls >/dev/null 2>&1 || true
  docker run -d --name rig02-tls --privileged --cgroupns=host -v /sys/fs/cgroup:/sys/fs/cgroup:rw rig02-systemd >/dev/null
  trap 'docker rm -f rig02-tls >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 30); do docker exec rig02-tls systemctl is-system-running 2>/dev/null | grep -qE "running|degraded" && break; sleep 1; done
  docker exec rig02-tls bash -c "cd /src && scripts/install.sh --host rig02.test --with-nginx --tls self-signed" > /tmp/rig02-tls.log 2>&1
  grep -E "Dashboard|self-signed|WARN" /tmp/rig02-tls.log
  docker exec rig02-tls bash -c '
    set -e
    curl -fsk https://127.0.0.1/api/health; echo
    echo "redirect: $(curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}" http://127.0.0.1/)"
    curl -s -m 3 http://$(hostname -i | cut -d" " -f1):8080/api/health && { echo "FAIL: backend reachable directly"; exit 1; } || echo "backend :8080 not reachable from the network (loopback only)"
    curl -fsk https://127.0.0.1/api/streams >/dev/null && echo "direct access works, no login"
    cp /src/tests/.fixtures/h264_2997.mp4 /data/rtsp-manager/videos/ && chown rtspmgr:rtspmgr /data/rtsp-manager/videos/h264_2997.mp4
    H=https://127.0.0.1
    curl -fsk -X POST $H/api/videos/refresh >/dev/null; sleep 11; curl -fsk -X POST $H/api/videos/refresh >/dev/null
    VID=$(curl -fsk $H/api/videos | python3 -c "import json,sys; print(json.load(sys.stdin)[0][\"id\"])")
    curl -fsk -H "Content-Type: application/json" -X POST "$H/api/streams?wait=true" -d "{\"name\":\"tls_check\",\"video_id\":$VID}" >/dev/null
    M=$(curl -fsk $H/api/streams/1/live/index.m3u8); echo "$M" | head -1
    V=$(echo "$M" | grep -v "^#" | grep . | head -1)
    curl -fsk "$H/api/streams/1/live/$V" | grep -q EXTINF && echo "live preview works over HTTPS through nginx"
    echo TLS-INSTALL-PASSED'
}

run_launcher() {
  echo "=== python3 app.py on a clean Debian 12 machine (only Python + FFmpeg installed)"
  docker build -q -f deployment/test/Dockerfile.launcher -t rig02-launcher . >/dev/null
  docker rm -f rig02-la >/dev/null 2>&1 || true
  docker run -d --name rig02-la rig02-launcher >/dev/null
  trap 'docker rm -f rig02-la >/dev/null 2>&1 || true' EXIT
  echo "--- terminal 1: first run"
  docker exec rig02-la python3 app.py | tee /tmp/rig02-la.log
  echo "--- terminal 2 (terminal 1 is closed): a colleague runs it"
  docker exec rig02-la python3 app.py
  docker exec rig02-la python3 app.py status
  docker exec rig02-la bash -c '
    set -e
    A=""; H=http://127.0.0.1:8080
    F=tests/.fixtures/h264_2997.mp4; SIZE=$(stat -c%s $F)
    echo "--- upload through the API"
    curl -fs $A -H "Content-Type: application/json" -X POST $H/api/videos/upload/check -d "{\"filename\":\"clip.mp4\",\"size\":$SIZE}" >/dev/null
    VID=$(curl -fs $A -H "Content-Type: application/octet-stream" --data-binary @$F "$H/api/videos/upload?filename=clip.mp4" | python3 -c "import json,sys; v=json.load(sys.stdin); print(v[\"id\"])")
    echo "uploaded video $VID"
    curl -fs $A -H "Content-Type: application/json" -X POST "$H/api/streams?wait=true" \
      -d "{\"name\":\"cam1\",\"video_id\":$VID,\"start\":\"00:00:02\",\"end\":\"00:00:10\"}" | python3 -c "import json,sys; s=json.load(sys.stdin); print(\"stream:\", s[\"name\"], s[\"status\"], s[\"rtsp_url\"]); assert s[\"status\"]==\"RUNNING\""
    echo "--- read the RTSP stream"
    ffprobe -v error -rtsp_transport tcp -read_intervals %+3 -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 rtsp://127.0.0.1:8554/cam1 | wc -l
    echo "--- live preview"
    curl -fs -L $A $H/api/streams/1/live/index.m3u8 | head -1
    curl -fs $A $H/ | grep -c "<div id=\"root\">"
  '
  echo "--- restart: the stream comes back"
  docker exec rig02-la python3 app.py restart | tail -3
  sleep 4
  docker exec rig02-la bash -c 'curl -fs http://127.0.0.1:8080/api/streams | python3 -c "import json,sys; s=json.load(sys.stdin)[0]; print(\"after restart:\", s[\"name\"], s[\"status\"]); assert s[\"status\"]==\"RUNNING\""'
  docker exec rig02-la python3 app.py stop
  docker exec rig02-la python3 app.py status || echo "(status correctly reports not running)"
  echo LAUNCHER-TEST-PASSED
}

run_spike() {
  echo "=== lead-in spike on Debian 12 (FFmpeg 5.1, GStreamer RTSP readers)"
  docker build -q -f deployment/test/Dockerfile.tests --build-arg BASE=debian:12 -t rig02-test:debian12 . >/dev/null
  docker build -q -f deployment/test/Dockerfile.spike -t rig02-spike . >/dev/null
  mkdir -p docs/reports/spike_leadin
  docker run --rm -v "$ROOT/docs/reports/spike_leadin:/out" rig02-spike
}

case "$WHAT" in
  tests) run_tests debian:12 debian12; run_tests ubuntu:24.04 ubuntu2404 ;;
  install) run_install ;;
  tls) run_install_tls ;;
  launcher) run_launcher ;;
  spike) run_spike ;;
  all) run_tests debian:12 debian12; run_tests ubuntu:24.04 ubuntu2404; run_install; run_install_tls; run_launcher ;;
  *) echo "usage: $0 [tests|install|tls|launcher|spike|all]"; exit 2 ;;
esac
