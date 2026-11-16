"""Picture shape: the size a viewer actually sees, and whether it is 16:9.

The stored width x height is not always what is shown: phone clips are often
stored landscape with a rotation flag, and some encoders use non-square
pixels (sample aspect ratio). Both are applied here, from the probe data
kept on the video.
"""

from __future__ import annotations

import re

SIXTEEN_NINE = 16 / 9
TOLERANCE = 0.02  # 1366x768 (1.779) and 854x480 (1.779) are 16:9


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                return int(float(sd["rotation"])) % 360
            except (TypeError, ValueError):
                pass
    try:
        return int(float((stream.get("tags") or {}).get("rotate", 0))) % 360
    except (TypeError, ValueError):
        return 0


def _sar(stream: dict) -> float:
    m = re.fullmatch(r"(\d+):(\d+)", str(stream.get("sample_aspect_ratio") or ""))
    if not m or int(m.group(1)) == 0 or int(m.group(2)) == 0:
        return 1.0
    return int(m.group(1)) / int(m.group(2))


def _video_stream(probe_json: dict | None) -> dict:
    for st in (probe_json or {}).get("streams") or []:
        if st.get("codec_type") == "video" and not (st.get("disposition") or {}).get("attached_pic"):
            return st
    return {}


def display_size(width: int | None, height: int | None, probe_json: dict | None = None) -> tuple[int, int] | None:
    """(width, height) as displayed: non-square pixels and rotation applied."""
    if not width or not height:
        return None
    st = _video_stream(probe_json)
    w = int(round(width * _sar(st)))
    h = int(height)
    if _rotation(st) in (90, 270):
        w, h = h, w
    return w, h


def shape_of(w: int, h: int) -> str:
    r = w / h
    if abs(r / SIXTEEN_NINE - 1) <= TOLERANCE:
        return "16:9"
    if r < 0.95:
        return "portrait"
    if r <= 1.05:
        return "square"
    if r <= 1.55:
        return "4:3"  # boxy CCTV frames: 5:4, 4:3, 768x544 (1.41), 3:2
    return "other"  # 16:10, ultra-wide...


SHAPE_LABEL = {"portrait": "portrait", "square": "square", "4:3": "4:3", "other": "wide/other", "16:9": "16:9"}


def video_shape(v) -> dict:
    """Shape fields for a Video row (any object with width, height, probe_json)."""
    size = display_size(v.width, v.height, getattr(v, "probe_json", None))
    if size is None:
        return {"display_width": None, "display_height": None, "shape": None, "is_16_9": None}
    w, h = size
    shape = shape_of(w, h)
    return {"display_width": w, "display_height": h, "shape": shape, "is_16_9": shape == "16:9"}


def parse_size(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"\s*(\d{2,4})\s*[x×]\s*(\d{2,4})\s*", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def fit_filter(w: int, h: int, fit: str = "pad") -> str:
    """FFmpeg -vf chain that turns any input into exactly w x h, square pixels.

    pad: whole picture, black bars; crop: fill the frame, cut the overflow
    (centred); blur: whole picture over a blurred, zoomed copy of itself.
    A 16:9 input to a 16:9 size is a plain scale with every mode.
    """
    norm = "scale=trunc(iw*sar/2)*2:ih,setsar=1"  # undo non-square pixels first
    fit_in = f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos"
    if fit == "crop":
        return (f"{norm},scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
                f"crop={w}:{h},setsar=1")
    if fit == "blur":
        return (f"{norm},split=2[bg][fg];"
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[b];"
                f"[fg]{fit_in}[f];[b][f]overlay=(W-w)/2:(H-h)/2,setsar=1")
    return f"{norm},{fit_in},pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
