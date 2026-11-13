"""Typed request/response schemas (documented in the OpenAPI spec at /docs)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ErrorOut(BaseModel):
    detail: str
    field: str | None = None


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    username: str
    role: str
    can_write: bool
    is_admin: bool = False
    auth_enabled: bool


Role = Literal["admin", "operator", "viewer"]


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    role: Role = "operator"
    password: str = Field(min_length=8, max_length=256)


class UserUpdateIn(BaseModel):
    role: Role | None = None
    password: str | None = Field(default=None, min_length=8, max_length=256)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class VideoOut(BaseModel):
    id: int
    rel_path: str
    filename: str
    size_bytes: int
    status: str
    error: str | None
    container: str | None
    duration_s: float | None
    width: int | None
    height: int | None
    fps: float | None
    fps_text: str | None
    is_vfr: bool
    video_codec: str | None
    video_profile: str | None
    pix_fmt: str | None
    bit_depth: int | None
    audio_codec: str | None
    bitrate_bps: int | None
    keyframe_interval_s: float | None
    compat_mode: str | None
    compat_reasons: list[str]
    compat_warnings: list[str]
    thumbnail_url: str | None
    streams_using: int = 0
    updated_at: datetime
    last_seen_at: datetime
    browser_playable: bool = True
    browser_note: str | None = None
    convert_reasons: list[str] = []
    converting: dict | None = None  # the running/queued conversion job, if any
    display_width: int | None = None  # as shown: rotation and non-square pixels applied
    display_height: int | None = None
    shape: str | None = None  # 16:9 | portrait | 4:3 | square | other
    is_16_9: bool | None = None
    copy_720p_id: int | None = None  # the ready "(720p)" copy of this video, if any
    copy_16x9_id: int | None = None  # the ready "(720p 16x9)" copy of a non-16:9 video, if any
    # Set when uploaded through the dashboard/API (None: copied onto the server)
    uploaded_by: str | None = None  # user name ("anonymous" when login is off)
    uploaded_at: datetime | None = None
    uploaded_from: str | None = None  # client IP
    uploaded_device: str | None = None  # e.g. "Windows · Chrome"


class ScanOut(BaseModel):
    added: int
    updated: int
    missing: int
    renamed: int
    settling: list[str]
    files: int
    seconds: float


class UploadCheckIn(BaseModel):
    filename: str = Field(min_length=1, max_length=512)
    size: int = Field(gt=0)


class UploadCheckOut(BaseModel):
    filename: str
    exists: bool
    ok: bool
    required: int
    free: int
    reserve: int
    in_flight_uploads: int
    available: int


TimeValue = str | float | None


class StreamCreate(BaseModel):
    name: str = Field(description="Unique path name: letters, digits, '_' and '-' (max 64).")
    video_id: int
    start: TimeValue = Field(default=None, description="HH:MM:SS(.mmm), MM:SS or seconds")
    end: TimeValue = Field(default=None, description="Optional end; empty = end of file")
    mode: Literal["loop", "once"] = "loop"
    force_transcode: bool = False
    output_size: str | None = Field(default=None, max_length=20,
                                    description="standard (RTSPM_OUTPUT_WIDTH x HEIGHT), original, or WxH from "
                                                "RTSPM_STANDARD_SIZES (default: RTSPM_OUTPUT_DEFAULT)")
    start_now: bool = True
    note: str | None = Field(default=None, max_length=500)
    protected: bool = False
    slot_id: int | None = Field(default=None, description="Analytics video placeholder this stream runs")
    lead_in_mode: Literal["none", "fixed", "hold"] | None = Field(
        default=None, description="Slate before the video: none, fixed N s, or hold until a viewer + N s "
                                  "(default: RTSPM_DEFAULT_LEAD_IN_MODE)")
    lead_in_s: float | None = Field(default=None, ge=0, le=120)
    hold_max_s: float | None = Field(default=None, gt=0, le=7 * 86400,
                                     description="Hold mode: start the video anyway after this many seconds")


class StreamUpdate(BaseModel):
    name: str | None = None
    video_id: int | None = None
    start: TimeValue = None
    end: TimeValue = None
    clear_end: bool = False
    mode: Literal["loop", "once"] | None = None
    force_transcode: bool | None = None
    output_size: str | None = Field(default=None, max_length=20)
    note: str | None = Field(default=None, max_length=500)
    protected: bool | None = None
    owner: str | None = Field(default=None, max_length=64)
    lead_in_mode: Literal["none", "fixed", "hold"] | None = None
    lead_in_s: float | None = Field(default=None, ge=0, le=120)
    hold_max_s: float | None = Field(default=None, gt=0, le=7 * 86400)
    clear_hold_max: bool = False


class StreamValidateOut(BaseModel):
    ok: bool
    errors: list[dict] = []
    start_s: float | None = None
    effective_start_s: float | None = None
    end_s: float | None = None
    effective_end_s: float | None = None
    period_s: float | None = None
    output_mode: str | None = None
    warnings: list[str] = []
    rtsp_url: str | None = None


class StreamOut(BaseModel):
    id: int
    name: str
    rtsp_url: str
    video_id: int | None
    source_rel_path: str
    start_offset_s: float
    effective_start_s: float
    end_offset_s: float | None
    effective_end_s: float | None
    playback_mode: str
    playback_speed: float
    force_transcode: bool
    output_mode: str | None
    output_size: str = "original"
    output_resolution: str | None = None  # what readers get, e.g. 1280x720
    desired_state: str
    status: str
    status_detail: str | None
    pid: int | None
    last_error: str | None
    loop_count: int
    restart_count: int
    version: int
    created_at: datetime
    created_by: str | None
    updated_at: datetime
    updated_by: str | None
    last_start_at: datetime | None
    last_stop_at: datetime | None
    running_since: datetime | None
    owner: str | None = None
    note: str | None = None
    protected: bool = False
    group_id: int | None = None
    slot_id: int | None = None
    lead_in_mode: str = "none"
    lead_in_s: float = 5.0
    hold_max_s: float | None = None
    warnings: list[str] = []
    runtime: dict[str, Any] = {}


class StreamDetailOut(StreamOut):
    ffmpeg_command: list[str] | None
    ffmpeg_command_text: str | None
    config: dict[str, Any]
    video: VideoOut | None
    log_tail: list[str]
    events: list[dict]


class LogsOut(BaseModel):
    stream_id: int
    lines: list[str]


class EventOut(BaseModel):
    id: int
    ts: datetime
    level: str
    kind: str
    stream_id: int | None
    stream_name: str | None
    username: str | None
    message: str
    data: dict | None = None


class MessageOut(BaseModel):
    message: str
    status: dict[str, Any] | None = None
