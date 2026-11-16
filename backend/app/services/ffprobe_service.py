"""FFprobe/FFmpeg helpers for metadata, keyframe lookup and thumbnails.

All calls use argument lists (never a shell) and hard timeouts. These are
blocking functions; async callers run them in a worker thread.
"""

from __future__ import annotations

import json
import logging
import re
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)


class ProbeError(RuntimeError):
    pass


@dataclass
class ProbeResult:
    container: str | None = None
    duration_s: float | None = None
    start_time_s: float = 0.0
    bitrate_bps: int | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    fps_text: str | None = None
    avg_fps: float | None = None
    is_vfr: bool = False
    video_codec: str | None = None
    video_profile: str | None = None
    pix_fmt: str | None = None
    bit_depth: int | None = None
    field_order: str | None = None
    audio_codec: str | None = None
    keyframe_interval_s: float | None = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw", None)
        return d


def _run(args: list[str], timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"{Path(args[0]).name} timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise ProbeError(f"{args[0]} not found; is FFmpeg installed?") from exc


def _fraction(text: str | None) -> float | None:
    if not text or text in ("0/0", "N/A"):
        return None
    try:
        f = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    return float(f) if f > 0 else None


def bit_depth_from(pix_fmt: str | None, bits_per_raw_sample: str | None) -> int | None:
    if bits_per_raw_sample and str(bits_per_raw_sample).isdigit():
        return int(bits_per_raw_sample)
    if not pix_fmt:
        return None
    m = re.search(r"p(\d{2})(le|be)?$", pix_fmt)
    if m:
        return int(m.group(1))
    if pix_fmt.startswith(("yuv", "yuvj", "nv12", "gray", "rgb", "bgr")):
        return 8
    return None


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def probe(ffprobe: str, path: Path, timeout: float = 30.0) -> ProbeResult:
    """Read container/stream metadata plus a short packet sample.

    The packet sample (first ~20 s) is used to estimate the keyframe interval
    and detect variable frame rate; it does not read the whole file.
    """
    proc = _run(
        [ffprobe, "-v", "error", "-hide_banner", "-show_format", "-show_streams",
         "-of", "json", str(path)],
        timeout,
    )
    if proc.returncode != 0:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeError(msg[-1] if msg else "ffprobe could not read the file")
    try:
        data = json.loads(proc.stdout or b"{}")
    except json.JSONDecodeError as exc:
        raise ProbeError("ffprobe returned invalid JSON") from exc

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next(
        (s for s in streams if s.get("codec_type") == "video"
         and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    res = ProbeResult(raw=data)
    res.container = fmt.get("format_name")
    res.duration_s = _num(fmt.get("duration"))
    res.start_time_s = _num(fmt.get("start_time")) or 0.0
    res.bitrate_bps = int(_num(fmt.get("bit_rate")) or 0) or None
    res.audio_codec = audio.get("codec_name") if audio else None
    if video is None:
        return res
    res.video_codec = video.get("codec_name")
    res.video_profile = video.get("profile")
    res.width = video.get("width")
    res.height = video.get("height")
    res.pix_fmt = video.get("pix_fmt")
    res.bit_depth = bit_depth_from(res.pix_fmt, video.get("bits_per_raw_sample"))
    res.field_order = video.get("field_order")
    r_fps = _fraction(video.get("r_frame_rate"))
    a_fps = _fraction(video.get("avg_frame_rate"))
    res.fps = a_fps or r_fps
    res.avg_fps = a_fps
    res.fps_text = video.get("avg_frame_rate") or video.get("r_frame_rate")
    if res.duration_s is None:
        res.duration_s = _num(video.get("duration"))

    try:
        kfi, vfr = _sample_packets(ffprobe, path, timeout)
        res.keyframe_interval_s = kfi
        res.is_vfr = vfr or (r_fps is not None and a_fps is not None and abs(r_fps - a_fps) / r_fps > 0.01)
    except ProbeError as exc:
        log.warning("packet sampling failed for %s: %s", path, exc)
    return res


def _sample_packets(ffprobe: str, path: Path, timeout: float) -> tuple[float | None, bool]:
    proc = _run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", "%+20",
         "-show_entries", "packet=pts_time,dts_time,flags", "-of", "json", str(path)],
        timeout,
    )
    if proc.returncode != 0:
        raise ProbeError("packet sampling failed")
    pkts = json.loads(proc.stdout or b"{}").get("packets", [])
    times, keys = [], []
    for p in pkts:
        t = _num(p.get("pts_time")) if p.get("pts_time") not in (None, "N/A") else _num(p.get("dts_time"))
        if t is None:
            continue
        times.append(t)
        if "K" in (p.get("flags") or ""):
            keys.append(t)
    kfi = None
    if len(keys) >= 2:
        kfi = statistics.median(b - a for a, b in zip(keys, keys[1:]))
    vfr = False
    st = sorted(times)
    deltas = [b - a for a, b in zip(st, st[1:]) if b > a]
    if len(deltas) >= 10:
        mean = statistics.fmean(deltas)
        if mean > 0 and statistics.pstdev(deltas) / mean > 0.10:
            vfr = True
    return kfi, vfr


def keyframe_at_or_before(ffprobe: str, path: Path, abs_t: float, window_s: float,
                          timeout: float = 20.0) -> tuple[float, float] | None:
    """Return (pts, dts) in absolute seconds of the last keyframe with pts <= abs_t.

    Uses ``-read_intervals`` so ffprobe seeks near the target instead of
    reading the file from the beginning (important for multi-hour files).
    The DTS matters for loop end points: the concat demuxer cuts at outpoint
    by DTS, and with B-frames a keyframe's DTS is earlier than its PTS.
    """
    for win in (window_s, window_s * 4, window_s * 16):
        lo = max(0.0, abs_t - win)
        proc = _run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", f"{lo:.6f}%{abs_t + 0.0005:.6f}",
             "-show_entries", "packet=pts_time,dts_time,flags", "-of", "json", str(path)],
            timeout,
        )
        if proc.returncode != 0:
            raise ProbeError("keyframe lookup failed")
        best: tuple[float, float] | None = None
        for p in json.loads(proc.stdout or b"{}").get("packets", []):
            if "K" not in (p.get("flags") or ""):
                continue
            dts = _num(p.get("dts_time")) if p.get("dts_time") not in (None, "N/A") else None
            pts = _num(p.get("pts_time")) if p.get("pts_time") not in (None, "N/A") else dts
            if pts is None:
                continue
            if pts <= abs_t + 1e-6 and (best is None or pts > best[0]):
                best = (pts, dts if dts is not None else pts)
        if best is not None:
            return best
        if lo == 0.0:
            break
    return None


