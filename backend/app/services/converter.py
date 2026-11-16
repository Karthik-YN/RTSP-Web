"""Convert library videos to a universal H.264 MP4.

One converted copy plays in every web browser, streams to the edge in copy
mode (no ongoing CPU), and gives new viewers a picture within 2 s. That fixes
all three problems a source file can have, whatever its codec or container:

* the browser cannot play it (MPEG-4 Part 2, H.265, 10-bit, ProRes, MKV, AVI…);
* streaming it needs live transcoding (costs a CPU core per stream);
* its keyframes are far apart (viewers wait for the first picture).

Two more profiles bring a video to the standard stream size (1280x720 by
default, ``RTSPM_OUTPUT_WIDTH/HEIGHT``):

* ``720p``: a 16:9 video scaled to the standard size, ``<name> (720p).mp4``.
  Streams set to "Standard" use it automatically, in copy mode.
* ``16x9``: any other shape (portrait, 4:3...) fitted into the standard
  16:9 frame with blurred bars, black bars or a crop, ``<name> (720p 16x9).mp4``.

The original is never touched. The copy is written next to it as
``<name> (h264).mp4`` (or the profile's suffix) through a temp file in the uploads folder (cleaned up at
startup if the server stops mid-way), linked in without overwriting, then
added to the library at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from ..database import utcnow
from ..models import Video, VideoStatus
from . import compatibility
from .ffprobe_service import ProbeError, probe
from .file_manager import LibraryError
from .shape import SHAPE_LABEL, fit_filter, video_shape

log = logging.getLogger(__name__)

BROWSER_CODECS = {"h264", "vp8", "vp9", "av1"}
BROWSER_PIX_FMTS = {"yuv420p", "yuvj420p"}
MP4_FAMILY = {".mp4", ".m4v", ".mov"}
SUFFIX = " (h264)"
LONG_GOP_S = 4.0
PROFILES = ("h264", "720p", "16x9")
FITS = ("blur", "pad", "crop")
PROFILE_SUFFIX = {"h264": SUFFIX, "720p": " (720p)", "16x9": " (720p 16x9)"}
PROFILE_LABEL = {"h264": "H.264 copy", "720p": "720p copy", "16x9": "16:9 copy"}


def is_copy(rel_path: str) -> bool:
    """A file this converter made (any profile)."""
    stem = PurePosixPath(rel_path).stem
    return any(stem.endswith(sfx) for sfx in PROFILE_SUFFIX.values())


def browser_playback(v: Video) -> tuple[bool, str | None]:
    """(plays in current Chrome/Edge/Firefox/Safari, why not)."""
    if v.status != VideoStatus.READY or not v.video_codec:
        return False, None
    codec = v.video_codec.lower()
    ext = PurePosixPath(v.rel_path).suffix.lower()
    if codec not in BROWSER_CODECS:
        return False, f"Browsers can't play {codec.upper()} video; VLC can."
    if (v.bit_depth or 8) > 8 or (v.pix_fmt and v.pix_fmt not in BROWSER_PIX_FMTS):
        return False, f"Browsers can't play {v.pix_fmt or 'this'} ({v.bit_depth or '?'}-bit) video; VLC can."
    if ext in MP4_FAMILY or (ext == ".webm" and codec != "h264"):
        return True, None
    return False, f"Browsers don't reliably play {ext or 'this'} files; VLC can."


def convert_reasons(v: Video) -> list[str]:
    """Why converting to H.264 would help (empty: nothing to gain)."""
    if v.status != VideoStatus.READY or v.compat_mode == compatibility.REJECT:
        return []
    if is_copy(v.rel_path):
        return []  # already a converted copy
    out = []
    ok, why = browser_playback(v)
    if not ok and why:
        out.append(why.replace("; VLC can.", "."))
    if v.compat_mode == compatibility.TRANSCODE:
        out.append("Streaming it needs live transcoding (a CPU core per stream).")
    if v.keyframe_interval_s and v.keyframe_interval_s > LONG_GOP_S:
        out.append(f"Keyframes only every {v.keyframe_interval_s:.1f} s: viewers wait for the first picture.")
    return out


def target_rel(rel_path: str, profile: str = "h264") -> str:
    p = PurePosixPath(rel_path)
    return str(p.with_name(f"{p.stem}{PROFILE_SUFFIX[profile]}.mp4"))


@dataclass
class Job:
    id: str
    video_id: int
    source: str
    target: str
    username: str | None
    profile: str = "h264"
    fit: str | None = None
    state: str = "queued"  # queued | running | done | failed | cancelled
    progress: float = 0.0
    error: str | None = None
    result_video_id: int | None = None
    created_at: str = field(default_factory=lambda: utcnow().isoformat())
    finished_at: str | None = None
    started_mono: float | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None

    @property
    def active(self) -> bool:
        return self.state in ("queued", "running")

    def view(self) -> dict:
        eta = None
        if self.state == "running" and self.started_mono and 0.02 < self.progress < 1:
            spent = time.monotonic() - self.started_mono
            eta = round(spent * (1 - self.progress) / self.progress)
        return {"id": self.id, "video_id": self.video_id, "source": self.source, "target": self.target,
                "state": self.state, "progress": round(self.progress, 3), "eta_s": eta, "error": self.error,
                "result_video_id": self.result_video_id, "username": self.username,
                "profile": self.profile, "fit": self.fit, "label": PROFILE_LABEL[self.profile],
                "created_at": self.created_at, "finished_at": self.finished_at}


class Converter:
    def __init__(self, library):
        self.lib = library  # VideoLibrary
        self.s = library.s
        self.jobs: dict[str, Job] = {}
        self._sem = asyncio.Semaphore(max(1, self.s.convert_concurrency))

    # ---------------------------------------------------------- queries
    def list(self) -> list[dict]:
        jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.view() for j in jobs[:50]]

    def active_for(self, video_id: int) -> Job | None:
        return next((j for j in self.jobs.values() if j.video_id == video_id and j.active), None)

    # ---------------------------------------------------------- control
    def start(self, video_id: int, username: str | None, profile: str = "h264", fit: str = "blur") -> Job:
        if profile not in PROFILES:
            raise LibraryError(f"Unknown conversion '{profile}' (use {', '.join(PROFILES)}).", 422)
        if fit not in FITS:
            raise LibraryError(f"Unknown fit '{fit}' (use {', '.join(FITS)}).", 422)
        with self.lib.db.session() as s:
            v = s.get(Video, video_id)
        if v is None:
            raise LibraryError(f"Video {video_id} not found.", 404)
        if v.status != VideoStatus.READY or v.compat_mode == compatibility.REJECT:
            raise LibraryError(f"'{v.rel_path}' can't be converted: {v.error or 'it is not a readable video'}.", 409)
        stem = PurePosixPath(v.rel_path).stem
        if stem.endswith(PROFILE_SUFFIX[profile]) or (profile == "h264" and is_copy(v.rel_path)):
            raise LibraryError(f"'{v.rel_path}' is already a converted copy.", 409)
        shape = video_shape(v)
        W, H = self.s.output_width, self.s.output_height
        if profile == "720p":
            if shape["is_16_9"] is False:
                raise LibraryError(f"'{v.rel_path}' is {SHAPE_LABEL[shape['shape']]}, not 16:9: make a 16:9 copy "
                                   "instead (blurred bars, black bars or crop).", 409)
            if (shape["display_width"], shape["display_height"]) == (W, H):
                raise LibraryError(f"'{v.rel_path}' is already {W}x{H}.", 409)
        if profile == "16x9" and shape["is_16_9"]:
            raise LibraryError(f"'{v.rel_path}' is already 16:9: make a 720p copy instead.", 409)
        if self.active_for(video_id):
            raise LibraryError(f"'{v.rel_path}' is already being converted.", 409)
        tgt = target_rel(v.rel_path, profile)
        if self.lib.lib.resolve(tgt).exists():
            raise LibraryError(f"'{tgt}' already exists in the library: this video was converted before.", 409)
        if self.s.transcode_policy == "never":
            raise LibraryError("Conversion is disabled (RTSPM_TRANSCODE_POLICY=never).", 409)
        need = self._estimate_bytes(v, profile)
        du = self.lib.lib.disk_usage()
        if du["free"] - need < self.lib.lib.reserve_bytes(du["total"]):
            raise LibraryError(f"Not enough disk space for the copy (about {need / 2**20:.0f} MiB needed).", 507,
                               {"free": du["free"]})
        job = Job(uuid.uuid4().hex[:12], v.id, v.rel_path, tgt, username, profile,
                  fit if profile == "16x9" else None)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, v), name=f"convert-{job.id}")
        self.lib.events.add("video.convert_started", f"{v.rel_path} -> {tgt}", username=username,
                            data={"video_id": v.id, "job": job.id})
        return job

    async def cancel(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise LibraryError("Conversion not found.", 404)
        if job.active:
            job.state = "cancelled"
            if job.proc and job.proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    job.proc.terminate()
            if job.task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(job.task), 10)
            job.finished_at = job.finished_at or utcnow().isoformat()
        return job

    async def stop(self) -> None:
        for job in list(self.jobs.values()):
            if job.active:
                await self.cancel(job.id)

    # ---------------------------------------------------------- work
    def _estimate_bytes(self, v: Video, profile: str = "h264") -> int:
        # CRF 20 H.264 is rarely above ~8 Mbit/s at 1080p; never assume less than the source
        if profile == "h264":
            pixels = (v.width or 1920) * (v.height or 1080)
        else:
            pixels = self.s.output_width * self.s.output_height
        mbps = max(2.0, 8.0 * pixels / (1920 * 1080))
        floor = (v.size_bytes or 0) if profile == "h264" else 0
        return int(max(floor, (v.duration_s or 600) * mbps * 1e6 / 8))

    def video_filter(self, profile: str = "h264", fit: str | None = None) -> str:
        if profile == "h264":
            return "scale=trunc(iw/2)*2:trunc(ih/2)*2"  # even dimensions (required by 4:2:0), any input size
        return fit_filter(self.s.output_width, self.s.output_height, fit or "pad")

    def command(self, v: Video, src: Path, out: Path, profile: str = "h264", fit: str | None = None) -> list[str]:
        fps = v.fps if v.fps and v.fps > 0 else 25.0
        gop = max(1, int(round(fps * self.s.x264_gop_seconds)))
        cmd = [self.s.ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "error", "-nostats", "-y",
               "-i", str(src), "-map", "0:v:0", "-sn", "-dn", "-an",
               "-c:v", "libx264", "-preset", self.s.convert_preset, "-crf", str(self.s.convert_crf),
               "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-vf", self.video_filter(profile, fit),
               "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
               "-force_key_frames", f"expr:gte(t,n_forced*{self.s.x264_gop_seconds:g})",
               "-movflags", "+faststart", "-f", "mp4", "-progress", "pipe:1", str(out)]
        if self.s.convert_threads > 0:
            cmd[cmd.index("-preset"):cmd.index("-preset")] = ["-threads", str(self.s.convert_threads)]
        return cmd

    async def _run(self, job: Job, v: Video) -> None:
        tmp = self.s.upload_tmp_path / f"convert-{job.id}.part"
        try:
            async with self._sem:
                if job.state == "cancelled":
                    return
                job.state, job.started_mono = "running", time.monotonic()
                src = self.lib.lib.resolve(v.rel_path)
                final = self.lib.lib.resolve(job.target)
                tmp.parent.mkdir(parents=True, exist_ok=True)
                err = await self._ffmpeg(job, v, src, tmp)
                if job.state == "cancelled":
                    return
                if err is not None:
                    raise LibraryError(err, 422)
                try:
                    meta = await asyncio.to_thread(probe, self.s.ffprobe_bin, tmp)
                except ProbeError as exc:
                    raise LibraryError(f"The converted file is not readable: {exc}", 422)
                if (meta.video_codec or "").lower() != "h264":
                    raise LibraryError("The converted file has no H.264 video.", 422)
                if job.profile != "h264" and (meta.width, meta.height) != (self.s.output_width, self.s.output_height):
                    raise LibraryError(f"The copy came out {meta.width}x{meta.height}, not "
                                       f"{self.s.output_width}x{self.s.output_height}.", 422)
                final.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.link(tmp, final)  # never overwrites
                except FileExistsError:
                    raise LibraryError(f"'{job.target}' appeared meanwhile; not overwritten.", 409)
                except OSError:  # no hard links (some network filesystems): exclusive rename
                    if final.exists():
                        raise LibraryError(f"'{job.target}' appeared meanwhile; not overwritten.", 409)
                    os.replace(tmp, final)
                async with self.lib._scan_lock:
                    with self.lib.db.session() as s:
                        row = s.scalar(select(Video).where(Video.rel_path == job.target))
                    video = row if row is not None else await self.lib._ingest(job.target, final.stat(), None)
                job.result_video_id = video.id
                job.progress, job.state = 1.0, "done"
                self.lib.events.add("video.converted", f"{job.source} -> {job.target}", username=job.username,
                                    data={"video_id": video.id, "source_video_id": job.video_id, "job": job.id})
        except asyncio.CancelledError:
            job.state = "cancelled"
            raise
        except Exception as exc:
            if job.state != "cancelled":
                job.state = "failed"
                job.error = exc.args[0] if isinstance(exc, LibraryError) else str(exc)
                log.warning("conversion %s failed: %s", job.id, job.error)
                self.lib.events.add("video.convert_failed", f"{job.source}: {job.error}", level="WARNING",
                                    username=job.username, data={"video_id": job.video_id, "job": job.id})
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
            job.proc = None
            job.finished_at = job.finished_at or utcnow().isoformat()

    async def _ffmpeg(self, job: Job, v: Video, src: Path, out: Path) -> str | None:
        """Run FFmpeg, following its progress. Returns an error message or None."""
        kw = {}
        if os.name == "posix":
            kw["preexec_fn"] = lambda: os.nice(10)  # never starve the live publishers
        job.proc = proc = await asyncio.create_subprocess_exec(
            *self.command(v, src, out, job.profile, job.fit), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kw)
        duration = v.duration_s or 0
        err_task = asyncio.create_task(proc.stderr.read())
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us=") and duration > 0:
                with contextlib.suppress(ValueError):
                    job.progress = min(0.99, max(0.0, int(line.split("=", 1)[1]) / 1e6 / duration))
        rc = await proc.wait()
        stderr = (await err_task).decode(errors="replace")
        if rc == 0:
            return None
        if job.state == "cancelled":
            return "cancelled"
        last = [ln for ln in stderr.strip().splitlines() if ln.strip()][-3:]
        msg = " ".join(last) or f"FFmpeg exited with code {rc}"
        # never leak server paths
        msg = msg.replace(str(src), PurePosixPath(v.rel_path).name).replace(str(out), "output")
        return "Conversion failed: " + re.sub(r"\s+", " ", msg)[:300]
