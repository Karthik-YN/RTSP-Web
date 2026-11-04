#!/usr/bin/env bash
# Validate one or more RTSP URLs from any machine with ffprobe (e.g. a laptop
# on the office Wi-Fi). Checks: reachable, H.264 video only (no audio),
# resolution/fps, real-time pacing (wall clock vs media time), timestamp
# continuity (no duplicates, no gaps).
#
#   scripts/validate_rtsp.sh [-t SECONDS] rtsp://rig02:8554/cam1 [rtsp://...]
#
# This is NOT proof of 5.x edge compatibility (see docs/EDGE_VALIDATION.md).
set -uo pipefail
SECS=15
if [[ "${1:-}" == "-t" ]]; then SECS="$2"; shift 2; fi
[[ $# -ge 1 ]] || { sed -n '2,10p' "$0"; exit 2; }
CHECK_PY=$(cat <<'PY'
import json, sys
t0, t1 = float(sys.argv[1]), float(sys.argv[2])
d = json.load(sys.stdin)
streams = d.get("streams", [])
ts = sorted(float(p["pts_time"]) for p in d.get("packets", []) if p.get("pts_time") not in (None, "N/A"))
ok = True
kinds = [s["codec_type"] for s in streams]
v = next((s for s in streams if s["codec_type"] == "video"), {})
print(f"  tracks: {kinds}  video: {v.get('codec_name')} {v.get('profile')} {v.get('width')}x{v.get('height')} {v.get('pix_fmt')} {v.get('avg_frame_rate')}")
if kinds != ["video"]:
    print("  FAIL: expected exactly one video track (audio must be dropped)"); ok = False
if v.get("codec_name") != "h264":
    print("  FAIL: not H.264"); ok = False
if len(ts) < 2:
    print("  FAIL: no packets"); sys.exit(1)
span = ts[-1] - ts[0]
steps = [b - a for a, b in zip(ts, ts[1:])]
dups = len(ts) - len({round(t, 5) for t in ts})
ratio = (t1 - t0) / span if span else 0
print(f"  media {span:.2f}s in wall {t1 - t0:.2f}s (ratio {ratio:.3f}, includes connect time); "
      f"{len(ts)} frames ({len(ts) / span:.2f} fps); max gap {max(steps) * 1000:.0f} ms; duplicate pts {dups}")
if not 0.95 <= ratio <= 1.15:
    print("  FAIL: not real time"); ok = False
if dups or max(steps) > 0.5:
    print("  FAIL: timestamp discontinuity"); ok = False
print("  PASS" if ok else "  => FAILED")
sys.exit(0 if ok else 1)
PY
)
RC=0
for URL in "$@"; do
  echo "== $URL ($SECS s)"
  T0=$(python3 -c 'import time; print(time.time())')
  ERR=$(mktemp)
  OUT=$(ffprobe -v error -rtsp_transport tcp -timeout 10000000 -read_intervals "%+$SECS" \
        -show_entries stream=codec_type,codec_name,profile,width,height,pix_fmt,avg_frame_rate:packet=pts_time \
        -of json "$URL" 2>"$ERR")
  ST=$?
  T1=$(python3 -c 'import time; print(time.time())')
  if [[ $ST -ne 0 ]]; then echo "  FAIL: $(tail -2 "$ERR" | tr '\n' ' ')"; rm -f "$ERR"; RC=1; continue; fi
  rm -f "$ERR"
  python3 -c "$CHECK_PY" "$T0" "$T1" <<<"$OUT" || RC=1
done
exit $RC
