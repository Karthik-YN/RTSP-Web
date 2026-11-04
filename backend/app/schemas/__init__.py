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
    start_now: bool = True
    note: str | None = Field(default=None, max_length=500)
    protected: bool = False


class StreamUpdate(BaseModel):
    name: str | None = None
    video_id: int | None = None
    start: TimeValue = None
    end: TimeValue = None
    clear_end: bool = False
    mode: Literal["loop", "once"] | None = None
    force_transcode: bool | None = None
    note: str | None = Field(default=None, max_length=500)
    protected: bool | None = None
    owner: str | None = Field(default=None, max_length=64)


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
