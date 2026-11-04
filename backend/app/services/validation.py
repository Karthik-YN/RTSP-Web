"""Pure validation helpers: stream names, timestamps and segments.

These never touch the filesystem or processes, so they are unit-tested
exhaustively and reused by the API (authoritative) and mirrored in the UI
(convenience only).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


class ValidationError(ValueError):
    """A user input problem; message is safe to show in the UI."""

    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.field = field


# ----------------------------------------------------------------- names

def validate_stream_name(name: str, pattern: str, reserved: list[str]) -> str:
    if name is None or name == "":
        raise ValidationError("Stream name is required.", "name")
    if name != name.strip():
        raise ValidationError("Stream name must not start or end with spaces.", "name")
    if not re.fullmatch(pattern, name):
        bad = sorted({c for c in name if not re.fullmatch(r"[A-Za-z0-9_-]", c)})
        if len(name) > 64:
            raise ValidationError("Stream name must be at most 64 characters.", "name")
        shown = " ".join(repr(c) for c in bad) if bad else ""
        raise ValidationError(
            "Stream name may only contain letters, digits, '_' and '-' "
            f"(no spaces, slashes or special characters). Invalid: {shown}".rstrip(": "),
            "name",
        )
    if name.lower() in {r.lower() for r in reserved}:
        raise ValidationError(f"'{name}' is a reserved name; choose another.", "name")
    return name


# ------------------------------------------------------------- timestamps

_TS_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")


def parse_timestamp(value: str | float | int | None, field: str = "time") -> float | None:
    """Parse ``HH:MM:SS(.mmm)``, ``MM:SS`` or plain seconds.

    Two-part values follow FFmpeg's convention (MM:SS), e.g. ``01:30`` is
    90 seconds. The UI always shows the interpreted value back to the user.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, bool) or not math.isfinite(value):
            raise ValidationError("Time must be a finite number of seconds.", field)
        if value < 0:
            raise ValidationError("Time cannot be negative.", field)
        return float(value)
    text = str(value).strip()
    if text == "":
        return None
    if text.startswith("-"):
        raise ValidationError("Time cannot be negative.", field)
    m = _TS_RE.match(text)
    if not m:
        raise ValidationError(
            f"Invalid time '{text}'. Use HH:MM:SS, MM:SS or seconds (e.g. 01:02:03 or 125.5).",
            field,
        )
    parts = [p for p in m.groups() if p is not None]
    nums = [float(p) for p in parts]
    if len(nums) == 3:
        h, mnt, s = nums
    elif len(nums) == 2:
        h, mnt, s = 0.0, nums[0], nums[1]
    else:
        h, mnt, s = 0.0, 0.0, nums[0]
    if len(nums) >= 2 and (s >= 60 or (len(nums) == 3 and mnt >= 60)):
        raise ValidationError(f"Invalid time '{text}': minutes and seconds must be below 60.", field)
    return h * 3600 + mnt * 60 + s


def format_timestamp(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    ms_total = int(round(seconds * 1000))
    h, rem = divmod(ms_total, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    base = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}" if ms else base


# --------------------------------------------------------------- segments

@dataclass(frozen=True)
class Segment:
    start_s: float
    end_s: float | None  # None = until end of file
    duration_s: float  # length of one pass of the segment


def validate_segment(
    start_s: float | None,
    end_s: float | None,
    file_duration_s: float | None,
    min_segment_s: float,
) -> Segment:
    """Validate a requested [start, end) range against the file duration."""
    start = 0.0 if start_s is None else float(start_s)
    if start < 0:
        raise ValidationError("Start time cannot be negative.", "start")
    if file_duration_s is None or file_duration_s <= 0:
        raise ValidationError("The video's duration is unknown, so a segment cannot be validated.", "video_id")
    dur = float(file_duration_s)
    if start >= dur:
        raise ValidationError(
            f"Start time {format_timestamp(start)} is beyond the end of the file "
            f"(duration {format_timestamp(dur)}).",
            "start",
        )
    if end_s is not None:
        end = float(end_s)
        if end <= start:
            raise ValidationError(
                f"End time {format_timestamp(end)} must be after start time {format_timestamp(start)}.",
                "end",
            )
        if end > dur + 1e-3:
            raise ValidationError(
                f"End time {format_timestamp(end)} is beyond the end of the file "
                f"(duration {format_timestamp(dur)}).",
                "end",
            )
        end = min(end, dur)
    else:
        end = None
    seg_len = (end if end is not None else dur) - start
    if seg_len < min_segment_s:
        raise ValidationError(
            f"The selected segment is only {seg_len:.3f}s long; the minimum is {min_segment_s:g}s.",
            "end" if end is not None else "start",
        )
    return Segment(start_s=start, end_s=end, duration_s=seg_len)
