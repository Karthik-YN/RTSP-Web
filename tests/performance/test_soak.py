"""Long-running loop soak (EC-04 server side): one RTSP reader for 30 minutes.

Run:  SOAK_SECONDS=1800 pytest -m long -s tests/performance/test_soak.py

Checks over the whole run: the reader never disconnects, timestamps stay
monotonic with no gap or duplicate at any loop point, playback stays at 1x,
the publisher never restarts and the loop counter matches elapsed media.
This validates the server side only; EC-04 still requires the 5.x edge run
described in docs/EDGE_VALIDATION.md.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone

import pytest

from helpers import REPO

pytestmark = pytest.mark.long

SOAK_S = int(os.environ.get("SOAK_SECONDS", "1800"))


def test_loop_soak(rig_factory):
    with rig_factory(["h264_2997.mp4"]) as rig:
        # 29.97 fps B-frame source; loop 00:00:04 -> 00:00:14 (snaps to keyframes 4.004 / 14.014)
        s = rig.create("soak", "h264_2997.mp4", start="00:00:04", end="00:00:15", mode="loop")
        rig.wait_status(s["id"], "RUNNING", 20)
        d = rig.stream(s["id"])
        period = d["runtime"]["loop_period_s"]
        t0 = time.monotonic()
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", f"%+{SOAK_S}",
             "-show_entries", "packet=pts_time", "-of", "csv=p=0", rig.url("soak")],
            capture_output=True, text=True, timeout=SOAK_S + 300,
        )
        wall = time.monotonic() - t0
        raw = REPO / "docs" / "reports" / f"soak-{SOAK_S}s-{datetime.now():%Y%m%d-%H%M}-packets.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(proc.stdout)  # keep the raw data even if analysis below fails
        ts = []
        for line in proc.stdout.splitlines():
            field = line.split(",")[0].strip()  # ffprobe may append empty side-data columns
            if field and field != "N/A":
                ts.append(float(field))
        st = sorted(ts)
        steps = [b - a for a, b in zip(st, st[1:])]
        dups = len(ts) - len(set(round(t, 5) for t in ts))
        backwards = sum(1 for a, b in zip(ts, ts[1:]) if b < a - 0.2)  # beyond B-frame reordering
        d = rig.stream(s["id"])
        span = st[-1] - st[0]
        result = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "requested_s": SOAK_S, "wall_s": round(wall, 1), "media_span_s": round(span, 2),
            "ratio_wall_to_media": round(wall / span, 4), "packets": len(ts),
            "expected_packets": round(span * 30000 / 1001), "max_step_s": round(max(steps), 4),
            "duplicate_pts": dups, "large_backward_jumps": backwards, "loop_period_s": period,
            "loop_count": d["runtime"]["loop_count"], "expected_loops": int(span // period),
            "publisher_restarts": d["restart_count"], "status": d["status"],
            "reader_exit_code": proc.returncode, "reader_stderr": proc.stderr[-500:],
        }
        out = REPO / "docs" / "reports" / f"soak-{SOAK_S}s-{datetime.now():%Y%m%d-%H%M}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2))
        print("\n" + json.dumps(result, indent=2))
        assert proc.returncode == 0
        assert span == pytest.approx(SOAK_S, rel=0.01)
        assert wall / span == pytest.approx(1.0, abs=0.01)
        assert dups == 0 and backwards == 0 and max(steps) < 0.1
        assert d["restart_count"] == 0 and d["status"] == "RUNNING"
        assert abs(d["runtime"]["loop_count"] - span // period) <= 1
