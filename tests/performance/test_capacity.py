"""Capacity measurements: 1/4/8/16 streams with RTSP readers, copy vs transcode.

Run:  pytest -m performance -s tests/performance/test_capacity.py
Writes docs/reports/performance-<hostname>-<date>.json.

These numbers describe the machine the test runs on. They are NOT a
statement about the Proxmox VM: run this on the VM itself for that.
"""

from __future__ import annotations

import json
import os
import platform
import socket
import subprocess
import time
from datetime import datetime, timezone

import psutil
import pytest

from helpers import FIXTURES, REPO, ffmpeg_publishers, wait_for

pytestmark = pytest.mark.performance

PERF_FILE = "perf_1080p25_4mbps.mp4"
MEASURE_S = float(os.environ.get("PERF_MEASURE_SECONDS", "30"))


@pytest.fixture(scope="module")
def perf_video():
    p = FIXTURES / PERF_FILE
    if not p.exists():
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=1920x1080:rate=25", "-t", "120", "-c:v", "libx264", "-preset", "veryfast",
                        "-b:v", "4M", "-maxrate", "4M", "-bufsize", "8M", "-g", "50", "-pix_fmt", "yuv420p",
                        str(p)], check=True)
    return PERF_FILE


def _sample(rig, pids_extra: list[int], seconds: float) -> dict:
    procs = {}
    for pid in ffmpeg_publishers(rig.run_dir_marker()) + pids_extra:
        try:
            procs[pid] = psutil.Process(pid)
            procs[pid].cpu_percent(None)
        except psutil.Error:
            pass
    mtx_pid = rig.client.get("/api/mediamtx/status").json()["managed_pid"]
    mtx = psutil.Process(mtx_pid)
    mtx.cpu_percent(None)
    psutil.cpu_percent(None)
    paths0 = {p["name"]: p for p in rig.client.get("/api/mediamtx/status").json()["paths"]}
    time.sleep(seconds)
    pub_cpu = pub_rss = 0.0
    pub_ids = set(ffmpeg_publishers(rig.run_dir_marker()))
    for pid, p in procs.items():
        if pid in pub_ids:
            try:
                pub_cpu += p.cpu_percent(None)
                pub_rss += p.memory_info().rss
            except psutil.Error:
                pass
    paths1 = {p["name"]: p for p in rig.client.get("/api/mediamtx/status").json()["paths"]}
    rx = sum(paths1[n]["bytes_received"] - paths0.get(n, {"bytes_received": 0})["bytes_received"]
             for n in paths1)
    streams = rig.client.get("/api/streams").json()
    return {
        "host_cpu_percent": psutil.cpu_percent(None),
        "publishers_cpu_percent": round(pub_cpu, 1),
        "publishers_rss_mb": round(pub_rss / 1e6, 1),
        "mediamtx_cpu_percent": round(mtx.cpu_percent(None), 1),
        "mediamtx_rss_mb": round(mtx.memory_info().rss / 1e6, 1),
        "ingest_mbit_s": round(rx * 8 / seconds / 1e6, 2),
        "viewers": sum(s["runtime"]["viewer_count"] for s in streams),
        "dropped_frames": sum((s["runtime"]["progress"] or {}).get("drop_frames", 0) for s in streams),
        "publisher_restarts": sum(s["restart_count"] for s in streams),
        "not_running": [s["name"] for s in streams if s["status"] != "RUNNING"],
        "speed_min": min(((s["runtime"]["progress"] or {}).get("speed") or 0) for s in streams),
    }


def _run_level(rig, video, n, readers_per_stream, transcode=False) -> dict:
    t0 = time.monotonic()
    ids = []
    for i in range(n):
        r = rig.client.post("/api/streams", json={"name": f"p{i}", "video_id": rig.video_id(video),
                                                  "start": f"00:00:{(i * 7) % 60:02d}",
                                                  "force_transcode": transcode})
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    for i in ids:
        rig.wait_status(i, "RUNNING", 60)
    startup = time.monotonic() - t0
    readers = []
    for i in range(n):
        for _ in range(readers_per_stream):
            readers.append(subprocess.Popen(
                ["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i", rig.url(f"p{i}"),
                 "-c", "copy", "-f", "null", "-"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    if readers:
        wait_for(lambda: sum(s["runtime"]["viewer_count"] for s in rig.client.get("/api/streams").json())
                 >= len(readers), 30, desc="readers connected")
    m = _sample(rig, [], MEASURE_S)
    reader_failures = sum(1 for p in readers if p.poll() is not None)
    for p in readers:
        p.terminate()
    for p in readers:
        p.wait()
    for i in ids:
        rig.client.delete(f"/api/streams/{i}")
    wait_for(lambda: not ffmpeg_publishers(rig.run_dir_marker()), 20, desc="publishers gone")
    return {"streams": n, "mode": "transcode" if transcode else "copy", "readers": len(readers),
            "startup_all_running_s": round(startup, 2), "reader_disconnects": reader_failures, **m}


def test_capacity_levels(rig_factory, perf_video):
    results = []
    with rig_factory([perf_video], max_active_streams=32, max_transcode_streams=8,
                     recovery_concurrency=16) as rig:
        for n, readers in ((1, 1), (4, 1), (8, 1), (16, 0), (16, 1), (16, 2)):
            results.append(_run_level(rig, perf_video, n, readers))
            print("\n", json.dumps(results[-1]))
        for n in (1, 2, 4):
            results.append(_run_level(rig, perf_video, n, 1, transcode=True))
            print("\n", json.dumps(results[-1]))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": socket.gethostname(), "platform": platform.platform(), "machine": platform.machine(),
        "cpu_count": psutil.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 1e9, 1),
        "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0],
        "source": f"{perf_video} (1920x1080 25 fps H.264 4 Mbit/s)", "measure_seconds": MEASURE_S,
        "note": "Measured on the development machine, not the Proxmox VM.",
        "results": results,
    }
    out = REPO / "docs" / "reports" / f"performance-{socket.gethostname()}-{datetime.now():%Y%m%d}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport written to {out}")
    for r in results:
        assert not r["not_running"], r
        assert r["reader_disconnects"] == 0, r
        assert r["publisher_restarts"] == 0, r
