"""Which H.264 encoder live transcoding uses: CPU, NVIDIA GPU, Intel iGPU (Quick Sync) or VAAPI.

Only streams that are *transcoded* use an encoder (copy-mode streams never re-encode).
Detection does a real 0.2 s test encode with exactly the arguments a stream would use,
so "available" means it actually works in this container on this machine, not just that
FFmpeg was built with it. A laptop without a GPU simply shows CPU only.

Choice: ``auto`` (default: the best one that works, GPU > iGPU > CPU) or a fixed one.
Set it on the Health page (saved in the database) or with ``RTSPM_ENCODER``. A fixed
choice that doesn't work here falls back to CPU, and the page says so.
New and restarted transcoding streams use the new encoder; running ones keep theirs.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import threading
import time

log = logging.getLogger(__name__)

CPU, NVIDIA, INTEL, VAAPI = "cpu", "nvidia", "intel", "vaapi"
CHOICES = ("auto", CPU, NVIDIA, INTEL, VAAPI)
AUTO_ORDER = (NVIDIA, INTEL, VAAPI, CPU)
INFO = {
    CPU: {"label": "CPU (libx264)", "encoder": "libx264",
          "about": "Works everywhere. Uses processor cores; fine for a few transcoded streams."},
    NVIDIA: {"label": "NVIDIA GPU (NVENC)", "encoder": "h264_nvenc",
             "about": "Dedicated graphics card. Many transcoded streams with little CPU."},
    INTEL: {"label": "Intel iGPU (Quick Sync)", "encoder": "h264_qsv",
            "about": "Graphics built into Intel processors (most laptops/mini PCs)."},
    VAAPI: {"label": "iGPU / GPU via VAAPI", "encoder": "h264_vaapi",
            "about": "Generic Linux GPU video API (Intel or AMD graphics)."},
}


def render_node() -> str | None:
    nodes = sorted(glob.glob("/dev/dri/renderD*"))
    return nodes[0] if nodes else None


def nvidia_present() -> bool:
    return os.path.exists("/dev/nvidia0") or os.path.exists("/dev/nvidiactl")


def device_missing_reason(enc: str) -> str | None:
    """Why this encoder can't work here before even trying (no device in the container)."""
    if enc == NVIDIA and not nvidia_present():
        return "No NVIDIA GPU visible to the rig (no /dev/nvidia0)."
    if enc in (INTEL, VAAPI) and render_node() is None:
        return "No GPU render device visible to the rig (no /dev/dri/renderD*)."
    return None


def input_args(enc: str) -> list[str]:
    """Global options that must come before ``-i``."""
    if enc == VAAPI:
        return ["-vaapi_device", render_node() or "/dev/dri/renderD128"]
    if enc == INTEL:
        return ["-init_hw_device", f"qsv=hw:{render_node() or '/dev/dri/renderD128'}", "-filter_hw_device", "hw"]
    return []


def upload_filter(enc: str) -> str | None:
    """Filter that hands frames to the GPU (appended to -vf); None when the encoder takes normal frames."""
    if enc == VAAPI:
        return "format=nv12,hwupload"
    if enc == INTEL:
        return "format=nv12,hwupload=extra_hw_frames=64"
    return None


def encoder_args(enc: str, *, profile: str, gop: int, gop_seconds: float, maxrate_kbps: int, crf: int,
                 preset: str, tune: str, threads: int) -> list[str]:
    """Codec options; the same rules for every encoder: H.264, fixed keyframe interval, capped bitrate."""
    rate = ["-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{maxrate_kbps * 2}k"]
    keys = ["-g", str(gop), "-force_key_frames", f"expr:gte(t,n_forced*{gop_seconds:g})"]
    if enc == NVIDIA:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-profile:v", profile, "-pix_fmt", "yuv420p",
                "-rc", "vbr", "-cq", str(crf), "-b:v", "0", *rate, *keys, "-forced-idr", "1", "-bf", "0"]
    if enc == INTEL:
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-profile:v", profile, "-global_quality", str(crf),
                "-look_ahead", "0", *rate, *keys, "-idr_interval", "0", "-bf", "0"]
    if enc == VAAPI:
        return ["-c:v", "h264_vaapi", "-profile:v", profile, "-rc_mode", "QVBR", "-global_quality", str(crf),
                "-b:v", f"{maxrate_kbps * 3 // 4}k", *rate, *keys, "-idr_interval", "0", "-bf", "0"]
    args = ["-c:v", "libx264", "-preset", preset, "-tune", tune, "-profile:v", profile, "-pix_fmt", "yuv420p",
            "-crf", str(crf), *rate, "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
            "-force_key_frames", f"expr:gte(t,n_forced*{gop_seconds:g})",
            "-bf", "0"]  # no B-frames, like a real camera (whatever the tune)
    if threads > 0:
        args += ["-threads", str(threads)]
    return args


