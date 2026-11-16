#!/usr/bin/env python3
"""Client-side recovery timer for EC-09 / EC-10. Run it on a LAPTOP.

  python3 scripts/measure_recovery.py urls.txt [--max 600] [--interval 2]

urls.txt holds one rtsp:// URL per line (e.g. the 16 cameras configured on
the edge). Start the script, wait for "all N streams up", THEN reboot the
server (or kill MediaMTX). Every URL is probed in parallel every --interval
seconds with ffprobe (one frame over RTSP/TCP). For each stream it records
when it went down and when frames came back, and writes recovery-<time>.csv.

For EC-10 report "seconds after boot" = up_time - server boot time. Get the
boot time on the server with `uptime -s` (or `journalctl --list-boots`), or
read /api/system/recovery which records it. The edge's own camera status
(green) must still be recorded by hand (docs/EDGE_VALIDATION.md).
"""

from __future__ import annotations

import argparse
import csv
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime


def probe(url: str, timeout_s: float = 4.0) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(int(timeout_s * 1e6)),
             "-read_intervals", "%+#1", "-select_streams", "v:0", "-show_entries", "packet=pts_time",
             "-of", "csv=p=0", url],
            capture_output=True, text=True, timeout=timeout_s + 4)
        return r.returncode == 0 and r.stdout.strip() != ""
    except subprocess.TimeoutExpired:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls")
    ap.add_argument("--max", type=float, default=600, help="give up after this many seconds")
    ap.add_argument("--interval", type=float, default=2.0)
    a = ap.parse_args()
    urls = [ln.strip() for ln in open(a.urls) if ln.strip().startswith("rtsp://")]
    if not urls:
        print("no rtsp:// URLs found")
        return 2
    down: dict[str, float] = {}
    up: dict[str, float] = {}
    first_down = None
    start = time.time()
    with ThreadPoolExecutor(max_workers=min(32, len(urls))) as pool:
        ok = dict(zip(urls, pool.map(probe, urls)))
        print(f"{datetime.now():%H:%M:%S} baseline: {sum(ok.values())}/{len(urls)} streams up. "
              "Reboot the server / kill MediaMTX now.")
        while True:
            t = time.time()
            for u, alive in zip(urls, pool.map(probe, urls)):
                if not alive and u not in down:
                    down[u] = t
                    first_down = first_down or t
                    print(f"{datetime.now():%H:%M:%S} DOWN {u}")
                elif alive and u in down and u not in up:
                    up[u] = t
                    print(f"{datetime.now():%H:%M:%S} UP   {u}  (+{t - first_down:.1f}s after first outage)")
            if first_down and len(up) == len(urls):
                break
            if t - start > a.max:
                print(f"timeout after {a.max:.0f}s")
                break
            time.sleep(max(0.0, a.interval - (time.time() - t)))
    out = f"recovery-{datetime.now():%Y%m%d-%H%M%S}.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["url", "down_at", "up_at", "seconds_after_first_outage"])
        for u in urls:
            w.writerow([u, datetime.fromtimestamp(down[u]).isoformat() if u in down else "",
                        datetime.fromtimestamp(up[u]).isoformat() if u in up else "",
                        f"{up[u] - first_down:.1f}" if u in up and first_down else ""])
    print(f"{len(up)}/{len(urls)} streams recovered; last at "
          f"{max(up.values()) - first_down:.1f}s after the first outage" if up and first_down else "no outage seen")
    print(f"written {out}")
    return 0 if first_down and len(up) == len(urls) else 1


if __name__ == "__main__":
    raise SystemExit(main())