def video_end_time(ffprobe: str, path: Path, approx_end: float, timeout: float = 20.0) -> float | None:
    """Absolute end of the *video* track: max(pts + duration) of its last packets.

    The container duration can be longer (e.g. an audio track that runs on);
    using it as the loop period would insert a frozen gap at every loop.
    """
    lo = max(0.0, approx_end - 15.0)
    proc = _run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-read_intervals", f"{lo:.3f}%",
         "-show_entries", "packet=pts_time,dts_time,duration_time", "-of", "json", str(path)],
        timeout,
    )
    if proc.returncode != 0:
        return None
    end = None
    durs = []
    for p in json.loads(proc.stdout or b"{}").get("packets", []):
        t = _num(p.get("pts_time")) if p.get("pts_time") not in (None, "N/A") else _num(p.get("dts_time"))
        d = _num(p.get("duration_time"))
        if d:
            durs.append(d)
        if t is not None:
            cand = t + (d or 0.0)
            end = cand if end is None else max(end, cand)
    if end is not None and not durs:
        return None  # no per-packet durations: caller falls back to container duration
    return end


def grab_frame_jpeg(ffmpeg: str, path_or_url: str, at_s: float | None, width: int = 320,
                    timeout: float = 15.0, rtsp: bool = False) -> bytes:
    args = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error"]
    if rtsp:
        args += ["-rtsp_transport", "tcp", "-timeout", "5000000"]
    if at_s is not None and at_s > 0:
        args += ["-ss", f"{at_s:.3f}"]
    args += ["-i", path_or_url, "-map", "0:v:0", "-an", "-sn", "-frames:v", "1",
             "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1"]
    proc = _run(args, timeout)
    if proc.returncode != 0 or not proc.stdout:
        msg = proc.stderr.decode(errors="replace").strip().splitlines()
        raise ProbeError(msg[-1] if msg else "could not extract a frame")
    return proc.stdout


@lru_cache(maxsize=4)
def available_decoders(ffmpeg: str) -> frozenset[str]:
    try:
        proc = _run([ffmpeg, "-hide_banner", "-decoders"], 15)
    except ProbeError:
        return frozenset()
    names = set()
    for line in proc.stdout.decode(errors="replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] == "V":
            names.add(parts[1])
    # map decoder implementations to codec names ffprobe reports
    aliases = {"libdav1d": "av1", "libaom-av1": "av1", "av1": "av1", "hevc": "hevc",
               "vp9": "vp9", "libvpx-vp9": "vp9", "h264": "h264", "mpeg4": "mpeg4",
               "mpeg2video": "mpeg2video", "mjpeg": "mjpeg", "vp8": "vp8"}
    return frozenset(names | {aliases[n] for n in names if n in aliases})


@lru_cache(maxsize=4)
def has_encoder(ffmpeg: str, name: str) -> bool:
    try:
        proc = _run([ffmpeg, "-hide_banner", "-encoders"], 15)
    except ProbeError:
        return False
    return any(line.split()[1:2] == [name] for line in proc.stdout.decode(errors="replace").splitlines()
               if line.strip())


def ffmpeg_version(ffmpeg: str) -> str | None:
    try:
        proc = _run([ffmpeg, "-hide_banner", "-version"], 10)
    except ProbeError:
        return None
    first = proc.stdout.decode(errors="replace").splitlines()[:1]
    return first[0] if first else None
