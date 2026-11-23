"""ORM models mapped onto the schema created by database/migrations.py."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """ISO-8601 text column that always round-trips timezone-aware UTC."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        return value.isoformat() if value is not None else None

    def process_result_value(self, value: str | None, dialect):
        return datetime.fromisoformat(value) if value else None


class JSONText(TypeDecorator):
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect):
        return json.dumps(value) if value is not None else None

    def process_result_value(self, value: str | None, dialect):
        return json.loads(value) if value else None


class Base(DeclarativeBase):
    pass


class StreamStatus:
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    RESTARTING = "RESTARTING"
    ERROR = "ERROR"
    COMPLETED = "COMPLETED"
    # Lead-in: the stream's slate is on its URL, the video comes after
    LEAD_IN = "LEAD_IN"  # counting down to the video
    WAITING_FOR_VIEWER = "WAITING_FOR_VIEWER"  # hold mode: slate until a reader connects
    ALL = (STOPPED, STARTING, RUNNING, STOPPING, RESTARTING, ERROR, COMPLETED, LEAD_IN, WAITING_FOR_VIEWER)


class DesiredState:
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


class VideoStatus:
    READY = "READY"
    INVALID = "INVALID"
    MISSING = "MISSING"


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rel_path: Mapped[str] = mapped_column(Text, unique=True)
    size_bytes: Mapped[int] = mapped_column(Integer)
    mtime_ns: Mapped[int] = mapped_column(Integer)
    inode: Mapped[int] = mapped_column(Integer)
    device: Mapped[int] = mapped_column(Integer)
    quick_hash: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    container: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    fps: Mapped[float | None] = mapped_column(Float)
    fps_text: Mapped[str | None] = mapped_column(Text)
    is_vfr: Mapped[bool] = mapped_column(Integer, default=0)
    video_codec: Mapped[str | None] = mapped_column(Text)
    video_profile: Mapped[str | None] = mapped_column(Text)
    pix_fmt: Mapped[str | None] = mapped_column(Text)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    field_order: Mapped[str | None] = mapped_column(Text)
    bitrate_bps: Mapped[int | None] = mapped_column(Integer)
    audio_codec: Mapped[str | None] = mapped_column(Text)
    keyframe_interval_s: Mapped[float | None] = mapped_column(Float)
    compat_mode: Mapped[str | None] = mapped_column(Text)
    compat_reasons: Mapped[list] = mapped_column(JSONText, default=list)
    compat_warnings: Mapped[list] = mapped_column(JSONText, default=list)
    probe_json: Mapped[dict | None] = mapped_column(JSONText)
    thumbnail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # Set for files uploaded through the dashboard/API; NULL for files copied onto the server.
    uploaded_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    uploaded_from: Mapped[str | None] = mapped_column(Text, nullable=True)  # client IP
    uploaded_agent: Mapped[str | None] = mapped_column(Text, nullable=True)  # browser User-Agent
    # naming convention: the file name before upload/rename, and every earlier name (newest last)
    original_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_names: Mapped[list | None] = mapped_column(JSONText, nullable=True)

    def fingerprint(self) -> dict:
        return {
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "inode": self.inode,
            "device": self.device,
            "quick_hash": self.quick_hash,
        }


class Stream(Base):
    __tablename__ = "streams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    source_rel_path: Mapped[str] = mapped_column(Text)
    source_fingerprint: Mapped[dict] = mapped_column(JSONText)
    start_offset_s: Mapped[float] = mapped_column(Float, default=0.0)
    effective_start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_offset_s: Mapped[float | None] = mapped_column(Float)
    playback_mode: Mapped[str] = mapped_column(Text)
    playback_speed: Mapped[float] = mapped_column(Float, default=1.0)
    force_transcode: Mapped[bool] = mapped_column(Integer, default=0)
    output_mode: Mapped[str | None] = mapped_column(Text)
    desired_state: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    status_detail: Mapped[str | None] = mapped_column(Text)
    pid: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    loop_count: Mapped[int] = mapped_column(Integer, default=0)
    restart_count: Mapped[int] = mapped_column(Integer, default=0)
    ffmpeg_command: Mapped[list | None] = mapped_column(JSONText)
    config_json: Mapped[dict] = mapped_column(JSONText, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    created_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_by: Mapped[str | None] = mapped_column(Text)
    last_start_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_stop_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    running_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    owner: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    protected: Mapped[bool] = mapped_column(Integer, default=0)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("stream_groups.id", ondelete="SET NULL"))
    slot_id: Mapped[int | None] = mapped_column(ForeignKey("analytics_slots.id", ondelete="SET NULL"))
    lead_in_mode: Mapped[str] = mapped_column(Text, default="none")
    lead_in_s: Mapped[float] = mapped_column(Float, default=5.0)
    hold_max_s: Mapped[float | None] = mapped_column(Float)


class AnalyticsCategory(Base):
    __tablename__ = "analytics_categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Analytics(Base):
    """One analytics use case (e.g. PPE) shown in the left taskbar."""

    __tablename__ = "analytics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("analytics_categories.id", ondelete="RESTRICT"))
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # WHAT part of video names (services/naming.py), e.g. ALPR; filled in for every analytics.
    code: Mapped[str | None] = mapped_column(Text, nullable=True)


class AnalyticsSlot(Base):
    """A video placeholder inside an analytics: title, description and (optionally) a video."""

    __tablename__ = "analytics_slots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    analytics_id: Mapped[int] = mapped_column(ForeignKey("analytics.id", ondelete="RESTRICT"))
    title: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime)


class StreamGroup(Base):
    __tablename__ = "stream_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    level: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    stream_id: Mapped[int | None] = mapped_column(Integer)
    stream_name: Mapped[str | None] = mapped_column(Text)
    username: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    data_json: Mapped[dict | None] = mapped_column(JSONText)


class StatusHistory(Base):
    """One row per status change of a stream (for uptime timelines)."""

    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    stream_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
