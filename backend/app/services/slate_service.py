"""Per-stream lead-in slates.

A slate is a short H.264 MP4 that MediaMTX loops on a stream's URL until the
video starts ("always available" path, see docs/SPIKE_LEADIN.md). It is
black with the stream name and a running clock, so two cameras never send
identical frames (the edge would discard them as duplicates). FFmpeg builds
without FreeType cannot draw text; the slate then shows a colour band derived
from the name plus a moving counter, which is just as unique.

The slate always matches what the publisher sends: the same width, height
and frame rate (for transcoded streams it goes through the same scale filter)
and the same H.264 profile, so readers do not see a format change at the
switch. Files are cached per stream and configuration, written atomically and
made world-readable, because the systemd install runs MediaMTX as its own user.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from ..config import Settings

log = logging.getLogger(__name__)

SLATE_VERSION = "1"  # bump to rebuild every cached slate after a format change

PROFILES = {"constrained baseline": "baseline", "baseline": "baseline", "main": "main", "high": "high"}


@dataclass(frozen=True)
class SlateSpec:
    name: str
    width: int
    height: int
    fps_text: str  # e.g. "25/1", "30000/1001"
    profile: str  # baseline | main | high
    scale_vf: str | None = None  # transcoded streams: the publisher's scale filter

    def key(self, seconds: int) -> str:
        raw = "|".join(map(str, (SLATE_VERSION, self.name, self.width, self.height, self.fps_text, self.profile,
                                 self.scale_vf, seconds)))
        return hashlib.sha256(raw.encode()).hexdigest()[:12]


def fps_text_of(fps_text: str | None, fps: float | None) -> str:
    m = re.fullmatch(r"(\d+)/(\d+)", fps_text or "")
    if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:  # "0/0" = unknown
        return fps_text
    f = Fraction(fps or 25.0).limit_denominator(1001)
    return f"{f.numerator}/{f.denominator}"


def profile_of(video_profile: str | None) -> str:
    return PROFILES.get((video_profile or "").strip().lower(), "high")


def name_color(name: str) -> str:
    h = sum(ord(c) * (i + 1) for i, c in enumerate(name))
    return f"0x{(h * 2654435761) & 0xFFFFFF:06x}"


def slate_graph(spec: SlateSpec, drawtext: bool, font: str = "") -> str:
    """lavfi graph for the slate (stream names are [A-Za-z0-9_-], safe in filter text)."""
    w, h, r = spec.width, spec.height, spec.fps_text
    if drawtext:
        ff = f"fontfile='{font}':" if font else ""
        return (f"color=c=black:size={w}x{h}:rate={r},"
                f"drawtext={ff}text='{spec.name}':fontsize={max(12, h // 12)}:fontcolor=white:"
                f"x=(w-tw)/2:y=h/2-th,"
                f"drawtext={ff}text='waiting for video  %{{pts\\:hms}}':fontsize={max(10, h // 24)}:"
                f"fontcolor=gray:x=(w-tw)/2:y=h/2+th")
    tw, th = max(160, w // 4 // 2 * 2), max(120, h // 3 // 2 * 2)
    tw, th = min(tw, w // 2 * 2), min(th, h // 2 * 2)
    return (f"color=c=black:size={w}x{h}:rate={r}[bg];testsrc=size={tw}x{th}:rate={r}[t];"
            f"[bg]drawbox=x=0:y={h - h // 10}:w={w}:h={max(2, h // 18)}:color={name_color(spec.name)}:t=fill[b];"
            f"[b][t]overlay=x=(W-w)/2:y=(H-h)/2")


def slate_command(spec: SlateSpec, out: Path, seconds: int, drawtext: bool, settings: Settings) -> list[str]:
    g = max(1, round(float(Fraction(spec.fps_text))))  # keyframe every second
    args = [settings.ffmpeg_bin, "-nostdin", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", slate_graph(spec, drawtext, settings.slate_font), "-t", str(seconds)]
    if spec.scale_vf:
        args += ["-vf", spec.scale_vf]
    args += ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", spec.profile, "-pix_fmt", "yuv420p",
             "-g", str(g), "-keyint_min", str(g), "-sc_threshold", "0", "-an",
             "-movflags", "+faststart", "-f", "mp4", str(out)]
    return args


class SlateService:
    def __init__(self, settings: Settings):
        self.s = settings
        self.dir = settings.slate_path
        self._locks: dict[str, asyncio.Lock] = {}
        self._drawtext: bool | None = None  # None = not known yet

    def _has_drawtext(self) -> bool:
        if self._drawtext is None:
            try:
                out = subprocess.run([self.s.ffmpeg_bin, "-hide_banner", "-filters"], capture_output=True,
                                     text=True, timeout=15).stdout
                self._drawtext = re.search(r"\sdrawtext\s", out) is not None
            except Exception:
                self._drawtext = False
        return self._drawtext

    def path_for(self, stream_id: int, spec: SlateSpec) -> Path:
        return self.dir / f"{stream_id}-{spec.key(self.s.slate_seconds)}.mp4"

    def style(self) -> str:
        return "name and clock" if self._drawtext else "colour band and counter"

    async def ensure(self, stream_id: int, spec: SlateSpec) -> Path:
        """Return the stream's slate, rendering it first if needed."""
        out = self.path_for(stream_id, spec)
        lock = self._locks.setdefault(out.name, asyncio.Lock())
        async with lock:
            if out.exists() and out.stat().st_size > 0:
                return out
            await asyncio.to_thread(self._render, stream_id, spec, out)
            return out

    def _render(self, stream_id: int, spec: SlateSpec, out: Path) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.dir, 0o755)
        tmp = out.with_suffix(".tmp.mp4")
        attempts = [True, False] if self._has_drawtext() else [False]
        err = ""
        for drawtext in attempts:
            argv = slate_command(spec, tmp, self.s.slate_seconds, drawtext, self.s)
            try:
                r = subprocess.run(argv, capture_output=True, text=True, timeout=self.s.slate_timeout_s)
            except subprocess.TimeoutExpired:
                err = f"slate rendering took longer than {self.s.slate_timeout_s:.0f}s"
                continue
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                if not drawtext and attempts[0]:
                    log.warning("drawtext failed (no font?); slates use the colour band: %s", err or r.stderr[-300:])
                    self._drawtext = False
                os.chmod(tmp, 0o644)  # MediaMTX may run as another user
                os.replace(tmp, out)
                self.prune(stream_id, keep=out)
                return
            err = (r.stderr or "").strip()[-500:]
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise RuntimeError(f"Could not make the lead-in slate: {err or 'ffmpeg failed'}")

    def prune(self, stream_id: int, keep: Path | None = None) -> None:
        """Remove a stream's slates (all of them, or all but ``keep``)."""
        if not self.dir.exists():
            return
        for p in self.dir.glob(f"{stream_id}-*.mp4"):
            if keep is None or p != keep:
                with contextlib.suppress(OSError):
                    p.unlink()

    @staticmethod
    def available() -> bool:
        return shutil.which("ffmpeg") is not None
