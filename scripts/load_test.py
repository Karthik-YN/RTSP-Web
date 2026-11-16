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

Capacity ramp (sets the server's stream limit from a measurement):

  python3 scripts/load_test.py --url http://rig02:8080 --video-id 3 \
      --ramp 10,20,40,60,80,100 --readers 2 --step-minutes 3 [--apply]

Adds streams step by step (camera starts stay 2 s apart), holds each level for
--step-minutes with --readers readers per stream, and stops at the first level
that degrades: a stream not RUNNING, publisher speed below 0.97x, dropped
frames, restarts, dead readers, or host CPU above 90%. The recommended limit
is 80% of the last good level; --apply saves it on the server
(Health page -> Capacity limits). Results: loadtest-ramp-<time>.json.
Run the readers from ANOTHER machine when possible: readers on the server
itself cost CPU the edge would not.
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
    ap.add_argument("--ramp", help="comma list of stream counts, e.g. 10,20,40,60,80,100")
    ap.add_argument("--step-minutes", type=float, default=3)
    ap.add_argument("--apply", action="store_true", help="save the recommended limit on the server")
    ap.add_argument("--rtsp-base", help="read streams from this base instead of their public URL, "
                    "e.g. rtsp://127.0.0.1:8554 when running on the server or inside the container")
    ap.add_argument("--out-dir", default=os.environ.get("RIG02_REPORT_DIR", "."),
                    help="where the CSV/JSON results are written (default: current folder)")
    a = ap.parse_args()
    api = Api(a.url, os.environ.get("RTSPM_USER"), os.environ.get("RTSPM_PASSWORD"))
    if a.ramp:
        return ramp(api, a)
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
                                                 "-i", reader_url(a, s["rtsp_url"]), "-c", "copy", "-f", "null", "-"],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        os.makedirs(a.out_dir, exist_ok=True)
        out = os.path.join(a.out_dir, f"loadtest-{datetime.now():%Y%m%d-%H%M%S}.csv")
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


DEGRADED_SPEED = 0.97


def reader_url(a, url: str) -> str:
    if not a.rtsp_base:
        return url
    return a.rtsp_base.rstrip("/") + "/" + url.rsplit("/", 1)[-1]


def _measure(api: Api, ids: list[int], readers: list, seconds: float) -> dict:
    """Sample every 5 s for `seconds`; return the worst values seen."""
    rows = []
    st0 = {s["id"]: s for s in api.call("GET", "/api/streams") if s["id"] in ids}
    end = time.time() + seconds
    while time.time() < end:
        time.sleep(5)
        sysinfo = api.call("GET", "/api/system")
        st = [s for s in api.call("GET", "/api/streams") if s["id"] in ids]
        speeds = [((s["runtime"] or {}).get("progress") or {}).get("speed") for s in st]
        speeds = [x for x in speeds if x]
        rows.append({
            "cpu": sysinfo["cpu_percent"], "mem": sysinfo["memory"]["percent"],
            "net_tx_mbit": round(sysinfo.get("net_sent_bps", 0) / 1e6, 1),
            "ffmpeg_rss_mb": round(sysinfo["ffmpeg_rss_bytes"] / 1e6),
            "running": sum(s["status"] in ("RUNNING", "LEAD_IN") for s in st),
            "speed_min": min(speeds) if speeds else None,
            "viewers": sysinfo["viewers"],
            "restarts": sum(s["restart_count"] - st0.get(s["id"], s)["restart_count"] for s in st),
            "dropped": sum(((s["runtime"] or {}).get("progress") or {}).get("drop_frames", 0) for s in st),
            "readers_dead": sum(p.poll() is not None for p in readers),
        })
    speeds = [r["speed_min"] for r in rows if r["speed_min"] is not None]
    return {
        "cpu_avg": round(sum(r["cpu"] for r in rows) / len(rows), 1), "cpu_max": max(r["cpu"] for r in rows),
        "mem_max": max(r["mem"] for r in rows), "net_tx_mbit_avg": round(sum(r["net_tx_mbit"] for r in rows) / len(rows), 1),
        "ffmpeg_rss_mb": rows[-1]["ffmpeg_rss_mb"], "running_min": min(r["running"] for r in rows),
        "speed_min": min(speeds) if speeds else None, "viewers": rows[-1]["viewers"],
        "restarts": rows[-1]["restarts"], "dropped": rows[-1]["dropped"], "readers_dead": rows[-1]["readers_dead"],
    }


