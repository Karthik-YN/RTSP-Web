"""Codec compatibility policy (EC-01).

Decides, for a probed video, whether it can be published by stream copy,
must be transcoded to the configured H.264 output profile, or must be
refused. The edge's real decoder capabilities are *not assumed*: the copy
whitelist (codec, pixel format, profile, resolution, fps) is configuration
and must be confirmed against the 5.x edge (see docs/EDGE_VALIDATION.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings

COPY = "copy"
TRANSCODE = "transcode"
REJECT = "reject"


@dataclass
class CompatResult:
    mode: str
    reasons: list[str] = field(default_factory=list)  # why copy is not possible / refused
    warnings: list[str] = field(default_factory=list)


def b_frames(probe_json: dict | None) -> bool | None:
    """B-frames in the video track of a stored probe. Real cameras never send them; some edge
    decoders then show frames out of order. None: unknown (not probed yet)."""
    streams = (probe_json or {}).get("streams") or []
    video = next((x for x in streams if x.get("codec_type") == "video"), None)
    if video is None or video.get("has_b_frames") is None:
        return None
    return int(video["has_b_frames"]) > 0


def evaluate(meta: dict, settings: Settings, decoders: frozenset[str],
             x264_available: bool, force_transcode: bool = False,
             extra_copy_codecs: tuple[str, ...] = ()) -> CompatResult:
    """``meta`` holds the probe fields (video_codec, pix_fmt, ...)."""
    codec = meta.get("video_codec")
    if not codec:
        return CompatResult(REJECT, ["The file has no video stream."])

    reasons: list[str] = []
    warnings: list[str] = []
    profile = meta.get("video_profile")
    pix_fmt = meta.get("pix_fmt")
    depth = meta.get("bit_depth")
    w, h = meta.get("width") or 0, meta.get("height") or 0
    fps = meta.get("fps")

    copy_codecs = [*settings.copy_codecs, *extra_copy_codecs]
    if codec not in copy_codecs:
        reasons.append(f"Codec {codec.upper()} is not in the edge copy list ({', '.join(copy_codecs)}).")
    if depth is not None and depth != 8:
        reasons.append(f"{depth}-bit video; the output profile requires 8-bit.")
    if pix_fmt and pix_fmt not in settings.copy_pix_fmts:
        reasons.append(f"Pixel format {pix_fmt} is not allowed for copy ({', '.join(settings.copy_pix_fmts)}).")
    if codec in settings.copy_codecs and profile and profile not in settings.copy_profiles:
        reasons.append(f"H.264 profile '{profile}' is not allowed for copy ({', '.join(settings.copy_profiles)}).")
    if w > settings.max_width or h > settings.max_height:
        reasons.append(f"Resolution {w}x{h} exceeds {settings.max_width}x{settings.max_height}.")
    if fps and fps > settings.max_fps + 0.01:
        reasons.append(f"Frame rate {fps:.3f} exceeds {settings.max_fps:g} fps.")

    if meta.get("has_b_frames"):
        warnings.append("The video has B-frames; real cameras don't, and some edges show frames out of order. "
                        "Make a camera copy in the library (no B-frames, keyframe every 2 s).")
    if meta.get("audio_codec"):
        warnings.append(f"Audio track ({meta['audio_codec']}) will be dropped.")
    if meta.get("is_vfr"):
        warnings.append("Variable frame rate detected; source timestamps are preserved (no frame-rate conversion).")
    if meta.get("field_order") not in (None, "progressive", "unknown"):
        warnings.append(f"Interlaced video ({meta['field_order']}); verify the edge handles it.")

    if not reasons and not force_transcode and settings.transcode_policy != "always":
        kfi = meta.get("keyframe_interval_s")
        if kfi and kfi > settings.keyframe_warn_interval_s:
            warnings.append(
                f"Keyframe interval is about {kfi:.1f}s: start positions snap to keyframes and a "
                f"reader may wait up to {kfi:.1f}s for its first picture."
            )
        return CompatResult(COPY, reasons, warnings)

    if force_transcode or settings.transcode_policy == "always":
        reasons = reasons or ["Transcoding was requested."]
    if settings.transcode_policy == "never" and not force_transcode:
        return CompatResult(REJECT, reasons + ["Transcoding is disabled (RTSPM_TRANSCODE_POLICY=never)."], warnings)
    if codec not in decoders:
        return CompatResult(REJECT, reasons + [f"This FFmpeg build cannot decode {codec}."], warnings)
    if not x264_available:
        return CompatResult(REJECT, reasons + ["libx264 encoder is not available in this FFmpeg build."], warnings)
    warnings.insert(0, "Will be transcoded to H.264 on the fly: this costs CPU (roughly one core per 1080p stream).")
    if w > settings.transcode_max_width or h > settings.transcode_max_height:
        warnings.append(
            f"Output will be scaled down to fit {settings.transcode_max_width}x{settings.transcode_max_height}."
        )
    return CompatResult(TRANSCODE, reasons, warnings)
