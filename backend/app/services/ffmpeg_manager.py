"""FFmpeg publisher: playlist generation, command construction, process I/O.

Design (see docs/FFMPEG.md for the measurements behind it):

* Input is always an ``ffconcat`` playlist with ``inpoint``/``outpoint``.
  - play once  -> one entry.
  - loop       -> the same entry repeated K times. FFmpeg's concat demuxer
    offsets every entry by exactly (outpoint - inpoint), so timestamps stay
    continuous and monotonic across loop boundaries, with no demuxer seek.
    (``-stream_loop`` was rejected: it ignores input ``-t``, and on sources
    with B-frames and a non-zero inpoint its seek fails and looping stops.)
* ``inpoint`` is snapped to the keyframe at/before the requested start so
  each iteration begins on a clean keyframe (no preroll packets, no
  backwards DTS at the boundary).
* An end offset is snapped to the keyframe at/before the requested end.
  ``outpoint`` is that keyframe's *DTS* (the demuxer cuts by DTS, so the
  keyframe itself is excluded even with B-frames) and the ``duration``
  directive is its *PTS* minus inpoint, which sets the per-loop timestamp
  offset. Measured result: every iteration carries exactly the same frames,
  no duplicate PTS, no gap at the loop point.
* ``-re`` paces reading at 1x on the (continuous) input timestamps.
* Loop count = floor(out_time / period): an exact count derived from the
  published media timestamps, not from wall-clock uptime.
* Commands are argv lists; no shell is ever involved.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import psutil

from ..config import Settings
from . import encoders
from .shape import fit_filter

log = logging.getLogger(__name__)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class PlanError(ValueError):
    pass


@dataclass
class PublishPlan:
    stream_id: int
    name: str
    source_path: Path
    inpoint_abs: float  # absolute file timestamp (includes container start_time)
    outpoint_abs: float | None  # cut point (keyframe DTS); None = to end of file
    duration_s: float  # length of one pass = per-loop timestamp offset
    mode: str  # loop | once
    output_mode: str  # copy | transcode
    publish_url: str
    playlist_path: Path
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    out_width: int | None = None  # scale to exactly this size (standard output); None = keep
    out_height: int | None = None
    entries: int = 1
    encoder: str = "cpu"  # live-transcode encoder (services/encoders.py); copy mode ignores it
    # What the playlist names: a per-stream symlink to source_path, so renaming the video only
    # re-points the link and the next loop (FFmpeg reopens the file per entry) follows it.
    input_path: Path | None = None
    # Server clock on every frame ("IST" | "UTC"); None = no clock. Only for transcoded streams.
    clock_tz: str | None = None

    @property
    def period_s(self) -> float:
        return self.duration_s


def loop_entries(period_s: float, settings: Settings) -> int:
    if period_s <= 0:
        raise PlanError("segment period must be positive")
    k = math.ceil(settings.loop_playlist_hours * 3600.0 / period_s)
    return max(2, min(k, settings.loop_playlist_max_entries))


def ffconcat_quote(path: str) -> str:
    """Quote a path for an ffconcat ``file`` directive."""
    if _CONTROL_CHARS.search(path):
        raise PlanError("source path contains control characters")
    return "'" + path.replace("'", "'\\''") + "'"


# Timestamps from ffprobe are printed rounded to 1 us. At NTSC timebases (e.g.
# 1/30000) a DTS like 418418/30000 = 13.9472666.. prints as 13.947267: an
# outpoint written as that value is slightly LATER than the real keyframe DTS,
# so the keyframe leaked into every loop (duplicate PTS once per loop, measured
# in the soak test). Cutting 0.5 ms early / seeking 0.5 ms late is exact for
# any real frame rate (frames are >= ~8 ms apart even at 120 fps).
TS_EPSILON_S = 0.0005


def render_playlist(plan: PublishPlan) -> str:
    inpoint = plan.inpoint_abs + TS_EPSILON_S if plan.inpoint_abs > 0 else 0.0
    entry = f"file {ffconcat_quote(str(plan.input_path or plan.source_path))}\ninpoint {inpoint:.6f}\n"
    if plan.outpoint_abs is not None:
        entry += f"outpoint {plan.outpoint_abs - TS_EPSILON_S:.6f}\n"
    entry += f"duration {plan.duration_s:.6f}\n"
    return "ffconcat version 1.0\n" + entry * plan.entries


def source_link(playlist_path: Path, source: Path) -> Path:
    """The per-stream symlink a playlist reads the video through (next to the playlist)."""
    return playlist_path.with_name(f"source{source.suffix.lower()}")


def point_link(link: Path, target: Path) -> None:
    """Make ``link`` a symlink to ``target``, atomically (readers see the old or the new target, never none)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(link.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    os.symlink(target, tmp)
    os.replace(tmp, link)


def write_playlist(plan: PublishPlan) -> None:
    plan.playlist_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.playlist_path.with_suffix(".tmp")
    tmp.write_text(render_playlist(plan))
    os.replace(tmp, plan.playlist_path)


def build_command(plan: PublishPlan, settings: Settings) -> list[str]:
    args: list[str] = [
        settings.ffmpeg_bin, "-hide_banner", "-nostdin",
        "-loglevel", "level+info", "-nostats",
        "-progress", "pipe:1", "-stats_period", "1",
        *(encoders.input_args(plan.encoder) if plan.output_mode == "transcode" else []),
        "-re",  # read input at native frame rate (1x)
        "-f", "concat", "-safe", "0", "-i", str(plan.playlist_path),
        "-map", "0:v:0",
        "-an", "-sn", "-dn",  # drop audio, subtitles and data tracks
    ]
    if plan.output_mode == "copy":
        args += ["-c:v", "copy"]
    elif plan.output_mode == "transcode":
        args += transcode_args(plan, settings)
    else:  # pragma: no cover - guarded by caller
        raise PlanError(f"unknown output mode {plan.output_mode}")
    args += ["-f", "rtsp", "-rtsp_transport", "tcp", plan.publish_url]
    return args


CLOCK_OFFSET = {"IST": 19800, "UTC": 0}


def clock_text_file(plan: PublishPlan) -> Path:
    return plan.playlist_path.parent / "clock.txt"


def write_clock_text(plan: PublishPlan) -> None:
    """The drawtext template (in a file: no filter-option escaping). pts = wall clock seconds at draw time."""
    off = CLOCK_OFFSET.get(plan.clock_tz or "IST", 19800)
    clock_text_file(plan).parent.mkdir(parents=True, exist_ok=True)
    clock_text_file(plan).write_text(
        "%{pts:gmtime:" + str(off) + r":%Y-%m-%d %H\:%M\:%S}.%{eif:mod(floor(t*1000),1000):d:3} " + (plan.clock_tz or "IST"))


def clock_filter(plan: PublishPlan, settings: Settings) -> str:
    h = plan.out_height or min(plan.height or 720, settings.transcode_max_height)
    size = max(16, round(h / 24))
    font = settings.slate_font or "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
    return (f"drawtext=fontfile={font}:textfile={clock_text_file(plan)}:x=w-tw-{size}:y={size}:fontsize={size}"
            f":fontcolor=white:box=1:boxcolor=black@0.65:boxborderw={max(4, size // 4)}")


def transcode_args(plan: PublishPlan, settings: Settings) -> list[str]:
    fps = plan.fps if plan.fps and plan.fps > 0 else 25.0
    gop = max(1, int(round(fps * settings.x264_gop_seconds)))
    args = encoders.encoder_args(plan.encoder, profile=settings.x264_profile, gop=gop,
                                 gop_seconds=settings.x264_gop_seconds, maxrate_kbps=settings.x264_maxrate_kbps,
                                 crf=settings.x264_crf, preset=settings.x264_preset, tune=settings.x264_tune,
                                 threads=settings.x264_threads)
    w, h = plan.width or 0, plan.height or 0
    if plan.out_width and plan.out_height:
        args += ["-vf", fit_filter(plan.out_width, plan.out_height, "pad")]
    elif w > settings.transcode_max_width or h > settings.transcode_max_height:
        args += ["-vf", (f"scale=w={settings.transcode_max_width}:h={settings.transcode_max_height}"
                         ":force_original_aspect_ratio=decrease:force_divisible_by=2")]
    if plan.clock_tz:
        # Wall-clock time (ms) of the moment each frame is drawn: frames get the real time as timestamps for the
        # drawtext, then timestamps restart from 0 (paced by -re, so playback timing is unchanged).
        clock = clock_filter(plan, settings)
        if "-vf" in args:
            k = args.index("-vf") + 1
            args[k] = f"setpts=RTCTIME/(TB*1000000),{args[k]},{clock},setpts=PTS-STARTPTS"
        else:
            args += ["-vf", f"setpts=RTCTIME/(TB*1000000),{clock},setpts=PTS-STARTPTS"]
    up = encoders.upload_filter(plan.encoder)
    if up:  # GPU encoders take frames in GPU memory: scale on the CPU, then upload
        if "-vf" in args:
            k = args.index("-vf") + 1
            args[k] = f"{args[k]},{up}"
        else:
            args += ["-vf", up]
    if settings.output_fps_mode == "cfr":
        args += ["-fps_mode", "cfr", "-r", f"{fps:.6g}"]
    else:
        args += ["-fps_mode", "passthrough"]
    return args


# ------------------------------------------------------------------ progress

@dataclass
class Progress:
    frame: int = 0
    fps: float = 0.0
    out_time_s: float = 0.0
    speed: float | None = None
    drop_frames: int = 0
    dup_frames: int = 0
    total_size: int = 0
    bitrate: str | None = None
    updated_at: datetime | None = None
    _pending: dict = field(default_factory=dict)

    def feed(self, line: str) -> bool:
        """Consume one ``-progress`` line; True when a block completed."""
        if "=" not in line:
            return False
        key, _, value = line.strip().partition("=")
        self._pending[key] = value
        if key != "progress":
            return False
        p = self._pending
        self._pending = {}
        self.load(p)
        return True

    def load(self, p: dict) -> None:
        """Apply one completed ``-progress`` block (from the pipe or the runner's state file)."""
        if not p:
            return
        with contextlib.suppress(ValueError):
            self.frame = int(p.get("frame", self.frame))
        with contextlib.suppress(ValueError):
            self.fps = float(p.get("fps", self.fps))
        us = p.get("out_time_us") or p.get("out_time_ms")  # both are microseconds
        if us and us != "N/A":
            with contextlib.suppress(ValueError):
                self.out_time_s = max(0.0, int(us) / 1_000_000)
        sp = (p.get("speed") or "").rstrip("x")
        with contextlib.suppress(ValueError):
            self.speed = float(sp) if sp and sp != "N/A" else self.speed
        with contextlib.suppress(ValueError):
            self.drop_frames = int(p.get("drop_frames", self.drop_frames))
        with contextlib.suppress(ValueError):
            self.dup_frames = int(p.get("dup_frames", self.dup_frames))
        with contextlib.suppress(ValueError):
            self.total_size = int(p.get("total_size", self.total_size))
        self.bitrate = p.get("bitrate", self.bitrate)
        upd = p.get("updated_at")
        self.updated_at = datetime.fromisoformat(upd) if upd else datetime.now(timezone.utc)

    def out_time_now(self, now: datetime | None = None, max_ahead_s: float = 1.5) -> float:
        """Output time right now: FFmpeg reports progress once per second (``-stats_period 1``), so the
        last report is up to ~1 s old. Extrapolate at real-time pace (publishers run with ``-re``),
        capped so a stalled publisher does not run ahead. Used only for the shown playback position."""
        if self.updated_at is None:
            return self.out_time_s
        age = ((now or datetime.now(timezone.utc)) - self.updated_at).total_seconds()
        return self.out_time_s + min(max(age, 0.0), max_ahead_s)

    def loops_completed(self, period_s: float) -> int:
        if period_s <= 0:
            return 0
        # small epsilon: the first iteration ends one frame after period
        return int(math.floor((self.out_time_s + 1e-3) / period_s))

    def snapshot(self) -> dict:
        return {
            "frame": self.frame, "fps": self.fps, "out_time_s": round(self.out_time_s, 3),
            "speed": self.speed, "drop_frames": self.drop_frames, "dup_frames": self.dup_frames,
            "total_size": self.total_size, "bitrate": self.bitrate,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ------------------------------------------------------------------- logging

# Expected once per loop boundary when stream-copying B-frame video; FFmpeg
# corrects the DTS itself. Collapsed so it does not drown real errors.
_NOISY = re.compile(r"(Non-monotonic DTS|non monotonically increasing dts|changing to \d+)", re.I)


class LogSink:
    """Per-stream log file with size-based rotation.

    FFmpeg's own output is written by the publisher runner process; this
    class adds the backend's "[rtspm]" lines and reads the tail of the file
    (so the UI shows the same thing whether or not the backend restarted).
    """

    def __init__(self, path: Path, max_bytes: int, backups: int, tail: int = 200):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.tail_size = tail
        self.suppressed = 0
        path.parent.mkdir(parents=True, exist_ok=True)

    def _rotate_if_needed(self) -> None:
        try:
            if self.path.stat().st_size < self.max_bytes:
                return
        except FileNotFoundError:
            return
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            dst = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, dst)

    def write(self, text: str, source: str = "ffmpeg") -> None:
        text = text.rstrip()
        if not text:
            return
        if source == "ffmpeg" and _NOISY.search(text):
            self.suppressed += 1
            if self.suppressed not in (1, 2, 3) and self.suppressed % 500 != 0:
                return
            if self.suppressed > 3:
                text = f"(timestamp fix-up warnings so far: {self.suppressed}; expected at loop boundaries) {text}"
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        try:
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(f"{ts} [{source}] {text}\n")
        except OSError as exc:  # never let logging kill supervision
            log.warning("cannot write stream log %s: %s", self.path, exc)

    @staticmethod
    def _tail_of(path: Path) -> list[str]:
        try:
            with path.open("rb") as f:
                size = path.stat().st_size
                f.seek(max(0, size - 64_000))
                lines = f.read().decode(errors="replace").splitlines()
                return lines[1:] if size > 64_000 else lines  # drop a partial first line
        except OSError:
            return []

    def _read_tail(self) -> list[str]:
        lines = self._tail_of(self.path)
        if len(lines) < self.tail_size:  # just rotated: continue from the previous file
            lines = self._tail_of(self.path.with_name(f"{self.path.name}.1")) + lines
        return lines[-self.tail_size:]

    def tail(self, n: int) -> list[str]:
        return self._read_tail()[-n:]

    def last_error(self) -> str | None:
        for line in reversed(self._read_tail()):
            if "[ffmpeg]" in line and ("[error]" in line or "[fatal]" in line):
                return line.split("[ffmpeg] ", 1)[-1].replace("[error] ", "").replace("[fatal] ", "")
        return None


# ------------------------------------------------------------------- process

_BACKEND_DIR = Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        p = psutil.Process(pid)
        return p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


class PublisherProcess:
    """One FFmpeg publisher, run through ``publisher_runner`` so it can
    outlive (and be re-adopted by) the backend."""

    def __init__(self, argv: list[str], sink: LogSink, state_path: Path, settings: Settings | None = None,
                 meta: dict | None = None):
        self.argv = argv
        self.sink = sink
        self.state_path = state_path
        self.settings = settings
        self.meta = meta or {}
        self.progress = Progress()
        self.proc: asyncio.subprocess.Process | None = None  # set when we spawned the runner
        self.runner_pid: int | None = None
        self.ffmpeg_pid: int | None = None
        self.started_at: datetime | None = None
        self.adopted = False
        self._exit_code: int | None = None
        self._state_mtime = 0.0

    # ------------------------------------------------------------ state file
    def refresh(self) -> dict | None:
        try:
            m = self.state_path.stat().st_mtime
        except OSError:
            return None
        if m == self._state_mtime:
            return None
        self._state_mtime = m
        st = _read_json(self.state_path)
        if not st:
            return None
        self.ffmpeg_pid = st.get("pid") or self.ffmpeg_pid
        self.sink.suppressed = st.get("suppressed", self.sink.suppressed)
        self.progress.load(st.get("progress") or {})
        if st.get("exit_code") is not None:
            self._exit_code = st["exit_code"]
        return st

    @property
    def pid(self) -> int | None:
        if self.ffmpeg_pid is None:
            self.refresh()
        return self.ffmpeg_pid or self.runner_pid

    @property
    def returncode(self) -> int | None:
        return None if self.alive() else self._exit_code

    @property
    def suppressed(self) -> int:
        return self.sink.suppressed

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        s = self.settings
        with contextlib.suppress(FileNotFoundError):
            self.state_path.unlink()
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "app.services.publisher_runner", "--state", str(self.state_path),
               "--log", str(self.sink.path),
               "--max-bytes", str(s.stream_log_max_bytes if s else 1_000_000),
               "--backups", str(s.stream_log_backups if s else 2),
               "--stop-timeout", str(s.stop_timeout_s if s else 5.0),
               "--meta", json.dumps(self.meta), "--", *self.argv]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(_BACKEND_DIR), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # own session: survives the backend and terminal signals
        )
        self.runner_pid = self.proc.pid
        self.started_at = datetime.now(timezone.utc)
        for _ in range(40):  # wait for the runner to start FFmpeg (normally < 100 ms)
            st = self.refresh()
            if self.ffmpeg_pid or (st and st.get("exit_code") is not None) or self.proc.returncode is not None:
                break
            await asyncio.sleep(0.05)

    @classmethod
    def adopt(cls, state_path: Path, sink: LogSink, settings: Settings | None = None) -> "PublisherProcess | None":
        """Attach to a publisher left running by a previous backend instance."""
        st = _read_json(state_path)
        if not st or st.get("exit_code") is not None:
            return None
        if not (_pid_alive(st.get("runner_pid")) and _pid_alive(st.get("pid"))):
            return None
        pub = cls(st.get("argv") or [], sink, state_path, settings, st.get("meta") or {})
        pub.runner_pid, pub.ffmpeg_pid, pub.adopted = st["runner_pid"], st["pid"], True
        with contextlib.suppress(ValueError, TypeError):
            pub.started_at = datetime.fromisoformat(st.get("started_at"))
        pub.refresh()
        return pub

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.returncode is None
        return _pid_alive(self.runner_pid)

    async def wait(self) -> int:
        if self.proc is not None:
            await self.proc.wait()
        else:
            while _pid_alive(self.runner_pid):
                await asyncio.sleep(0.5)
        self._state_mtime = 0.0
        self.refresh()
        if self._exit_code is None:  # runner killed before it could record FFmpeg's exit
            self._exit_code = -9
        return self._exit_code

    async def stop(self, timeout: float) -> int | None:
        """SIGTERM to the runner -> FFmpeg exits cleanly (RTSP TEARDOWN); SIGKILL after timeout."""
        if not self.alive():
            return self.returncode
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(self.runner_pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(self.wait(), timeout + 2)
        except asyncio.TimeoutError:
            self.sink.write(f"publisher did not exit within {timeout:.0f}s; killing", "rtspm")
            for pid in (self.ffmpeg_pid, self.runner_pid):
                if pid:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(pid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wait(), 5)
        return self.returncode
