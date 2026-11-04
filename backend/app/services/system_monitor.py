"""Server resource monitoring with psutil (sampled in the background)."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from pathlib import Path

import psutil

from ..config import Settings


class SystemMonitor:
    def __init__(self, settings: Settings, interval: float = 2.0):
        self.s = settings
        self.interval = interval
        self.sample: dict = {}
        self._prev_net = None
        self._prev_disk = None
        self._prev_t = None
        self._task: asyncio.Task | None = None
        psutil.cpu_percent(None)  # prime the counter

    def collect(self) -> dict:
        now = time.monotonic()
        vm = psutil.virtual_memory()
        net = psutil.net_io_counters()
        dio = psutil.disk_io_counters()
        rates = {}
        if self._prev_t is not None:
            dt = max(1e-3, now - self._prev_t)
            if net and self._prev_net:
                rates["net_sent_bps"] = (net.bytes_sent - self._prev_net.bytes_sent) * 8 / dt
                rates["net_recv_bps"] = (net.bytes_recv - self._prev_net.bytes_recv) * 8 / dt
            if dio and self._prev_disk:
                rates["disk_read_Bps"] = (dio.read_bytes - self._prev_disk.read_bytes) / dt
                rates["disk_write_Bps"] = (dio.write_bytes - self._prev_disk.write_bytes) / dt
        self._prev_net, self._prev_disk, self._prev_t = net, dio, now

        ff = []
        for p in psutil.process_iter(["pid", "name"]):
            if "ffmpeg" in (p.info.get("name") or ""):
                with contextlib.suppress(psutil.Error):
                    ff.append({"pid": p.pid, "cpu_percent": p.cpu_percent(None),
                               "rss_bytes": p.memory_info().rss})
        disks = {}
        for label, path in (("videos", self.s.video_dir), ("data", self.s.data_dir)):
            with contextlib.suppress(OSError):
                du = shutil.disk_usage(path)
                disks[label] = {"path": str(Path(path)), "total": du.total, "used": du.used, "free": du.free,
                                "percent": round(du.used / du.total * 100, 1) if du.total else None}
        load = None
        with contextlib.suppress(OSError, AttributeError):
            load = list(psutil.getloadavg())
        self.sample = {
            "cpu_percent": psutil.cpu_percent(None),
            "cpu_count": psutil.cpu_count(),
            "load_avg": load,
            "memory": {"total": vm.total, "available": vm.available, "used": vm.total - vm.available,
                       "percent": vm.percent},
            "disks": disks,
            "ffmpeg_processes": len(ff),
            "ffmpeg_cpu_percent": round(sum(x["cpu_percent"] for x in ff), 1),
            "ffmpeg_rss_bytes": sum(x["rss_bytes"] for x in ff),
            "ffmpeg": ff,
            **{k: round(v) for k, v in rates.items()},
            "sampled_at": time.time(),
        }
        return self.sample

    async def run(self) -> None:
        while True:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.collect)
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self.collect()
        self._task = asyncio.create_task(self.run(), name="system-monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    def warnings(self) -> list[str]:
        w = []
        smp = self.sample or {}
        if smp.get("cpu_percent", 0) >= self.s.cpu_warn_percent:
            w.append(f"CPU at {smp['cpu_percent']:.0f}% (warning threshold {self.s.cpu_warn_percent:.0f}%).")
        mem = smp.get("memory", {})
        if mem.get("percent", 0) >= self.s.mem_warn_percent:
            w.append(f"Memory at {mem['percent']:.0f}% (warning threshold {self.s.mem_warn_percent:.0f}%).")
        vid = smp.get("disks", {}).get("videos")
        if vid:
            reserve = max(self.s.disk_reserve_bytes, int(vid["total"] * self.s.disk_reserve_percent / 100))
            if vid["free"] < reserve * 2:
                w.append(f"Video disk nearly full: {vid['free'] / 1024**3:.1f} GiB free.")
        return w
