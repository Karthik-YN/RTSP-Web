"""Helpers shared by the test suites (real FFmpeg/MediaMTX tooling)."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "tests" / ".fixtures"


def have_binaries(*names: str) -> bool:
    return all(shutil.which(n) for n in names)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def mediamtx_config(rtsp_port: int, api_port: int, metrics_port: int, hold_s: int = 60) -> str:
    """The real deployment/mediamtx.yml with test ports substituted."""
    text = (REPO / "deployment" / "mediamtx.yml").read_text()
    text = text.replace("rtspAddress: :8554", f"rtspAddress: :{rtsp_port}")
    text = text.replace("127.0.0.1:9997", f"127.0.0.1:{api_port}")
    text = text.replace("127.0.0.1:9998", f"127.0.0.1:{metrics_port}")
    text = text.replace("runOnDemandStartTimeout: 60s", f"runOnDemandStartTimeout: {hold_s}s")
    return text


def link_or_copy(src: Path, dst: Path, link_min_bytes: int = 50 * 1024 * 1024) -> None:
    """Put a fixture into a test library.

    Small files are COPIED: tests modify/truncate/delete library files, and a
    hard link would damage the shared fixture. Only large files (read-only
    tests) are hard-linked to save time and disk.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.stat().st_size >= link_min_bytes:
        try:
            os.link(src, dst)
            return
        except OSError:
            pass
    shutil.copy2(src, dst)


def rtsp_packets(url: str, seconds: float, timeout: float = 60) -> tuple[list[dict], float, dict]:
    """Read ``seconds`` of media from an RTSP URL with ffprobe.

    Returns (packets, wall_seconds, stream_info).
    """
    t0 = time.monotonic()
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", f"%+{seconds}",
         "-show_entries", "packet=pts_time,dts_time,flags,stream_index:stream=index,codec_type,codec_name,"
         "pix_fmt,profile,width,height", "-of", "json", url],
        capture_output=True, timeout=timeout,
    )
    wall = time.monotonic() - t0
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[-800:])
    data = json.loads(proc.stdout or b"{}")
    return data.get("packets", []), wall, {"streams": data.get("streams", [])}


def packet_times(packets: list[dict]) -> list[float]:
    out = []
    for p in packets:
        t = p.get("pts_time")
        if t in (None, "N/A"):
            continue
        out.append(float(t))
    return out


def ffmpeg_publishers(marker: str) -> list[int]:
    """PIDs of ffmpeg processes whose command line contains ``marker``."""
    import psutil

    pids = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        with contextlib.suppress(psutil.Error):
            cmd = p.info.get("cmdline") or []
            if cmd and "ffmpeg" in os.path.basename(cmd[0]) and any(marker in a for a in cmd):
                pids.append(p.pid)
    return pids


def wait_for(predicate, timeout: float, interval: float = 0.25, desc: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {desc} (last={last!r})")
