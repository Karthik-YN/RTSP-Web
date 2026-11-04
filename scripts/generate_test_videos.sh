#!/usr/bin/env bash
# Generate synthetic test videos with FFmpeg.
#
# Usage: scripts/generate_test_videos.sh <output-dir> [--large]
#
# Every clip uses the lavfi "testsrc" pattern, which burns a running
# timestamp into the picture: compare it against a stopwatch to check that
# playback runs at real time (EC-03). Existing files are not regenerated.
# --large additionally creates a ~4 GB, ~70 minute 1080p file (EC-13).
set -euo pipefail

OUT="${1:?usage: $0 <output-dir> [--large]}"
LARGE="${2:-}"
FFMPEG="${FFMPEG:-ffmpeg}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
cd "$OUT"

gen() {  # gen <file> <ffmpeg args...>
  local f="$1"; shift
  if [[ -s "$f" ]]; then return; fi
  echo "generating $f" >&2
  "$FFMPEG" -nostdin -hide_banner -loglevel error -y "$@" "$f.tmp.${f##*.}"
  mv "$f.tmp.${f##*.}" "$f"
}

SRC="testsrc=size=640x360"
X264=(-c:v libx264 -preset veryfast -pix_fmt yuv420p)

# H.264 25 fps, 1 s GOP, B-frames, with an AAC audio track (audio must be dropped)
gen h264_25fps_audio.mp4 -f lavfi -i "$SRC:rate=25" -f lavfi -i "sine=frequency=440:sample_rate=48000" \
  -t 20 "${X264[@]}" -g 25 -keyint_min 25 -sc_threshold 0 -c:a aac -b:a 64k -shortest
# Odd NTSC-style rates (EC-03), 2 s GOP with B-frames
gen h264_23976.mp4 -f lavfi -i "$SRC:rate=24000/1001" -t 20 "${X264[@]}" -g 48
gen h264_2997.mp4  -f lavfi -i "$SRC:rate=30000/1001" -t 20 "${X264[@]}" -g 60
gen h264_5994.mp4  -f lavfi -i "$SRC:rate=60000/1001" -t 20 "${X264[@]}" -g 120
# Variable frame rate: alternating 20 ms / 60 ms frame durations (mean 25 fps)
gen h264_vfr.mkv -f lavfi -i "$SRC:rate=25" -t 20 -vf "settb=1/1000,setpts=(N*0.04+mod(N\,2)*0.02)/TB" -enc_time_base 1/1000 \
  -fps_mode passthrough "${X264[@]}" -g 25
# Long GOP (8 s) to exercise keyframe snapping of start offsets
gen h264_longgop.mp4 -f lavfi -i "$SRC:rate=25" -t 40 "${X264[@]}" -g 200 -keyint_min 200 -sc_threshold 0
# Codecs the edge may not decode (EC-01)
gen hevc.mp4 -f lavfi -i "$SRC:rate=25" -t 10 -c:v libx265 -preset ultrafast -pix_fmt yuv420p \
  -x265-params log-level=error -tag:v hvc1
gen hevc_10bit.mkv -f lavfi -i "$SRC:rate=25" -t 10 -c:v libx265 -preset ultrafast -pix_fmt yuv420p10le \
  -x265-params log-level=error
gen vp9.webm -f lavfi -i "$SRC:rate=25" -t 10 -c:v libvpx-vp9 -deadline realtime -cpu-used 8 -b:v 500k -pix_fmt yuv420p
gen av1.mkv  -f lavfi -i "$SRC:rate=25" -t 10 -c:v libsvtav1 -preset 12 -pix_fmt yuv420p
gen h264_10bit.mp4 -f lavfi -i "$SRC:rate=25" -t 10 -c:v libx264 -preset veryfast -pix_fmt yuv420p10le -g 25
gen h264_422.mp4   -f lavfi -i "$SRC:rate=25" -t 10 -c:v libx264 -preset veryfast -pix_fmt yuv422p -g 25
# Not a video at all
if [[ ! -s corrupt.mp4 ]]; then head -c 200000 /dev/urandom > corrupt.mp4; fi

# Two-hour file built by stream-copy concatenation of a 60 s clip (EC-13)
if [[ ! -s long_2h.mp4 ]]; then
  gen _chunk60.mp4 -f lavfi -i "testsrc=size=320x180:rate=25" -t 60 -c:v libx264 -preset veryfast \
    -pix_fmt yuv420p -g 50 -b:v 150k
  printf "file '_chunk60.mp4'\n%.0s" $(seq 120) > _long.txt
  "$FFMPEG" -nostdin -hide_banner -loglevel error -y -f concat -safe 0 -i _long.txt -c copy long_2h.mp4.tmp.mp4
  mv long_2h.mp4.tmp.mp4 long_2h.mp4
  rm -f _long.txt
fi

if [[ "$LARGE" == "--large" && ! -s large_1080p.mp4 ]]; then
  gen _chunk1080.mp4 -f lavfi -i "testsrc=size=1920x1080:rate=25" -t 60 -c:v libx264 -preset ultrafast \
    -pix_fmt yuv420p -g 50 -b:v 8M -maxrate 8M -bufsize 16M
  printf "file '_chunk1080.mp4'\n%.0s" $(seq 70) > _large.txt
  "$FFMPEG" -nostdin -hide_banner -loglevel error -y -f concat -safe 0 -i _large.txt -c copy large_1080p.mp4.tmp.mp4
  mv large_1080p.mp4.tmp.mp4 large_1080p.mp4
  rm -f _large.txt
fi
ls -la "$OUT" >&2