class EncoderService:
    """Detects the encoders that work here and resolves the one to use."""

    def __init__(self, settings, runtime_settings=None):
        self.s = settings
        self.rs = runtime_settings
        self._lock = threading.Lock()
        self.results: dict[str, dict] = {}
        self.checked_at: float | None = None

    # ---------------------------------------------------------------- detection
    def _trial(self, enc: str) -> tuple[bool, str | None, float | None]:
        missing = device_missing_reason(enc)
        if missing:
            return False, missing, None
        s = self.s
        vf = "scale=640:360"
        up = upload_filter(enc)
        if up:
            vf += "," + up
        argv = [s.ffmpeg_bin, "-hide_banner", "-nostdin", "-v", "error", *input_args(enc),
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-t", "0.4", "-vf", vf,
                *encoder_args(enc, profile=s.x264_profile, gop=50, gop_seconds=s.x264_gop_seconds,
                              maxrate_kbps=s.x264_maxrate_kbps, crf=s.x264_crf, preset=s.x264_preset,
                              tune=s.x264_tune, threads=0),
                "-f", "null", "-"]
        t0 = time.monotonic()
        try:
            p = subprocess.run(argv, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"Test encode failed: {type(exc).__name__}", None
        ms = round((time.monotonic() - t0) * 1000)
        if p.returncode != 0:
            err = [ln for ln in p.stderr.decode(errors="replace").strip().splitlines() if ln.strip()]
            return False, "Test encode failed: " + (err[-1][:200] if err else f"exit {p.returncode}"), ms
        return True, None, ms

    def detect(self) -> dict[str, dict]:
        results = {}
        for enc in (CPU, NVIDIA, INTEL, VAAPI):
            ok, reason, ms = self._trial(enc)
            results[enc] = {"id": enc, **INFO[enc], "available": ok, "reason": reason, "test_ms": ms}
        with self._lock:
            self.results, self.checked_at = results, time.time()
        log.info("encoders: %s", ", ".join(f"{k}={'yes' if v['available'] else 'no'}" for k, v in results.items()))
        return results

    # ---------------------------------------------------------------- choice
    def choice(self) -> str:
        if self.rs is not None:
            v = self.rs.get_str("encoder")
            if v in CHOICES:
                return v
        return self.s.encoder if self.s.encoder in CHOICES else "auto"

    def available(self, enc: str) -> bool:
        if not self.results:
            return enc == CPU
        return bool(self.results.get(enc, {}).get("available"))

    def active(self) -> str:
        """The encoder new transcoding streams use now."""
        want = self.choice()
        if want == "auto":
            return next((e for e in AUTO_ORDER if self.available(e)), CPU)
        return want if self.available(want) else CPU

    def view(self) -> dict:
        if not self.results:
            self.detect()
        want, act = self.choice(), self.active()
        note = None
        if want != "auto" and act != want:
            note = f"{INFO[want]['label']} was chosen but doesn't work here; using {INFO[act]['label']}."
        return {"choice": want, "active": act, "active_label": INFO[act]["label"], "note": note,
                "choices": list(CHOICES), "encoders": [self.results[e] for e in (CPU, NVIDIA, INTEL, VAAPI)],
                "checked_at": self.checked_at,
                "default": self.s.encoder if self.s.encoder in CHOICES else "auto",
                "overridden": bool(self.rs and self.rs.get_str("encoder") in CHOICES)}
