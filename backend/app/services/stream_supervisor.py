"""Stream lifecycle supervisor.

Owns every FFmpeg publisher process. Guarantees:

* one publisher per stream: every lifecycle operation for a stream runs under
  that stream's asyncio.Lock and always stops the old process before a new one
  is spawned; stale exit notifications are ignored by identity check;
* desired state (what users asked for, persisted) is kept separate from the
  runtime status (what is actually happening);
* RUNNING is only reported once MediaMTX says the path is ready;
* unexpected exits are classified: play-once completion, loop playlist
  rollover, invalid source (stop + ERROR), MediaMTX down (wait for it),
  other failures (retry with exponential backoff, ERROR after N failures);
* optimistic concurrency: config/desired-state changes bump ``version`` and
  callers may pass the version they saw (HTTP If-Match) to avoid silently
  overwriting another user's change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..database import Database, utcnow
from ..models import DesiredState, StatusHistory, Stream, StreamStatus, Video, VideoStatus
from . import compatibility
from .errors import CapacityExceeded, Conflict, Invalid, NotFound
from .events import EventLog
from .ffmpeg_manager import (LogSink, PublishPlan, PublisherProcess, build_command, loop_entries,
                             transcode_args, write_playlist)
from .ffprobe_service import ProbeError, keyframe_at_or_before, video_end_time
from .file_manager import LibraryError, describe_change, fingerprint, public_error
from .mediamtx_service import DOWN, UP, MediaMTXService
from .runtime_settings import RuntimeSettings
from .shape import SHAPE_LABEL, parse_size, video_shape
from .slate_service import SlateService, SlateSpec, fps_text_of, profile_of
from .start_gate import StartGate, Ticket, phase_delay
from .validation import ValidationError, validate_segment, validate_stream_name
from .video_library import VideoLibrary

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {StreamStatus.STARTING, StreamStatus.RUNNING, StreamStatus.RESTARTING, StreamStatus.STOPPING,
                   StreamStatus.LEAD_IN, StreamStatus.WAITING_FOR_VIEWER}
LEAD_IN_STATUSES = {StreamStatus.LEAD_IN, StreamStatus.WAITING_FOR_VIEWER}


@dataclass
class StreamConfig:
    video_id: int
    start_s: float | None = 0.0
    end_s: float | None = None
    mode: str = "loop"
    force_transcode: bool = False
    # "standard" (RTSPM_OUTPUT_WIDTH x HEIGHT), "original", or "WxH" from RTSPM_STANDARD_SIZES
    output_size: str = "original"
    # New streams and video changes must be 16:9; restarting an existing stream is not refused.
    enforce_shape: bool = True


@dataclass
class Runtime:
    stream_id: int
    sink: LogSink
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    publisher: PublisherProcess | None = None
    plan: PublishPlan | None = None
    stop_requested: bool = False
    base_loops: int = 0
    failures: int = 0
    retry_task: asyncio.Task | None = None
    ready_task: asyncio.Task | None = None
    waiter_task: asyncio.Task | None = None
    waiting_for_mediamtx: bool = False
    not_ready_since: float | None = None
    running_since_mono: float | None = None
    fingerprint: dict | None = None
    last_source_check: float = 0.0
    persisted_loops: int = -1
    ready_at: datetime | None = None
    spawned_at_mono: float | None = None
    ticket: Ticket | None = None  # waiting in the start queue
    launch_wall: float | None = None  # wall clock when the publisher was spawned
    origin_wall: float | None = None  # wall clock when it showed its first frame (best estimate)
    spacing_warning: str | None = None
    lead_task: asyncio.Task | None = None  # lead-in in progress (slate on the URL, video not started)
    lead: dict | None = None  # what the lead-in is doing, for the UI
    go_event: asyncio.Event | None = None  # "Start video now"
    slate_up: asyncio.Event | None = None  # set once the slate is on the URL
    begun_mono: float | None = None  # when the user (or recovery) asked for the start: queue order


class StreamSupervisor:
    def __init__(self, settings: Settings, db: Database, mediamtx: MediaMTXService,
                 library: VideoLibrary, events: EventLog):
        self.s = settings
        self.db = db
        self.mtx = mediamtx
        self.library = library
        self.events = events
        self.runtimes: dict[int, Runtime] = {}
        self.limits = RuntimeSettings(db, settings)
        self._create_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None
        self._start_sem = asyncio.Semaphore(settings.recovery_concurrency)
        self._transcode_sem = asyncio.Semaphore(settings.recovery_transcode_concurrency)
        self._shutting_down = False
        # Seconds from spawning a publisher to the moment its first frame counts as
        # sent (start-up minus FFmpeg's initial read burst); learnt from real starts.
        self.origin_lag_s = 0.3
        self.slates = SlateService(settings)
        self._last_slate_check = 0.0
        self.gate = StartGate(settings.start_spacing_s, settings.start_ready_wait_s,
                              launch=self._gate_launch, phase=self._gate_phase)
        # True until boot recovery finished: recovery alone starts/adopts streams
        # then, so the MediaMTX 'up' handler cannot race it (double publishers).
        self.recovering = True
        mediamtx.on_down.append(self.on_mediamtx_down)
        mediamtx.on_up.append(self.on_mediamtx_up)

    # ================================================================ helpers
    def runtime(self, stream_id: int) -> Runtime:
        rt = self.runtimes.get(stream_id)
        if rt is None:
            sink = LogSink(self.s.log_path / "streams" / f"{stream_id}.log",
                           self.s.stream_log_max_bytes, self.s.stream_log_backups)
            rt = Runtime(stream_id=stream_id, sink=sink)
            self.runtimes[stream_id] = rt
        return rt

    def get_row(self, stream_id: int) -> Stream:
        with self.db.session() as s:
            row = s.get(Stream, stream_id)
            if row is None:
                raise NotFound(f"Stream {stream_id} does not exist (it may have been deleted by another user).")
            return row

    def _update(self, stream_id: int, **fields) -> Stream | None:
        with self.db.session() as s:
            row = s.get(Stream, stream_id)
            if row is None:
                return None
            if "status" in fields and fields["status"] != row.status:
                s.add(StatusHistory(stream_id=stream_id, ts=utcnow(), status=fields["status"],
                                    detail=fields.get("status_detail")))
            for k, v in fields.items():
                setattr(row, k, v)
            return row

    def history(self, stream_id: int, hours: float) -> dict:
        """Status segments over the last ``hours`` and the share of time RUNNING."""
        now = utcnow()
        since_ts = now - timedelta(hours=hours)
        with self.db.session() as s:
            before = s.scalar(select(StatusHistory).where(StatusHistory.stream_id == stream_id,
                                                          StatusHistory.ts < since_ts)
                              .order_by(StatusHistory.ts.desc()).limit(1))
            rows = list(s.scalars(select(StatusHistory).where(StatusHistory.stream_id == stream_id,
                                                              StatusHistory.ts >= since_ts)
                                  .order_by(StatusHistory.ts)))
            created = s.get(Stream, stream_id).created_at
        points = ([(since_ts, before.status)] if before else []) + [(r.ts, r.status) for r in rows]
        start = max(since_ts, created)
        segments = []
        for i, (ts, status) in enumerate(points):
            end = points[i + 1][0] if i + 1 < len(points) else now
            ts = max(ts, start)
            if end > ts:
                segments.append({"status": status, "start": ts.isoformat(), "end": end.isoformat(),
                                 "seconds": round((end - ts).total_seconds(), 1)})
        total = sum(x["seconds"] for x in segments) or 1
        running = sum(x["seconds"] for x in segments if x["status"] == StreamStatus.RUNNING)
        return {"hours": hours, "since": start.isoformat(), "segments": segments,
                "running_percent": round(running / total * 100, 2),
                "changes": len(rows)}

    def prune_history(self, days: float = 7) -> None:
        cutoff = utcnow() - timedelta(days=days)
        with self.db.session() as s:
            s.execute(delete(StatusHistory).where(StatusHistory.ts < cutoff))

    @staticmethod
    def _check_version(row: Stream, expected: int | None) -> None:
        if expected is not None and expected != row.version:
            raise Conflict(
                f"Stream '{row.name}' was changed by {row.updated_by or 'another user'} "
                f"(version {row.version}, you had {expected}). Refresh and try again.",
                {"current_version": row.version, "updated_by": row.updated_by},
            )

    def _counts(self, exclude_id: int | None = None) -> tuple[int, int]:
        with self.db.session() as s:
            q = select(Stream).where(Stream.desired_state == DesiredState.RUNNING)
            rows = [r for r in s.scalars(q) if r.id != exclude_id]
        return len(rows), sum(1 for r in rows if r.output_mode == compatibility.TRANSCODE)

    def _check_capacity(self, will_transcode: bool, exclude_id: int | None = None) -> None:
        active, transcodes = self._counts(exclude_id)
        if active >= self.max_active():
            raise CapacityExceeded(
                f"Capacity reached: {active} streams are already running (limit "
                f"{self.max_active()}, set on the Health page or RTSPM_MAX_ACTIVE_STREAMS). Stop a stream first.")
        if will_transcode and transcodes >= self.max_transcode():
            raise CapacityExceeded(
                f"Transcoding capacity reached: {transcodes} transcoded streams are running (limit "
                f"{self.max_transcode()}). Stop one, or use an H.264 8-bit source that can be copied.")

    def max_active(self) -> int:
        return self.limits.get_int("max_active_streams")

    def max_transcode(self) -> int:
        return self.limits.get_int("max_transcode_streams")

    # ================================================================ config
    def _output_for(self, video: Video, choice: str,
                    enforce_shape: bool = True) -> tuple[Video, tuple[int, int] | None, list[str]]:
        """Apply the 16:9 rule and the output size.

        Returns (video to stream, size to scale to or None for as-is, notes).
        With "standard", a ready 720p copy of the video is used instead when one
        exists, so the stream runs in copy mode.
        """
        from .converter import target_rel  # local: converter imports the library module

        shape = video_shape(video)
        if enforce_shape and self.s.require_16_9 and shape["is_16_9"] is False:
            raise Invalid(
                f"'{video.rel_path}' is {SHAPE_LABEL[shape['shape']]} ({shape['display_width']}x"
                f"{shape['display_height']}), not 16:9. Streams must be 16:9: make a 16:9 copy in the library "
                "first (blurred bars, black bars or crop). The original stays in the library.",
                {"field": "video_id", "shape": shape["shape"]})
        if choice in (None, "", "original"):
            return video, None, []
        if choice == "standard":
            size = (self.s.output_width, self.s.output_height)
        else:
            size = parse_size(choice)
            allowed = {parse_size(x) for x in self.s.standard_sizes} | {(self.s.output_width, self.s.output_height)}
            if size is None or size not in allowed:
                raise Invalid(f"Output size '{choice}' is not allowed (use standard, original or one of "
                              f"{', '.join(self.s.standard_sizes)}).", {"field": "output_size"})
        if (shape["display_width"], shape["display_height"]) == size:
            return video, None, []
        if size == (self.s.output_width, self.s.output_height):
            with self.db.session() as s:
                copy = s.scalar(select(Video).where(Video.rel_path == target_rel(video.rel_path, "720p"),
                                                    Video.status == VideoStatus.READY))
            if copy is not None and (copy.width, copy.height) == size:
                return copy, None, [f"Uses the {size[1]}p copy '{copy.rel_path}' (no live transcoding)."]
        return video, size, []

    async def prepare(self, cfg: StreamConfig) -> dict:
        """Validate a stream configuration against the current file.

        Returns the fields to persist. Raises Invalid with a clear message.
        """
        if cfg.mode not in ("loop", "once"):
            raise Invalid("Playback mode must be 'loop' or 'once'.", {"field": "mode"})
        with self.db.session() as s:
            video = s.get(Video, cfg.video_id)
        if video is None:
            raise Invalid("The selected video does not exist.", {"field": "video_id"})
        if video.status != VideoStatus.READY:
            raise Invalid(f"Video '{video.rel_path}' is not usable: {video.error or video.status}.",
                          {"field": "video_id"})
        video, out_size, notes = self._output_for(video, cfg.output_size, cfg.enforce_shape)
        cfg.video_id = video.id  # may now be the 720p copy (re-reads below must use it)
        try:
            path = self.library.lib.resolve(video.rel_path)
            try:
                fp = await asyncio.to_thread(fingerprint, path)
            except FileNotFoundError:
                # Maybe renamed/moved inside the library since the last scan:
                # rescan once (the scanner follows renames by inode) and retry.
                await self.library.scan()
                with self.db.session() as s:
                    video = s.get(Video, cfg.video_id)
                if video is None or video.status != VideoStatus.READY:
                    raise
                path = self.library.lib.resolve(video.rel_path)
                fp = await asyncio.to_thread(fingerprint, path)
        except (OSError, LibraryError) as exc:
            raise Invalid(f"Video file '{video.rel_path}' cannot be read: "
                          f"{public_error(exc, self.library.lib.root / video.rel_path, video.rel_path)}",
                          {"field": "video_id"})
        if (fp["size_bytes"], fp["mtime_ns"], fp["inode"], fp["quick_hash"]) != (
                video.size_bytes, video.mtime_ns, video.inode, video.quick_hash):
            # File changed since the last scan: re-probe before trusting metadata.
            await self.library.scan()
            with self.db.session() as s:
                video = s.get(Video, cfg.video_id)
            if video is None or video.status != VideoStatus.READY:
                raise Invalid("The video file changed and is no longer usable.", {"field": "video_id"})
            fp = await asyncio.to_thread(fingerprint, path)
        try:
            seg = validate_segment(cfg.start_s, cfg.end_s, video.duration_s, self.s.min_segment_s)
        except ValidationError as exc:
            raise Invalid(str(exc), {"field": exc.field})
        compat = self.library.compat_for(self.library.meta_of(video), cfg.force_transcode or out_size is not None)
        if out_size is not None and compat.mode == compatibility.TRANSCODE:
            compat.warnings.append(f"Scaled to {out_size[0]}x{out_size[1]} live (costs CPU). Make a 720p copy in the "
                                   "library to stream it without transcoding.")
        compat.warnings[:0] = notes
        if compat.mode == compatibility.REJECT:
            raise Invalid("This video cannot be published: " + " ".join(compat.reasons),
                          {"field": "video_id", "reasons": compat.reasons})
        start_time = float((video.probe_json or {}).get("start_time_s") or 0.0)
        window = max(10.0, 2 * (video.keyframe_interval_s or 5.0))

        async def keyframe(rel_t: float):
            try:
                return await asyncio.to_thread(keyframe_at_or_before, self.s.ffprobe_bin, path,
                                               start_time + rel_t, window)
            except ProbeError as exc:
                log.warning("keyframe lookup failed: %s", exc)
                return None

        # Start and end both snap to the keyframe at or before the requested
        # time (see ffmpeg_manager docstring for why this gives clean loops).
        effective = seg.start_s
        if seg.start_s > 0:
            kf = await keyframe(seg.start_s)
            if kf is not None:
                effective = max(0.0, kf[0] - start_time)
        outpoint_abs = None
        if seg.end_s is not None and seg.end_s < video.duration_s - 1e-3:
            kf = await keyframe(seg.end_s)
            if kf is None or kf[0] - start_time <= effective + 1e-6:
                kfi = f" (keyframes are about {video.keyframe_interval_s:.1f}s apart)" if video.keyframe_interval_s else ""
                raise Invalid("There is no keyframe between the start and end times, so this segment cannot be "
                              f"cut cleanly{kfi}. Choose a later end time.", {"field": "end"})
            end_rel = kf[0] - start_time
            outpoint_abs = kf[1]
        else:
            vend = await asyncio.to_thread(video_end_time, self.s.ffprobe_bin, path,
                                           start_time + video.duration_s)
            end_rel = (vend - start_time) if vend else video.duration_s
        if end_rel - effective < self.s.min_segment_s:
            raise Invalid(f"The segment is only {end_rel - effective:.2f}s long once aligned to keyframes; "
                          f"the minimum is {self.s.min_segment_s:g}s.", {"field": "end"})
        return {
            "video_id": video.id,
            "source_rel_path": video.rel_path,
            "source_fingerprint": fp,
            "start_offset_s": seg.start_s,
            "effective_start_s": effective,
            "end_offset_s": seg.end_s,
            "playback_mode": cfg.mode,
            "force_transcode": bool(cfg.force_transcode),
            "output_mode": compat.mode,
            "config_json": {
                "output_size": cfg.output_size,
                "out_width": out_size[0] if out_size else None,
                "out_height": out_size[1] if out_size else None,
                "compat_reasons": compat.reasons,
                "compat_warnings": compat.warnings,
                "container_start_time_s": start_time,
                "file_duration_s": video.duration_s,
                "segment_end_s": end_rel,  # effective end (keyframe-aligned), relative
                "outpoint_abs": outpoint_abs,
                "period_s": end_rel - effective,
                "fps": video.fps, "width": video.width, "height": video.height,
            },
        }

    # ================================================================ public API
    async def create(self, name: str, cfg: StreamConfig, start: bool, user: str | None,
                     note: str | None = None, protected: bool = False, slot_id: int | None = None,
                     lead_in_mode: str | None = None, lead_in_s: float | None = None,
                     hold_max_s: float | None = None) -> Stream:
        try:
            name = validate_stream_name(name, self.s.stream_name_pattern, self.s.reserved_names)
        except ValidationError as exc:
            raise Invalid(str(exc), {"field": "name"})
        fields = await self.prepare(cfg)
        async with self._create_lock:
            with self.db.session() as s:
                existing = s.scalar(select(Stream).where(func.lower(Stream.name) == name.lower()))
                if existing is not None:
                    raise Conflict(f"Stream name '{name}' is already in use (created by "
                                   f"{existing.created_by or 'someone'}). Choose another name.",
                                   {"field": "name"})
            if start:
                self._check_capacity(fields["output_mode"] == compatibility.TRANSCODE)
            now = utcnow()
            try:
                with self.db.session() as s:
                    row = Stream(name=name, playback_speed=1.0,
                                 desired_state=DesiredState.RUNNING if start else DesiredState.STOPPED,
                                 status=StreamStatus.STARTING if start else StreamStatus.STOPPED,
                                 created_at=now, created_by=user, updated_at=now, updated_by=user,
                                 version=1, loop_count=0, owner=user, note=note or None,
                                 protected=bool(protected), slot_id=slot_id,
                                 lead_in_mode=lead_in_mode or self.s.default_lead_in_mode,
                                 lead_in_s=self.s.default_lead_in_s if lead_in_s is None else float(lead_in_s),
                                 hold_max_s=hold_max_s, **fields)
                    s.add(row)
                    s.flush()
                    sid = row.id
                    s.add(StatusHistory(stream_id=sid, ts=now, status=row.status, detail="created"))
            except IntegrityError:
                raise Conflict(f"Stream name '{name}' is already in use. Choose another name.", {"field": "name"})
        self.events.add("stream.created", f"created ({cfg.mode}, start {fields['start_offset_s']:.3f}s)",
                        stream_id=sid, stream_name=name, username=user)
        if start:
            rt = self.runtime(sid)
            async with rt.lock:
                rt.base_loops = 0
                await self._begin(rt)
        return self.get_row(sid)

    async def update(self, stream_id: int, expected_version: int | None, user: str | None,
                     name: str | None = None, cfg: StreamConfig | None = None,
                     meta: dict | None = None) -> Stream:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            changes: dict = {}
            if name is not None and name != row.name:
                if row.desired_state == DesiredState.RUNNING or rt.publisher and rt.publisher.alive():
                    raise Conflict("Stop the stream before renaming it (the RTSP URL changes).")
                try:
                    name = validate_stream_name(name, self.s.stream_name_pattern, self.s.reserved_names)
                except ValidationError as exc:
                    raise Invalid(str(exc), {"field": "name"})
                changes["name"] = name
            if cfg is not None:
                changes.update(await self.prepare(cfg))
                if row.desired_state == DesiredState.RUNNING:
                    self._check_capacity(changes["output_mode"] == compatibility.TRANSCODE, exclude_id=stream_id)
            meta = meta or {}
            if "note" in meta:
                changes["note"] = meta["note"].strip() or None
            if "protected" in meta:
                changes["protected"] = bool(meta["protected"])
            if "owner" in meta:
                changes["owner"] = meta["owner"]
            for k in ("lead_in_mode", "lead_in_s", "hold_max_s"):
                if k in meta and meta[k] != getattr(row, k):
                    changes[k] = meta[k]
            if not changes:
                return row
            config_changed = cfg is not None or "name" in changes
            changes.update(version=row.version + 1, updated_at=utcnow(), updated_by=user)
            if config_changed:
                changes["loop_count"] = 0
            try:
                self._update(stream_id, **changes)
            except IntegrityError:
                raise Conflict(f"Stream name '{name}' is already in use.", {"field": "name"})
            self.events.add("stream.updated", "configuration changed", stream_id=stream_id,
                            stream_name=changes.get("name", row.name), username=user,
                            data={k: v for k, v in changes.items() if k in ("start_offset_s", "end_offset_s",
                                                                             "playback_mode", "name", "note",
                                                                             "protected", "owner", "lead_in_mode",
                                                                             "lead_in_s", "hold_max_s")})
            if config_changed and row.desired_state == DesiredState.RUNNING:
                await self._stop_process(rt, StreamStatus.RESTARTING, "applying new configuration")
                rt.base_loops = 0
                await self._begin(rt)
            return self.get_row(stream_id)

    async def start(self, stream_id: int, expected_version: int | None, user: str | None) -> Stream:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            if rt.publisher and rt.publisher.alive():
                raise Conflict(f"Stream '{row.name}' is already running.")
            await self._revalidate(row)
            row = self.get_row(stream_id)
            self._check_capacity(row.output_mode == compatibility.TRANSCODE, exclude_id=stream_id)
            self._update(stream_id, desired_state=DesiredState.RUNNING, version=row.version + 1,
                         updated_at=utcnow(), updated_by=user, last_error=None, loop_count=0)
            self.events.add("stream.start", "start requested", stream_id=stream_id, stream_name=row.name,
                            username=user)
            rt.base_loops = 0
            rt.failures = 0
            await self._begin(rt)
            return self.get_row(stream_id)

    async def stop(self, stream_id: int, expected_version: int | None, user: str | None) -> Stream:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            self._update(stream_id, desired_state=DesiredState.STOPPED, version=row.version + 1,
                         updated_at=utcnow(), updated_by=user)
            await self._stop_process(rt, StreamStatus.STOPPED, f"stopped by {user or 'user'}")
            await self._clear_slate(row.name)
            if row.status == StreamStatus.COMPLETED and not (rt.publisher and rt.publisher.alive()):
                self._update(stream_id, status=StreamStatus.COMPLETED)
            self.events.add("stream.stop", "stopped", stream_id=stream_id, stream_name=row.name, username=user)
            return self.get_row(stream_id)

    async def restart(self, stream_id: int, expected_version: int | None, user: str | None) -> Stream:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            await self._stop_process(rt, StreamStatus.RESTARTING, f"restart requested by {user or 'user'}")
            await self._revalidate(row)
            row = self.get_row(stream_id)
            self._check_capacity(row.output_mode == compatibility.TRANSCODE, exclude_id=stream_id)
            self._update(stream_id, desired_state=DesiredState.RUNNING, version=row.version + 1,
                         updated_at=utcnow(), updated_by=user, last_error=None, loop_count=0)
            self.events.add("stream.restart", "restart requested", stream_id=stream_id, stream_name=row.name,
                            username=user)
            rt.base_loops = 0
            rt.failures = 0
            await self._begin(rt)
            return self.get_row(stream_id)

    async def delete(self, stream_id: int, expected_version: int | None, user: str | None) -> None:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            await self._stop_process(rt, StreamStatus.STOPPED, "deleting")
            await self._clear_slate(row.name)
            self.slates.prune(stream_id)
            with self.db.session() as s:
                obj = s.get(Stream, stream_id)
                if obj is not None:
                    s.delete(obj)
            # Remove runtime config (playlist) and logs. The source video is never touched.
            shutil.rmtree(self.s.run_path / "streams" / str(stream_id), ignore_errors=True)
            for p in (self.s.log_path / "streams").glob(f"{stream_id}.log*"):
                with contextlib.suppress(OSError):
                    p.unlink()
            self.events.add("stream.deleted", f"deleted (source '{row.source_rel_path}' kept)",
                            stream_id=stream_id, stream_name=row.name, username=user)
        self.runtimes.pop(stream_id, None)

    async def wait_until_settled(self, stream_id: int, timeout: float) -> Stream:
        """Wait until a stream leaves STARTING/RESTARTING (used by ?wait=true).

        A fixed lead-in is waited through (the caller gets RUNNING); a hold
        lead-in settles at WAITING_FOR_VIEWER, which can last indefinitely."""
        row = self.get_row(stream_id)
        if (row.lead_in_mode or "none") == "fixed":
            timeout += float(row.lead_in_s or 0) + self.s.slate_timeout_s / 4
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.get_row(stream_id)
            if row.status not in (StreamStatus.STARTING, StreamStatus.RESTARTING, StreamStatus.STOPPING,
                                  StreamStatus.LEAD_IN):
                return row
            await asyncio.sleep(0.25)
        return self.get_row(stream_id)

    async def _revalidate(self, row: Stream) -> None:
        """User-initiated (re)start: re-check the source and re-bind to it.

        If the file was replaced/modified, the stream is re-validated against
        the new file (the user explicitly asked to start it again).
        """
        if row.video_id is None:
            raise Invalid("The stream's source video no longer exists in the library.")
        try:
            fields = await self.prepare(StreamConfig(
                video_id=row.video_id, start_s=row.start_offset_s, end_s=row.end_offset_s,
                mode=row.playback_mode, force_transcode=bool(row.force_transcode),
                output_size=(row.config_json or {}).get("output_size") or "original", enforce_shape=False))
        except Invalid as exc:
            self._update(row.id, status=StreamStatus.ERROR, last_error=exc.message)
            raise
        if fields["source_fingerprint"] != row.source_fingerprint:
            self.events.add("stream.source_rebound", "source file changed; stream re-validated against the "
                            "current file", stream_id=row.id, stream_name=row.name, level="WARNING")
        self._update(row.id, **fields)

    # ================================================================ lead-in
    # A stream's lead-in puts its slate on the URL (MediaMTX "always available"
    # path, see slate_service and docs/SPIKE_LEADIN.md), then starts the video:
    # after lead_in_s (fixed), or once a reader connects + lead_in_s (hold).
    # The video start itself still goes through the start queue (_spawn).
    # Crash retries, loop rollovers and MediaMTX recovery go straight to the
    # video: the edge stays on the slate meanwhile instead of disconnecting.

    @staticmethod
    def _in_lead_in(rt: Runtime) -> bool:
        return rt.lead_task is not None and not rt.lead_task.done()

    def _cancel_lead(self, rt: Runtime) -> None:
        t = rt.lead_task
        if t is not None and not t.done() and t is not asyncio.current_task():
            t.cancel()
        rt.lead_task = None
        rt.lead = None
        rt.go_event = None

    async def _clear_slate(self, name: str) -> None:
        if name not in self.mtx.slate_paths:
            return
        try:
            await self.mtx.clear_slate(name)
        except Exception as exc:  # MediaMTX down: its restart forgets the path anyway
            self.mtx.slate_paths.pop(name, None)
            log.warning("could not remove lead-in path %s: %s", name, exc)

    def _slate_spec(self, row: Stream) -> SlateSpec:
        """The slate must look exactly like what the publisher will send."""
        cfg = row.config_json or {}
        video = None
        if row.video_id is not None:
            with self.db.session() as s:
                video = s.get(Video, row.video_id)
        width, height = int(cfg.get("width") or 0), int(cfg.get("height") or 0)
        if width <= 0 or height <= 0:
            raise Invalid("The video's size is unknown, so no lead-in slate can be made for it.")
        fps_text = fps_text_of(video.fps_text if video else None, cfg.get("fps"))
        if row.output_mode == compatibility.TRANSCODE:
            args = transcode_args(self._plan(row), self.s)
            scale = args[args.index("-vf") + 1] if "-vf" in args else None
            return SlateSpec(row.name, width, height, fps_text, self.s.x264_profile, scale)
        return SlateSpec(row.name, width, height, fps_text, profile_of(video.video_profile if video else None))

    async def _begin(self, rt: Runtime) -> None:
        """A user (or boot recovery) starts the stream: lead-in first if it has one.
        Caller must hold rt.lock."""
        if self._shutting_down:
            return
        row = self.get_row(rt.stream_id)
        mode = row.lead_in_mode or "none"
        self._cancel_lead(rt)
        rt.begun_mono = time.monotonic()
        if mode == "none":
            await self._clear_slate(row.name)
            await self._spawn(rt, user_initiated=True)
            return
        rt.go_event = asyncio.Event()
        rt.slate_up = asyncio.Event()
        rt.lead = {"mode": mode, "phase": "preparing", "lead_in_s": row.lead_in_s, "hold_max_s": row.hold_max_s}
        self._update(rt.stream_id, status=StreamStatus.STARTING, pid=None, status_detail="Preparing the lead-in")
        rt.lead_task = asyncio.create_task(self._lead_in(rt), name=f"lead-in-{rt.stream_id}")

    async def _lead_in(self, rt: Runtime) -> None:
        me = asyncio.current_task()
        try:
            row = self.get_row(rt.stream_id)
            spec = self._slate_spec(row)
            slate = await self.slates.ensure(row.id, spec)
            # The URL shows the slate from here on.
            while True:
                try:
                    await self.mtx.set_slate(row.name, str(slate))
                    break
                except Exception as exc:
                    if rt.lead_task is not me:
                        return
                    self._update(rt.stream_id, status=StreamStatus.RESTARTING,
                                 status_detail="Waiting for MediaMTX to show the lead-in")
                    log.info("lead-in for %s waiting for MediaMTX: %s", row.name, exc)
                    await asyncio.sleep(2)
            if rt.lead_task is not me:
                return
            t0 = time.monotonic()
            lead_s = float(row.lead_in_s or 0)
            rt.lead.update(slate_at=utcnow().isoformat(), style=self.slates.style())
            rt.slate_up.set()
            rt.sink.write(f"lead-in: slate on the URL ({row.lead_in_mode}, {lead_s:g}s, "
                          f"{spec.width}x{spec.height} {spec.fps_text})", "rtspm")
            go = rt.go_event
            reason = "lead-in finished"
            if row.lead_in_mode == "hold":
                rt.lead.update(phase="waiting")
                self._update(rt.stream_id, status=StreamStatus.WAITING_FOR_VIEWER,
                             status_detail="Showing the lead-in until a viewer connects")
                self.events.add("stream.waiting_for_viewer", "lead-in on the URL; the video starts "
                                f"{lead_s:g}s after the first viewer connects", stream_id=row.id, stream_name=row.name)
                deadline = t0 + row.hold_max_s if row.hold_max_s else None
                while True:
                    if go.is_set():
                        reason = "started by user"
                        break
                    p = self.mtx.path(row.name)
                    found = list(p.readers) if p is not None else []
                    if not found:  # fresh check: a reader that only probes leaves within the 2 s poll
                        found = (await self.mtx.rtsp_readers()).get(row.name, [])
                    if found:
                        rd = found[0]
                        who = rd.get("hostname") or rd.get("remote_addr") or rd.get("type")
                        rt.lead.update(viewer_seen_at=utcnow().isoformat(), viewer=who)
                        rt.sink.write(f"lead-in: viewer connected ({who}); video in {lead_s:g}s", "rtspm")
                        self.events.add("stream.viewer_connected", f"viewer {who} connected during the lead-in; "
                                        f"video in {lead_s:g}s", stream_id=row.id, stream_name=row.name)
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        reason = f"no viewer after {row.hold_max_s:g}s; starting anyway"
                        lead_s = 0.0
                        break
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(go.wait(), 0.25)
                rt.begun_mono = time.monotonic()  # queue place: when the hold ended, not when it began
            if lead_s > 0 and not go.is_set():
                rt.lead.update(phase="countdown", video_at_mono=time.monotonic() + lead_s)
                detail = "Viewer connected; video" if rt.lead.get("viewer_seen_at") else "Lead-in: video"
                self._update(rt.stream_id, status=StreamStatus.LEAD_IN, status_detail=f"{detail} in {lead_s:g} s")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(go.wait(), lead_s)
                if go.is_set():
                    reason = "started by user"
            async with rt.lock:
                if rt.lead_task is not me:
                    return
                row = self.get_row(rt.stream_id)
                if row.desired_state != DesiredState.RUNNING:
                    return
                rt.lead_task = None
                rt.lead = None
                rt.go_event = None
                rt.sink.write(f"lead-in: {reason}; starting the video", "rtspm")
                await self._spawn(rt, user_initiated=True)
        except asyncio.CancelledError:
            raise
        except NotFound:
            return
        except Exception as exc:
            msg = exc.message if isinstance(exc, Invalid) else str(exc)
            log.exception("lead-in failed for stream %s", rt.stream_id)
            async with rt.lock:
                if rt.lead_task is not me:
                    return
                rt.lead_task = None
                rt.lead = None
                if rt.slate_up:
                    rt.slate_up.set()
                self._update(rt.stream_id, status=StreamStatus.ERROR, pid=None,
                             last_error=f"Lead-in failed: {msg} Set the lead-in to None to start without it.",
                             status_detail="Lead-in failed")
                self.events.add("stream.lead_in_failed", msg, level="ERROR", stream_id=rt.stream_id)

    async def go_now(self, stream_id: int, user: str | None) -> Stream:
        """'Start video now': end the lead-in at once."""
        rt = self.runtime(stream_id)
        row = self.get_row(stream_id)
        if not self._in_lead_in(rt) or rt.go_event is None:
            raise Conflict(f"Stream '{row.name}' is not in its lead-in.")
        rt.go_event.set()
        self.events.add("stream.go", "video started by user during the lead-in", stream_id=stream_id,
                        stream_name=row.name, username=user)
        # let the lead-in hand over to the start queue before answering
        for _ in range(20):
            if not self._in_lead_in(rt):
                break
            await asyncio.sleep(0.05)
        return self.get_row(stream_id)

    def _lead_view(self, row: Stream, rt: Runtime | None) -> dict | None:
        if rt is None or not self._in_lead_in(rt) or rt.lead is None:
            return None
        lead = rt.lead
        video_in = None
        if lead.get("phase") == "countdown" and lead.get("video_at_mono"):
            video_in = max(0.0, round(lead["video_at_mono"] - time.monotonic(), 1))
        return {"mode": lead.get("mode"), "phase": lead.get("phase"), "lead_in_s": lead.get("lead_in_s"),
                "hold_max_s": lead.get("hold_max_s"), "video_in_s": video_in,
                "slate_at": lead.get("slate_at"), "viewer_seen_at": lead.get("viewer_seen_at"),
                "viewer": lead.get("viewer"), "slate_style": lead.get("style")}

    # ================================================================ processes
    def _plan(self, row: Stream) -> PublishPlan:
        cfg = row.config_json or {}
        start_time = float(cfg.get("container_start_time_s") or 0.0)
        path = self.library.lib.resolve(row.source_rel_path)
        plan = PublishPlan(
            stream_id=row.id, name=row.name, source_path=path,
            inpoint_abs=start_time + row.effective_start_s,
            outpoint_abs=cfg.get("outpoint_abs"),
            duration_s=float(cfg["period_s"]),
            mode=row.playback_mode, output_mode=row.output_mode or compatibility.COPY,
            publish_url=self.s.publish_url(row.name),
            playlist_path=self.s.run_path / "streams" / str(row.id) / "playlist.ffconcat",
            fps=cfg.get("fps"), width=cfg.get("width"), height=cfg.get("height"),
            out_width=cfg.get("out_width"), out_height=cfg.get("out_height"),
        )
        plan.entries = loop_entries(plan.period_s, self.s) if row.playback_mode == "loop" else 1
        return plan

    async def _spawn(self, rt: Runtime, user_initiated: bool = False) -> None:
        """Start the publisher, through the start queue. Caller must hold rt.lock.

        With start spacing enabled this only queues the start and returns at
        once (``rt.ticket``); the queue launches it in turn (``_gate_launch``).
        """
        if self._shutting_down:
            return
        if rt.publisher and rt.publisher.alive():  # never two publishers
            await self._stop_process(rt, StreamStatus.RESTARTING, "replacing publisher")
        if not self.gate.enabled:
            await self._launch(rt, user_initiated)
            return
        if rt.ticket is not None:
            self.gate.cancel(rt.ticket)
        # a user start keeps its place from when it was asked for (its lead-in may end a
        # few ms after a later camera's); crash retries queue at the back
        rt.ticket = self.gate.submit(rt.stream_id, user_initiated,
                                     order=rt.begun_mono if user_initiated else None)
        self._update(rt.stream_id, status=StreamStatus.STARTING, pid=None,
                     status_detail=f"Queued for a start slot ({self.gate.spacing_s:g} s spacing)")

    async def _gate_launch(self, t: Ticket) -> asyncio.Future | None:
        """Called by the start queue when it is this ticket's turn. Returns the
        task that completes when the stream is on air, or None if nothing was
        launched (cancelled, stopped, deleted, MediaMTX down, source invalid)."""
        rt = self.runtimes.get(t.stream_id)
        if rt is None:
            return None
        async with rt.lock:
            if rt.ticket is not t or t.cancelled or self._shutting_down:
                return None
            rt.ticket = None
            try:
                row = self.get_row(t.stream_id)
            except NotFound:
                return None
            if row.desired_state != DesiredState.RUNNING or (rt.publisher and rt.publisher.alive()):
                return None
            await self._launch(rt, t.user_initiated)
            return rt.ready_task if rt.publisher and rt.ready_task else None

    @staticmethod
    def _content_key(plan: PublishPlan) -> tuple:
        out = round(plan.outpoint_abs, 2) if plan.outpoint_abs is not None else None
        return (str(plan.source_path), round(plan.inpoint_abs, 2), out)

    def _observe_origin(self, rt: Runtime) -> float | None:
        """Best estimate of when this publisher sent its first frame (wall clock).

        Each progress report gives ``report time - media time sent``; reports
        arrive a little late, so the earliest value seen is the best one.
        """
        pub = rt.publisher
        if pub is None or not pub.alive():
            return rt.origin_wall
        pub.refresh()
        prog = pub.progress
        if prog.updated_at is None or prog.out_time_s <= 0:
            return rt.origin_wall
        o = prog.updated_at.timestamp() - prog.out_time_s
        rt.origin_wall = o if rt.origin_wall is None else min(rt.origin_wall, o)
        return rt.origin_wall

    def _learn_origin_lag(self) -> float:
        """Seconds from spawning a publisher to its first frame, as seen on the
        running publishers (median; their estimates improve the longer they run).
        Every stream is measured the same way, so the systematic error cancels
        out when a new stream's start is compared with the running ones."""
        lags = sorted(rt.origin_wall - rt.launch_wall for rt in list(self.runtimes.values())
                      if rt.origin_wall is not None and rt.launch_wall is not None
                      and rt.publisher is not None and rt.publisher.alive()
                      and rt.publisher.progress.out_time_s >= 3.0
                      and -2.0 < rt.origin_wall - rt.launch_wall < 5.0)
        if lags:
            self.origin_lag_s = lags[len(lags) // 2]
        return self.origin_lag_s

    def _peers(self, stream_id: int, plan: PublishPlan) -> list[tuple[float, float, bool]]:
        """Running streams of the same video and segment, with when each showed its first frame."""
        key = self._content_key(plan)
        now = time.time()
        peers = []
        for other in list(self.runtimes.values()):
            pub = other.publisher
            if other.stream_id == stream_id or other.plan is None or pub is None or not pub.alive():
                continue
            if self._content_key(other.plan) != key:
                continue
            origin = self._observe_origin(other)
            if origin is None:  # just launched: about to show its first frame
                origin = (other.launch_wall + self.origin_lag_s) if other.launch_wall else now
            peers.append((origin, other.plan.period_s, other.plan.mode == "loop"))
        return peers

    async def _gate_phase(self, t: Ticket) -> float:
        """Seconds to wait so this stream does not show the frame another camera of
        the same video is showing (see start_gate.phase_delay)."""
        rt = self.runtimes.get(t.stream_id)
        if rt is None:
            return 0.0
        try:
            row = self.get_row(t.stream_id)
            plan = self._plan(row)
        except Exception:
            return 0.0
        peers = self._peers(t.stream_id, plan)
        self._learn_origin_lag()
        gap = self.gate.spacing_s + self.s.start_phase_margin_s
        delay, dist, ok = phase_delay(time.time() + self.origin_lag_s, peers, gap, max_best_effort_s=2 * gap)
        if ok:
            rt.spacing_warning = None
        else:
            msg = (f"The clip ({plan.period_s:.1f} s) is too short to keep {len(peers) + 1} cameras of this "
                   f"video {self.gate.spacing_s:g} s apart; the closest is {dist:.1f} s away, so the edge may "
                   f"see the same frame from two cameras.")
            if rt.spacing_warning != msg:
                self.events.add("stream.spacing_short", msg, level="WARNING", stream_id=row.id,
                                stream_name=row.name)
            rt.spacing_warning = msg
            # best effort: wait once for the least-bad moment, then start anyway
            return delay if t.phase_tries == 0 else 0.0
        if delay > 0.01:
            rt.sink.write(f"start delayed {delay:.2f}s to stay {gap:g}s of content away from "
                          f"{len(peers)} other stream(s) of the same video", "rtspm")
        return delay

    async def _launch(self, rt: Runtime, user_initiated: bool = False) -> None:
        """Spawn the publisher now. Caller must hold rt.lock."""
        if self._shutting_down:
            return
        row = self.get_row(rt.stream_id)
        if rt.publisher and rt.publisher.alive():  # never two publishers
            await self._stop_process(rt, StreamStatus.RESTARTING, "replacing publisher")
            row = self.get_row(rt.stream_id)
        path = self.library.lib.resolve(row.source_rel_path)
        change = await asyncio.to_thread(describe_change, row.source_fingerprint, path)
        if change:
            await self._fail_source(rt, row, change)
            return
        if self.mtx.state == DOWN:
            rt.waiting_for_mediamtx = True
            self._update(rt.stream_id, status=StreamStatus.RESTARTING, pid=None,
                         status_detail="Waiting for MediaMTX to come back up")
            return
        try:
            plan = self._plan(row)
            await asyncio.to_thread(write_playlist, plan)
            argv = build_command(plan, self.s)
        except Exception as exc:
            self._update(rt.stream_id, status=StreamStatus.ERROR, last_error=f"Cannot build publisher: {exc}")
            return
        rt.plan = plan
        rt.fingerprint = row.source_fingerprint
        rt.stop_requested = False
        rt.waiting_for_mediamtx = False
        rt.not_ready_since = None
        rt.running_since_mono = None
        rt.ready_at = None
        rt.base_loops = row.loop_count if not user_initiated else 0
        rt.persisted_loops = rt.base_loops
        pub = PublisherProcess(argv, rt.sink, self._state_path(rt.stream_id), self.s,
                               meta={"stream_id": rt.stream_id, "base_loops": rt.base_loops})
        outp = f"{plan.outpoint_abs:.3f}s" if plan.outpoint_abs is not None else "end of file"
        rt.sink.write(f"starting publisher ({plan.output_mode}, {plan.mode}, inpoint {plan.inpoint_abs:.3f}s, "
                      f"outpoint {outp}, period {plan.period_s:.3f}s, entries {plan.entries})", "rtspm")
        try:
            await pub.start()
        except OSError as exc:
            self._update(rt.stream_id, status=StreamStatus.ERROR, pid=None,
                         last_error=f"Could not start FFmpeg: {exc}")
            rt.sink.write(f"could not start ffmpeg: {exc}", "rtspm")
            return
        rt.publisher = pub
        rt.spawned_at_mono = time.monotonic()
        rt.launch_wall = time.time()
        rt.origin_wall = None
        rt.sink.write(f"pid {pub.pid}: {' '.join(argv)}", "rtspm")
        self._update(rt.stream_id, status=StreamStatus.STARTING, status_detail="Publisher starting",
                     pid=pub.pid, ffmpeg_command=argv, last_start_at=utcnow(),
                     restart_count=row.restart_count + (0 if user_initiated else 1))
        rt.waiter_task = asyncio.create_task(self._watch_exit(rt, pub))
        rt.ready_task = asyncio.create_task(self._await_ready(rt, pub))

    async def _await_ready(self, rt: Runtime, pub: PublisherProcess) -> None:
        name = rt.plan.name if rt.plan else None
        deadline = time.monotonic() + self.s.startup_timeout_s
        while time.monotonic() < deadline:
            if rt.publisher is not pub or not pub.alive():
                return
            try:
                r = await self.mtx.client.get(f"/v3/paths/get/{name}")
                j = r.json() if r.status_code == 200 else {}
                online = j.get("online") if "online" in j else (j.get("ready") or j.get("available"))
                if r.status_code == 200 and online:
                    # refresh the shared path cache so every view agrees with RUNNING
                    with contextlib.suppress(Exception):
                        await self.mtx.poll_once()
                    async with rt.lock:
                        if rt.publisher is not pub or not pub.alive():
                            return
                        now = utcnow()
                        rt.ready_at = now
                        rt.running_since_mono = time.monotonic()
                        self._update(rt.stream_id, status=StreamStatus.RUNNING, status_detail=None,
                                     running_since=now, last_error=None)
                    elapsed = time.monotonic() - (rt.spawned_at_mono or time.monotonic())
                    rt.sink.write(f"RTSP path '{name}' is ready in MediaMTX ({elapsed:.2f}s after spawn)", "rtspm")
                    return
            except Exception:
                pass
            await asyncio.sleep(0.25)
        async with rt.lock:
            if rt.publisher is not pub or not pub.alive():
                return
            err = rt.sink.last_error() or "MediaMTX never reported the path as ready"
            rt.sink.write(f"publisher not ready after {self.s.startup_timeout_s:.0f}s; restarting", "rtspm")
            await self._stop_process(rt, StreamStatus.RESTARTING, "startup timeout")
            await self._handle_failure(rt, f"Startup timeout: {err}")

    async def _watch_exit(self, rt: Runtime, pub: PublisherProcess) -> None:
        rc = await pub.wait()
        async with rt.lock:
            if rt.publisher is not pub:  # replaced or stopped deliberately
                return
            self._persist_loops(rt, force=True)
            rt.publisher = None
            if rt.stop_requested or self._shutting_down:
                return
            row = self.get_row(rt.stream_id)
            elapsed = pub.progress.out_time_s
            rt.sink.write(f"publisher exited with code {rc} after {elapsed:.1f}s of media", "rtspm")
            if row.desired_state != DesiredState.RUNNING:
                self._update(rt.stream_id, status=StreamStatus.STOPPED, pid=None, running_since=None)
                return
            if rc == 0 and row.playback_mode == "once":
                self._update(rt.stream_id, status=StreamStatus.COMPLETED, desired_state=DesiredState.STOPPED,
                             status_detail="Played to the end of the segment", pid=None, running_since=None,
                             last_stop_at=utcnow())
                self.events.add("stream.completed", "play-once segment finished", stream_id=row.id,
                                stream_name=row.name)
                await self._clear_slate(row.name)
                return
            if rc == 0 and row.playback_mode == "loop":
                # Loop playlist exhausted (after loop_playlist_hours); start a fresh one.
                self.events.add("stream.rollover", "loop playlist exhausted; republishing", stream_id=row.id,
                                stream_name=row.name)
                await self._spawn(rt)
                return
            await self._handle_failure(rt, rt.sink.last_error() or f"FFmpeg exited with code {rc}")

    async def _handle_failure(self, rt: Runtime, error: str) -> None:
        """Classify an unexpected publisher failure. Caller holds rt.lock."""
        row = self.get_row(rt.stream_id)
        path = self.library.lib.resolve(row.source_rel_path)
        change = await asyncio.to_thread(describe_change, row.source_fingerprint, path)
        if change:
            await self._fail_source(rt, row, change)
            return
        if not await self._mediamtx_reachable():
            rt.waiting_for_mediamtx = True
            self._update(rt.stream_id, status=StreamStatus.RESTARTING, pid=None, running_since=None,
                         status_detail="MediaMTX is down; will republish when it is back")
            self.events.add("stream.waiting_mediamtx", "publisher lost: MediaMTX unavailable", level="WARNING",
                            stream_id=row.id, stream_name=row.name)
            return
        rt.failures += 1
        if rt.failures >= self.s.restart_max_failures:
            self._update(rt.stream_id, status=StreamStatus.ERROR, pid=None, running_since=None, last_error=error,
                         status_detail=f"Gave up after {rt.failures} consecutive failures")
            self.events.add("stream.error", f"gave up after {rt.failures} failures: {error}", level="ERROR",
                            stream_id=row.id, stream_name=row.name)
            return
        delay = min(self.s.restart_backoff_max_s, self.s.restart_backoff_initial_s * 2 ** (rt.failures - 1))
        self._update(rt.stream_id, status=StreamStatus.RESTARTING, pid=None, running_since=None, last_error=error,
                     status_detail=f"Publisher failed; retry {rt.failures} in {delay:.0f}s")
        self.events.add("stream.publisher_failed", f"{error} (retry in {delay:.0f}s)", level="WARNING",
                        stream_id=row.id, stream_name=row.name)
        rt.retry_task = asyncio.create_task(self._retry_later(rt, delay))

    async def _retry_later(self, rt: Runtime, delay: float) -> None:
        await asyncio.sleep(delay)
        async with rt.lock:
            if rt.publisher and rt.publisher.alive():
                return
            try:
                row = self.get_row(rt.stream_id)
            except NotFound:
                return
            if row.desired_state == DesiredState.RUNNING:
                await self._spawn(rt)

    async def _fail_source(self, rt: Runtime, row: Stream, change: str) -> None:
        """Source became invalid: stop safely with a clear error. Caller holds lock."""
        await self._stop_process(rt, StreamStatus.ERROR, change)
        self._update(rt.stream_id, status=StreamStatus.ERROR, desired_state=DesiredState.STOPPED,
                     last_error=f"{change} Stream stopped. Restart it to re-validate the file.",
                     status_detail=change, pid=None, running_since=None, last_stop_at=utcnow(),
                     version=row.version + 1, updated_at=utcnow(), updated_by="system")
        rt.sink.write(f"source check failed: {change}", "rtspm")
        await self._clear_slate(row.name)
        self.events.add("stream.source_invalid", change, level="ERROR", stream_id=row.id, stream_name=row.name)

    async def _mediamtx_reachable(self) -> bool:
        if self.mtx.state == DOWN:
            return False
        try:
            r = await self.mtx.client.get("/v3/paths/list", params={"itemsPerPage": 1})
            return r.status_code == 200
        except Exception:
            return False

    async def _stop_process(self, rt: Runtime, status: str, detail: str) -> None:
        """Stop the publisher if any (or take it out of the start queue). Caller holds rt.lock."""
        if rt.ticket is not None:
            self.gate.cancel(rt.ticket)
            rt.ticket = None
        self._cancel_lead(rt)
        for t in (rt.retry_task, rt.ready_task):
            if t and not t.done() and t is not asyncio.current_task():
                t.cancel()
        rt.retry_task = None
        pub = rt.publisher
        rt.stop_requested = True
        if pub is not None:
            if pub.alive():
                self._update(rt.stream_id, status=StreamStatus.STOPPING, status_detail=detail)
                rt.sink.write(f"stopping publisher pid {pub.pid}: {detail}", "rtspm")
                rc = await pub.stop(self.s.stop_timeout_s)
                rt.sink.write(f"publisher stopped (exit code {rc})", "rtspm")
            self._persist_loops(rt, force=True)
        rt.publisher = None
        rt.running_since_mono = None
        self._update(rt.stream_id, status=status, status_detail=detail, pid=None, running_since=None,
                     last_stop_at=utcnow() if pub is not None else None)

    # ================================================================ monitoring
    def _state_path(self, stream_id: int) -> Path:
        return self.s.run_path / "streams" / str(stream_id) / "state.json"

    def live_loops(self, rt: Runtime) -> int:
        if rt.publisher:
            rt.publisher.refresh()
        if rt.publisher and rt.plan and rt.plan.mode == "loop":
            return rt.base_loops + rt.publisher.progress.loops_completed(rt.plan.period_s)
        return rt.base_loops

    def _persist_loops(self, rt: Runtime, force: bool = False) -> None:
        loops = self.live_loops(rt)
        if loops != rt.persisted_loops:
            self._update(rt.stream_id, loop_count=loops)
            rt.persisted_loops = loops
        if rt.publisher and rt.plan and force:
            rt.base_loops = loops

    async def monitor_loop(self) -> None:
        last_prune = 0.0
        while not self._shutting_down:
            try:
                if time.monotonic() - last_prune > 3600:
                    last_prune = time.monotonic()
                    self.prune_history()
                await self.monitor_once()
            except Exception:
                log.exception("monitor iteration failed")
            await asyncio.sleep(self.s.monitor_interval_s)

    async def monitor_once(self) -> None:
        now = time.monotonic()
        # Streams waiting for MediaMTX: republish as soon as it answers. This
        # must not depend on a DOWN->UP transition: systemd can restart a
        # killed MediaMTX (~1 s) before the health monitor declares it DOWN.
        waiting = [rt for rt in self.runtimes.values()
                   if rt.waiting_for_mediamtx and rt.ticket is None and not self._in_lead_in(rt)
                   and not (rt.publisher and rt.publisher.alive())]
        if self.mtx.slate_paths and self.mtx.state == UP and now - self._last_slate_check >= 5:
            self._last_slate_check = now
            with contextlib.suppress(Exception):
                restored = await self.mtx.restore_slates()
                if restored:
                    self.events.add("mediamtx.slates_restored", f"lead-in restored on {len(restored)} URL(s) "
                                    "after MediaMTX restarted", level="WARNING", data={"paths": restored})
        if waiting and not self.recovering and self.mtx.state == UP and await self._mediamtx_reachable():
            asyncio.create_task(self.start_many([rt.stream_id for rt in waiting], reason="mediamtx-available"))
        for rt in list(self.runtimes.values()):
            pub = rt.publisher
            if pub is None or not pub.alive() or rt.plan is None:
                continue
            self._persist_loops(rt)
            self._observe_origin(rt)
            # ---- source watchdog (EC-07)
            if now - rt.last_source_check >= self.s.source_check_interval_s and rt.fingerprint:
                rt.last_source_check = now
                change = await asyncio.to_thread(describe_change, rt.fingerprint, rt.plan.source_path)
                if change:
                    async with rt.lock:
                        if rt.publisher is pub:
                            await self._fail_source(rt, self.get_row(rt.stream_id), change)
                    continue
            # ---- path health (process alive is not proof of a healthy stream)
            if self.mtx.state != UP or rt.ready_at is None:
                continue
            p = self.mtx.path(rt.plan.name)
            if p is not None and p.ready:
                rt.not_ready_since = None
                if rt.running_since_mono and now - rt.running_since_mono > 60:
                    rt.failures = 0
                continue
            if rt.not_ready_since is None:
                rt.not_ready_since = now
            elif now - rt.not_ready_since > self.s.stalled_grace_s:
                async with rt.lock:
                    if rt.publisher is not pub:
                        continue
                    rt.sink.write("publisher alive but RTSP path not ready; restarting publisher", "rtspm")
                    self.events.add("stream.stalled", "path not ready while publisher alive; restarting",
                                    level="WARNING", stream_id=rt.stream_id, stream_name=rt.plan.name)
                    await self._stop_process(rt, StreamStatus.RESTARTING, "stalled")
                    await self._handle_failure(rt, "Publisher stalled (RTSP path not ready)")

    # ================================================================ MediaMTX events
    async def on_mediamtx_down(self) -> None:
        self.events.add("mediamtx.down", f"MediaMTX unreachable: {self.mtx.last_error}", level="ERROR",
                        data={"detection_s": self.mtx.last_detection_s})
        with self.db.session() as s:
            ids = [r.id for r in s.scalars(select(Stream).where(Stream.desired_state == DesiredState.RUNNING))]
        for sid in ids:
            rt = self.runtime(sid)
            if self._in_lead_in(rt):
                continue  # the lead-in waits for MediaMTX itself
            rt.waiting_for_mediamtx = True
            self._update(sid, status=StreamStatus.RESTARTING,
                         status_detail="MediaMTX is down; will republish when it is back")

    async def on_mediamtx_up(self) -> None:
        if self.recovering:
            return
        with self.db.session() as s:
            ids = [r.id for r in s.scalars(select(Stream).where(Stream.desired_state == DesiredState.RUNNING))]
        with contextlib.suppress(Exception):
            await self.mtx.restore_slates()
        todo = [sid for sid in ids
                if not (self.runtime(sid).publisher and self.runtime(sid).publisher.alive())
                and not self._in_lead_in(self.runtime(sid))]
        if not todo:
            return
        self.events.add("mediamtx.up", f"MediaMTX available; republishing {len(todo)} stream(s)")
        asyncio.create_task(self.start_many(todo, reason="mediamtx-recovery"))

    async def start_many(self, ids: list[int], reason: str) -> list[dict]:
        """Start several streams in parallel with bounded concurrency.

        Returns per-stream timing (seconds from call until RUNNING).
        """
        t0 = time.monotonic()

        async def one(sid: int) -> dict:
            rt = self.runtime(sid)
            try:
                row = self.get_row(sid)
            except NotFound:
                return {"id": sid, "result": "deleted"}
            sem2 = self._transcode_sem if row.output_mode == compatibility.TRANSCODE else contextlib.nullcontext()
            async with self._start_sem:
                async with sem2:
                    async with rt.lock:
                        row = self.get_row(sid)
                        if row.desired_state != DesiredState.RUNNING or (rt.publisher and rt.publisher.alive()):
                            return {"id": sid, "name": row.name, "result": "skipped"}
                        if self._in_lead_in(rt):
                            return {"id": sid, "name": row.name, "result": row.status}
                        if reason == "boot-recovery" and (row.lead_in_mode or "none") != "none":
                            await self._begin(rt)
                        elif rt.ticket is None:  # already queued: wait for that start instead
                            await self._spawn(rt)
                        ticket = rt.ticket
                        slate_up = rt.slate_up if self._in_lead_in(rt) else None
                    if slate_up is not None:
                        # The URL works once the slate is on it; the video follows the lead-in.
                        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
                            await asyncio.wait_for(asyncio.shield(slate_up.wait()), self.s.startup_timeout_s)
                        row = self.get_row(sid)
                        return {"id": sid, "name": row.name, "result": row.status,
                                "seconds": round(time.monotonic() - t0, 2),
                                "ready_at": (rt.lead or {}).get("slate_at")}
                    if ticket is not None:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await asyncio.shield(ticket.launched)
                    if rt.ready_task:
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await rt.ready_task
            row = self.get_row(sid)
            return {"id": sid, "name": row.name, "result": row.status,
                    "seconds": round(time.monotonic() - t0, 2),
                    "ready_at": rt.ready_at.isoformat() if rt.ready_at else None}

        results = await asyncio.gather(*(one(i) for i in ids))
        ok = sum(1 for r in results if r.get("result") == StreamStatus.RUNNING)
        self.events.add("streams.started", f"{reason}: {ok}/{len(ids)} running in {time.monotonic() - t0:.1f}s",
                        data={"results": results})
        return results

    # ================================================================ lifecycle
    def start_monitor(self) -> None:
        self._monitor_task = asyncio.create_task(self.monitor_loop(), name="stream-monitor")

    @property
    def keeps_publishers(self) -> bool:
        # A backend-owned MediaMTX (process mode) dies with the backend, so its
        # publishers would be useless: only keep them when MediaMTX is separate.
        return self.s.keep_publishers_on_restart and self.s.mediamtx_control != "process"

    async def shutdown(self) -> None:
        """Backend stopping. By default publishers keep running (and are
        re-adopted on start) so an upgrade/restart does not drop cameras.
        Otherwise stop them, keeping desired_state for the next start."""
        self._shutting_down = True
        await self.gate.close()
        for rt in list(self.runtimes.values()):
            self._cancel_lead(rt)
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task

        async def stop_one(rt: Runtime):
            async with rt.lock:
                if rt.retry_task and not rt.retry_task.done():
                    rt.retry_task.cancel()
                # Only touch streams that have a publisher: COMPLETED / ERROR /
                # STOPPED statuses must survive a reboot unchanged.
                if rt.publisher is None:
                    return
                if self.keeps_publishers and rt.publisher.alive():
                    self._persist_loops(rt)
                    rt.sink.write("backend stopping; publisher left running for re-adoption", "rtspm")
                    return
                await self._stop_process(rt, StreamStatus.STOPPED, "backend shutting down")

        await asyncio.gather(*(stop_one(rt) for rt in list(self.runtimes.values())), return_exceptions=True)

    async def adopt_running(self) -> list[int]:
        """Re-attach to publishers that kept running while the backend restarted.

        A publisher is adopted only if its stream still wants to run and its
        command line is exactly what the current configuration would start;
        anything else is left for reap_orphans() to terminate.
        """
        adopted: list[int] = []
        with self.db.session() as s:
            rows = list(s.scalars(select(Stream).where(Stream.desired_state == DesiredState.RUNNING)))
        for row in rows:
            rt = self.runtime(row.id)
            pub = PublisherProcess.adopt(self._state_path(row.id), rt.sink, self.s)
            if pub is None:
                continue
            try:
                plan = self._plan(row)
                expected = build_command(plan, self.s)
            except Exception:
                expected = None
            if pub.argv != expected:
                rt.sink.write("found a publisher with an outdated command; replacing it", "rtspm")
                await pub.stop(self.s.stop_timeout_s)
                continue
            if (row.lead_in_mode or "none") != "none":
                # MediaMTX kept running, so the slate is still configured: remember it
                with contextlib.suppress(Exception):
                    slate = self.slates.path_for(row.id, self._slate_spec(row))
                    if slate.exists():
                        await self.mtx.set_slate(row.name, str(slate))
            async with rt.lock:
                rt.plan, rt.publisher, rt.fingerprint = plan, pub, row.source_fingerprint
                rt.stop_requested = False
                rt.base_loops = int(pub.meta.get("base_loops", row.loop_count))
                rt.persisted_loops = row.loop_count
                rt.spawned_at_mono = time.monotonic()
                rt.sink.write(f"backend restarted; adopted running publisher pid {pub.pid}", "rtspm")
                self._update(row.id, status=StreamStatus.STARTING, status_detail="Re-attached after backend restart",
                             pid=pub.pid)
                rt.waiter_task = asyncio.create_task(self._watch_exit(rt, pub))
                rt.ready_task = asyncio.create_task(self._await_ready(rt, pub))
            adopted.append(row.id)
        if adopted:
            self.events.add("recovery.adopted", f"re-attached {len(adopted)} running publisher(s) without interruption")
        return adopted

    def reap_orphans(self) -> int:
        """Kill publishers (runner + FFmpeg) left behind and not adopted."""
        marker = str(self.s.run_path / "streams")
        me = psutil.Process().username()
        keep = set()
        for rt in self.runtimes.values():
            if rt.publisher and rt.publisher.alive():
                keep |= {rt.publisher.runner_pid, rt.publisher.ffmpeg_pid}
        killed = 0
        for p in psutil.process_iter(["pid", "name", "cmdline", "username"]):
            try:
                cmd = p.info.get("cmdline") or []
                if p.info.get("username") != me or not cmd or p.pid in keep:
                    continue
                is_ffmpeg = "ffmpeg" in Path(cmd[0]).name
                is_runner = "app.services.publisher_runner" in cmd
                if not (is_ffmpeg or is_runner) or not any(marker in a for a in cmd):
                    continue
                p.terminate()
                try:
                    p.wait(3)
                except psutil.TimeoutExpired:
                    p.kill()
                killed += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if killed:
            self.events.add("recovery.orphans", f"terminated {killed} orphaned publisher(s)", level="WARNING")
        return killed

    # ================================================================ views
    def runtime_view(self, row: Stream, proc_index: dict[int, dict] | None = None) -> dict:
        """Live view of a stream; ``proc_index`` maps pid -> sampled cpu/rss."""
        rt = self.runtimes.get(row.id)
        p = self.mtx.path(row.name) if self.mtx.state == UP else None
        pub = rt.publisher if rt else None
        if pub:
            pub.refresh()
        progress = pub.progress.snapshot() if pub else None
        proc_stats = (proc_index or {}).get(pub.pid) if pub and pub.alive() else None
        loops = self.live_loops(rt) if rt else row.loop_count
        return {
            "path_ready": bool(p and p.ready),
            "path_known": p is not None,
            "readers": p.readers if p else [],
            "viewer_count": len(p.readers) if p else 0,
            "viewer_peak_today": self.mtx.peak_today(row.name),
            "preview_watchers": self.mtx.preview_watchers(row.name),
            "bytes_received": p.bytes_received if p else None,
            "ingest_bps": round(p.ingest_bps) if p and p.ingest_bps is not None else None,
            "egress_bps": round(p.egress_bps) if p and p.egress_bps is not None else None,
            "tracks": p.tracks if p else [],
            "progress": progress,
            "process": proc_stats,
            "loop_count": loops if row.playback_mode == "loop" else 0,
            "loop_period_s": rt.plan.period_s if rt and rt.plan else (row.config_json or {}).get("period_s"),
            "playback_position_s": (row.effective_start_s + (pub.progress.out_time_s % rt.plan.period_s))
            if pub and rt and rt.plan and rt.plan.period_s > 0 else None,
            "waiting_for_mediamtx": bool(rt and rt.waiting_for_mediamtx),
            "consecutive_failures": rt.failures if rt else 0,
            "start_queue": ({"position": self.gate.position(rt.ticket), "eta_s": self.gate.eta_s(rt.ticket)}
                            if rt and rt.ticket and self.gate.position(rt.ticket) else None),
            "spacing_warning": rt.spacing_warning if rt else None,
            "lead_in": self._lead_view(row, rt),
            "slate_on_url": row.name in self.mtx.slate_paths,
            "suppressed_timestamp_warnings": pub.suppressed if pub else (rt.sink.suppressed if rt else 0),
        }

    def logs(self, stream_id: int, lines: int) -> list[str]:
        self.get_row(stream_id)
        return self.runtime(stream_id).sink.tail(lines)
