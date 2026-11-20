#!/usr/bin/env python3
"""Rename library videos to the naming convention from the agreed CSV, through the rig's safe rename API.

    python3 scripts/apply_renames.py                      # dry run against http://127.0.0.1:8081 (changes nothing)
    python3 scripts/apply_renames.py --apply              # rename
    python3 scripts/apply_renames.py --url http://127.0.0.1:8082 --apply   # the preview rig

Reads docs/naming/video-renames-proposed.csv (columns video_id, current_name, proposed_name, needs_input).
Rows with needs_input=yes are skipped unless --include-needs-input. A row is also skipped when the video's
current name on the rig no longer matches current_name (someone renamed it meanwhile).

Streams using a renamed video keep running; their names and RTSP URLs do not change (see services/renamer.py).
Writes a report next to the CSV with the old and new name of every video, which also serves as the undo list
(swap the columns and run again).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def call(url: str, method: str = "GET", body: dict | None = None, auth: str | None = None):
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    if auth:
        import base64
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{method} {url}: HTTP {e.code} {e.read().decode(errors='replace')[:500]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://127.0.0.1:8081")
    ap.add_argument("--csv", default=str(REPO / "docs/naming/video-renames-proposed.csv"))
    ap.add_argument("--apply", action="store_true", help="rename (default: dry run)")
    ap.add_argument("--include-needs-input", action="store_true")
    ap.add_argument("--user", help="user:password when the rig has login turned on")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.csv, newline="")))
    current = {v["id"]: v["filename"] for v in call(f"{a.url}/api/videos?include_missing=true", auth=a.user)}
    items, skipped = [], []
    for r in rows:
        vid = int(r["video_id"])
        if r.get("needs_input") == "yes" and not a.include_needs_input:
            skipped.append((r["current_name"], "needs input"))
        elif current.get(vid) == r["proposed_name"]:
            skipped.append((r["current_name"], "already renamed"))
        elif current.get(vid) != r["current_name"]:
            skipped.append((r["current_name"], f"name on the rig is now {current.get(vid)!r}"))
        else:
            items.append({"video_id": vid, "name": r["proposed_name"]})

    print(f"{len(items)} to rename, {len(skipped)} skipped ({'APPLY' if a.apply else 'dry run'}) on {a.url}")
    for name, why in skipped:
        print(f"  skip  {name}  ({why})")
    if not items:
        return 0
    res = call(f"{a.url}/api/videos/rename-batch", "POST", {"items": items, "dry_run": not a.apply}, auth=a.user)
    for r in res["results"]:
        mark = "ok  " if r["ok"] else "FAIL"
        extra = f"  streams: {', '.join(r['streams'])}" if r.get("streams") else ""
        print(f"  {mark}  {r['old']}  ->  {r['new']}{extra}{'' if r['ok'] else '  ' + r['error']}")
    print(f"{res['ok']} ok, {res['failed']} failed")
    if a.apply:
        out = Path(a.csv).with_name(f"renames-applied-{time.strftime('%Y%m%d-%H%M%S')}.csv")
        with open(out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["video_id", "old_name", "new_name", "ok", "error"])
            for r in res["results"]:
                w.writerow([r["video_id"], r["old"], r["new"], r["ok"], r.get("error", "")])
        print(f"report (and undo list): {out}")
    return 0 if res["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
