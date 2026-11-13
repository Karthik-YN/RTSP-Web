"""Short browser previews of long videos.

Playing a 2-hour file in the browser means streaming hundreds of megabytes
(and some long recordings are in formats browsers can't play at all). For
videos longer than ``RTSPM_PREVIEW_THRESHOLD_S`` the library player shows a
short clip instead: ``RTSPM_PREVIEW_SECONDS`` from a chosen point, made once on
first request and cached in ``<data_dir>/previews``. A browser-playable H.264
source is cut without re-encoding (about a second; starts at the keyframe at or
before the chosen point); anything else is encoded to H.264 at up to 720p
(least recently used clips go first above ``RTSPM_PREVIEW_CACHE_BYTES``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from pathlib import Path

from ..models import Video
from .file_manager import LibraryError

log = logging.getLogger(__name__)


class PreviewCache:
    def __init__(self, library):
        self.lib = library
        self.s = library.s
        self._locks: dict[str, asyncio.Lock] = {}
        self._sem = asyncio.Semaphore(2)  # at most two clips being made at once

    @property
    def root(self) -> Path:
        return self.s.preview_path

    def offset(self, v: Video, start_s: float) -> int:
        """Clip start, whole seconds, so nearby requests share one cached clip."""
        dur = v.duration_s or 0
        latest = max(0.0, dur - self.s.preview_seconds)
        return int(max(0.0, min(float(start_s), latest)))

    def path_for(self, v: Video, start: int) -> Path:
        return self.root / f"{v.id}-{v.quick_hash[:8]}-{start}.mp4"

    @staticmethod
    def can_copy(v: Video) -> bool:
        """Cut without re-encoding: H.264 the browser plays, at a bitrate fit for a preview."""
        from .converter import browser_playback

        return ((v.video_codec or "").lower() == "h264" and browser_playback(v)[0]
                and (v.bitrate_bps or 0) <= 12_000_000)

    def command(self, src: Path, out: Path, start: int, copy: bool = False) -> list[str]:
        head = [self.s.ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                "-ss", str(start), "-i", str(src), "-t", str(self.s.preview_seconds),
                "-map", "0:v:0", "-an", "-sn", "-dn"]
        if copy:
            enc = ["-c:v", "copy"]
        else:
            enc = ["-vf", "scale=w=1280:h=720:force_original_aspect_ratio=decrease:force_divisible_by=2",
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p", "-g", "50"]
            # veryfast, not ultrafast: ~5x smaller clips, which matters more for viewers on
            # Tailscale than a few seconds once per clip (and a many-core server is quicker)
        return head + enc + ["-movflags", "+faststart", "-f", "mp4", str(out)]

    async def get(self, v: Video, start_s: float = 0.0) -> tuple[Path, int]:
        """The cached clip (made now if needed) and its start in seconds."""
        start = self.offset(v, start_s)
        out = self.path_for(v, start)
        lock = self._locks.setdefault(out.name, asyncio.Lock())
        async with lock:
            if out.is_file():
                with contextlib.suppress(OSError):
                    os.utime(out)  # recently used
                return out, start
            src = self.lib.lib.resolve(v.rel_path)
            if not src.is_file():
                raise LibraryError("The video file is missing on the server.", 404)
            self.root.mkdir(parents=True, exist_ok=True)
            self._forget_stale(v)
            tmp = out.with_suffix(".part")
            kw = {"preexec_fn": lambda: os.nice(10)} if os.name == "posix" else {}
            async with self._sem:
                t0 = time.monotonic()
                proc = await asyncio.create_subprocess_exec(
                    *self.command(src, tmp, start, self.can_copy(v)), stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE, **kw)
                _, err = await proc.communicate()
            if proc.returncode != 0 or not tmp.is_file():
                with contextlib.suppress(OSError):
                    tmp.unlink()
                msg = err.decode(errors="replace").strip().splitlines()[-1:] or ["FFmpeg failed"]
                raise LibraryError(f"Could not make a preview: {msg[0].replace(str(src), v.rel_path)[:200]}", 422)
            os.replace(tmp, out)
            log.info("preview %s from %ss made in %.1fs", out.name, start, time.monotonic() - t0)
        self._locks.pop(out.name, None)
        self.prune(keep=out)
        return out, start

    def _forget_stale(self, v: Video) -> None:
        """Clips of an older version of this file (different hash)."""
        for p in self.root.glob(f"{v.id}-*.mp4"):
            if not p.name.startswith(f"{v.id}-{v.quick_hash[:8]}-"):
                with contextlib.suppress(OSError):
                    p.unlink()

    def forget(self, video_id: int) -> None:
        """Drop every clip of a video (it was deleted)."""
        if self.root.is_dir():
            for p in self.root.glob(f"{video_id}-*.mp4"):
                with contextlib.suppress(OSError):
                    p.unlink()

    def prune(self, keep: Path | None = None) -> None:
        """Keep the cache under its size limit, least recently used first (never ``keep``,
        the clip about to be sent)."""
        if not self.root.is_dir():
            return
        files = []
        for p in self.root.glob("*.mp4"):
            with contextlib.suppress(OSError):
                st = p.stat()
                files.append((st.st_mtime, st.st_size, p))
        total = sum(f[1] for f in files)
        for _, size, p in sorted(files):
            if total <= self.s.preview_cache_bytes:
                break
            if p == keep:
                continue
            with contextlib.suppress(OSError):
                p.unlink()
                total -= size