def _problems(level: int, m: dict) -> list[str]:
    out = []
    if m["running_min"] < level:
        out.append(f"only {m['running_min']}/{level} streams running")
    if m["speed_min"] is not None and m["speed_min"] < DEGRADED_SPEED:
        out.append(f"a publisher fell behind real time ({m['speed_min']:.2f}x)")
    if m["restarts"]:
        out.append(f"{m['restarts']} publisher restart(s)")
    if m["dropped"]:
        out.append(f"{m['dropped']} dropped frame(s)")
    if m["readers_dead"]:
        out.append(f"{m['readers_dead']} reader(s) disconnected")
    if m["cpu_max"] > 90:
        out.append(f"host CPU {m['cpu_max']:.0f}%")
    return out


def ramp(api: Api, a) -> int:
    levels = sorted({int(x) for x in a.ramp.split(",") if x.strip()})
    video = api.call("GET", f"/api/videos/{a.video_id}")
    dur = video["duration_s"] or 60
    cap0 = api.call("GET", "/api/settings/capacity")
    old = cap0["max_active_streams"]
    if old["value"] < levels[-1]:
        print(f"raising the server's stream limit {old['value']} -> {levels[-1]} for the test")
        api.call("PUT", "/api/settings/capacity", {"max_active_streams": levels[-1]})
    readers: list[subprocess.Popen] = []
    ids: list[int] = []
    results = []
    last_good = 0
    stop_reason = None

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
    try:
        for level in levels:
            t0 = time.time()
            new = []
            for n in range(len(ids), level):
                start = (n * 37) % max(1, int(dur * 0.8))
                s = api.call("POST", "/api/streams", {"name": f"loadtest_{n:03d}", "video_id": a.video_id, "start": start,
                                                      "mode": "loop", "force_transcode": a.transcode,
                                                      "lead_in_mode": "none"})
                ids.append(s["id"])
                new.append(s["id"])
            while True:
                mine = [s for s in api.call("GET", "/api/streams") if s["id"] in ids]
                if sum(s["status"] == "RUNNING" for s in mine) == len(ids) or time.time() - t0 > 60 + 3 * len(new):
                    break
                time.sleep(2)
            startup = round(time.time() - t0, 1)
            urls = {s["id"]: s["rtsp_url"] for s in api.call("GET", "/api/streams") if s["id"] in new}
            for sid in new:
                for _ in range(a.readers):
                    readers.append(subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp",
                                                     "-i", reader_url(a, urls[sid]), "-c", "copy", "-f", "null", "-"],
                                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            print(f"level {level}: {len(ids)} streams started in {startup}s, {len(readers)} readers; measuring "
                  f"{a.step_minutes} min")
            m = _measure(api, ids, readers, a.step_minutes * 60)
            probs = _problems(level, m)
            results.append({"streams": level, "readers": len(readers), "startup_s": startup, **m, "problems": probs})
            print(json.dumps(results[-1]))
            if probs:
                stop_reason = f"{level} streams: " + "; ".join(probs)
                break
            last_good = level
    finally:
        cleanup()
    rec = max(1, int(last_good * 0.8)) if last_good else None
    bitrate = video.get("bitrate_bps") or 0
    report = {
        "url": a.url, "video": video["filename"], "video_bitrate_mbit": round(bitrate / 1e6, 2),
        "readers_per_stream": a.readers, "transcode": a.transcode, "step_minutes": a.step_minutes,
        "levels": results, "last_good_level": last_good, "stopped_because": stop_reason,
        "recommended_max_active_streams": rec,
        "network_note": (f"Each stream sends ~{bitrate / 1e6:.1f} Mbit/s per reader: {rec or 0} streams x "
                         f"{a.readers} readers = ~{(rec or 0) * a.readers * bitrate / 1e6:.0f} Mbit/s out") if bitrate else None,
        "finished_at": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(a.out_dir, exist_ok=True)
    out = os.path.join(a.out_dir, f"loadtest-ramp-{datetime.now():%Y%m%d-%H%M%S}.json")
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nlast good level: {last_good} streams; stopped because: {stop_reason or 'all levels passed'}")
    if rec:
        print(f"recommended limit: {rec} streams (80% of {last_good})")
        if report["network_note"]:
            print(report["network_note"])
    if a.apply and rec:
        api.call("PUT", "/api/settings/capacity", {"max_active_streams": rec})
        print(f"saved on the server: max_active_streams = {rec}")
    elif old["value"] < levels[-1]:
        api.call("PUT", "/api/settings/capacity",
                 {"max_active_streams": old["value"]} if old["overridden"] else {"reset": ["max_active_streams"]})
        print(f"stream limit restored to {old['value']}")
    print(f"written {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
