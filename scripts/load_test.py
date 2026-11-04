#!/usr/bin/env python3
"""Load test against a LIVE deployment through its API (Part 12 / EC-10 prep).

  RTSPM_USER=admin RTSPM_PASSWORD=... python3 scripts/load_test.py \
      --url http://rig02:8080 --video-id 3 --streams 16 --readers 1 --minutes 10

Creates streams named loadtest_00..NN from one video (different start
offsets), starts --readers RTSP readers per stream on THIS machine, samples
/api/system and /api/streams every 5 s, prints a summary and writes
loadtest-<time>.csv. All loadtest_* streams are deleted at the end (also on
Ctrl-C). Use --transcode to force H.264 transcoding (CPU test).
Readers need ffmpeg on the machine running this script.
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime


class Api:
    def __init__(self, base: str, user: str | None, pw: str | None):
        self.base = base.rstrip("/")
        self.auth = ("Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()) if user else None

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Content-Type", "application/json")
        if self.auth:
            req.add_header("Authorization", self.auth)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
                return json.loads(data) if data else None
        except urllib.error.HTTPError as e:
            raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--video-id", type=int, required=True)
    ap.add_argument("--streams", type=int, default=16)
    ap.add_argument("--readers", type=int, default=1, help="RTSP readers per stream on this machine")
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--transcode", action="store_true")
    a = ap.parse_args()
    api = Api(a.url, os.environ.get("RTSPM_USER"), os.environ.get("RTSPM_PASSWORD"))
    video = api.call("GET", f"/api/videos/{a.video_id}")
    dur = video["duration_s"] or 60
    readers: list[subprocess.Popen] = []
    ids: list[int] = []

    def cleanup(*_):
        for p in readers:
            p.terminate()
        for i in ids:
            try:
                api.call("DELETE", f"/api/streams/{i}")
            except SystemExit as e:
                print(e)
        print(f"cleaned up {len(ids)} streams")

    signal.signal(signal.SIGINT, lambda *x: (cleanup(), sys.exit(130)))
    t0 = time.time()
    try:
        for n in range(a.streams):
            start = (n * 37) % max(1, int(dur * 0.8))
            s = api.call("POST", "/api/streams", {"name": f"loadtest_{n:02d}", "video_id": a.video_id,
                                                  "start": start, "mode": "loop", "force_transcode": a.transcode})
            ids.append(s["id"])
        while True:
            st = api.call("GET", "/api/streams")
            mine = [s for s in st if s["id"] in ids]
            if all(s["status"] == "RUNNING" for s in mine):
                break
            if time.time() - t0 > 180:
                print("not all streams RUNNING after 180 s:", [(s["name"], s["status"]) for s in mine])
                break
            time.sleep(1)
        startup = time.time() - t0
        print(f"{len(ids)} streams RUNNING after {startup:.1f}s")
        for s in [s for s in api.call("GET", "/api/streams") if s["id"] in ids]:
            for _ in range(a.readers):
                readers.append(subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp",
                                                 "-i", s["rtsp_url"], "-c", "copy", "-f", "null", "-"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        out = f"loadtest-{datetime.now():%Y%m%d-%H%M%S}.csv"
        rows = []
        end = time.time() + a.minutes * 60
        while time.time() < end:
            sysinfo = api.call("GET", "/api/system")
            st = [s for s in api.call("GET", "/api/streams") if s["id"] in ids]
            row = {
                "t": round(time.time() - t0, 1), "cpu": sysinfo["cpu_percent"], "mem": sysinfo["memory"]["percent"],
                "ffmpeg_cpu": sysinfo["ffmpeg_cpu_percent"], "ffmpeg_rss_mb": round(sysinfo["ffmpeg_rss_bytes"] / 1e6),
                "net_tx_mbit": round(sysinfo.get("net_sent_bps", 0) / 1e6, 1),
                "running": sum(s["status"] == "RUNNING" for s in st), "viewers": sysinfo["viewers"],
                "restarts": sum(s["restart_count"] for s in st),
                "dropped": sum((s["runtime"]["progress"] or {}).get("drop_frames", 0) for s in st),
                "readers_dead": sum(p.poll() is not None for p in readers),
            }
            rows.append(row)
            print(json.dumps(row))
            time.sleep(5)
        with open(out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\nsummary: streams={a.streams} readers={len(readers)} transcode={a.transcode} startup={startup:.1f}s")
        for k in ("cpu", "mem", "ffmpeg_cpu", "ffmpeg_rss_mb", "net_tx_mbit"):
            vals = [r[k] for r in rows]
            print(f"  {k:14s} avg {sum(vals) / len(vals):8.1f}  max {max(vals):8.1f}")
        print(f"  restarts={rows[-1]['restarts']} dropped={rows[-1]['dropped']} readers_dead={rows[-1]['readers_dead']}"
              f" not_running={a.streams - rows[-1]['running']}")
        print(f"written {out}")
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
