"""The browser's own live-preview path, for streams browsers cannot play.

Edges read ``rtsp://…/<name>`` exactly as published (for example H.265 sent as-is). Browsers can
only play H.264, so for such a stream the dashboard preview reads ``preview/<name>`` instead: a
small H.264 copy (at most 540 lines, low priority) made from the RTSP stream itself while someone
watches, and stopped about a minute after the last preview request. The edge's stream is never
touched, and H.264 streams keep previewing their own path (no extra work at all).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time

log = logging.getLogger(__name__)

PREFIX = "preview/"


def preview_path(name: str) -> str:
    return PREFIX + name


class LivePreviews:
    def __init__(self, settings, max_running: int = 4, idle_s: float = 60.0):
        self.s = settings
        self.max_running = max_running
        self.idle_s = idle_s
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._last: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._janitor: asyncio.Task | None = None

    def command(self, name: str) -> list[str]:
        return [self.s.ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-rtsp_transport", "tcp", "-i", self.s.internal_read_url(name),
                "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", "scale=-2:'min(ih,540)'",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-bf", "0",
                "-pix_fmt", "yuv420p", "-crf", "28",
                "-force_key_frames", "expr:gte(t,n_forced*1)",
                "-f", "rtsp", "-rtsp_transport", "tcp", self.s.publish_url(preview_path(name))]

    def running(self) -> list[str]:
        return [n for n, p in self._procs.items() if p.returncode is None]

    async def ensure(self, name: str) -> str:
        """Start (or keep alive) the preview copy of ``name``; returns the MediaMTX path to read."""
        self._last[name] = time.monotonic()
        async with self._lock:
            p = self._procs.get(name)
            if p is not None and p.returncode is None:
                return preview_path(name)
            if len(self.running()) >= self.max_running:
                raise RuntimeError(f"{self.max_running} previews of non-H.264 streams are already running; "
                                   "close one and try again.")
            kw = {"preexec_fn": lambda: os.nice(10)} if os.name == "posix" else {}
            self._procs[name] = await asyncio.create_subprocess_exec(
                *self.command(name), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL, **kw)
            log.info("live preview copy started for %s", name)
            if self._janitor is None or self._janitor.done():
                self._janitor = asyncio.create_task(self._sweep(), name="live-previews")
            return preview_path(name)

    async def _sweep(self) -> None:
        while self._procs:
            await asyncio.sleep(10)
            now = time.monotonic()
            for name in list(self._procs):
                p = self._procs[name]
                if p.returncode is not None or now - self._last.get(name, 0) > self.idle_s:
                    await self._stop(name)

    async def _stop(self, name: str) -> None:
        p = self._procs.pop(name, None)
        if p is None:
            return
        if p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
            try:
                await asyncio.wait_for(p.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    p.kill()
        log.info("live preview copy stopped for %s", name)

    async def stop(self) -> None:
        if self._janitor:
            self._janitor.cancel()
        for name in list(self._procs):
            await self._stop(name)
