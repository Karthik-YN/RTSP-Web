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
from .ffmpeg_manager import LogSink, PublishPlan, PublisherProcess, build_command, loop_entries, write_playlist
from .ffprobe_service import ProbeError, keyframe_at_or_before, video_end_time
from .file_manager import LibraryError, describe_change, fingerprint, public_error
from .mediamtx_service import DOWN, UP, MediaMTXService
from .validation import ValidationError, validate_segment, validate_stream_name
from .video_library import VideoLibrary

log = logging.getLogger(__name__)

ACTIVE_STATUSES = {StreamStatus.STARTING, StreamStatus.RUNNING, StreamStatus.RESTARTING, StreamStatus.STOPPING}


@dataclass
class StreamConfig:
    video_id: int
    start_s: float | None = 0.0
    end_s: float | None = None
    mode: str = "loop"
    force_transcode: bool = False


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


class StreamSupervisor:
    def __init__(self, settings: Settings, db: Database, mediamtx: MediaMTXService,
                 library: VideoLibrary, events: EventLog):
        self.s = settings
        self.db = db
        self.mtx = mediamtx
        self.library = library
        self.events = events
        self.runtimes: dict[int, Runtime] = {}
        self._create_lock = asyncio.Lock()
        self._monitor_task: asyncio.Task | None = None
        self._start_sem = asyncio.Semaphore(settings.recovery_concurrency)
        self._transcode_sem = asyncio.Semaphore(settings.recovery_transcode_concurrency)
        self._shutting_down = False
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
        if active >= self.s.max_active_streams:
            raise CapacityExceeded(
                f"Capacity reached: {active} streams are already running (limit "
                f"{self.s.max_active_streams}, RTSPM_MAX_ACTIVE_STREAMS). Stop a stream first.")
        if will_transcode and transcodes >= self.s.max_transcode_streams:
            raise CapacityExceeded(
                f"Transcoding capacity reached: {transcodes} transcoded streams are running (limit "
                f"{self.s.max_transcode_streams}). Stop one, or use an H.264 8-bit source that can be copied.")

    # ================================================================ config
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
        compat = self.library.compat_for(self.library.meta_of(video), cfg.force_transcode)
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
                     note: str | None = None, protected: bool = False) -> Stream:
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
                                 protected=bool(protected), **fields)
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
                await self._spawn(rt, user_initiated=True)
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
                                                                             "protected", "owner")})
            if config_changed and row.desired_state == DesiredState.RUNNING:
                await self._stop_process(rt, StreamStatus.RESTARTING, "applying new configuration")
                rt.base_loops = 0
                await self._spawn(rt, user_initiated=True)
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
            await self._spawn(rt, user_initiated=True)
            return self.get_row(stream_id)

    async def stop(self, stream_id: int, expected_version: int | None, user: str | None) -> Stream:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            self._update(stream_id, desired_state=DesiredState.STOPPED, version=row.version + 1,
                         updated_at=utcnow(), updated_by=user)
            await self._stop_process(rt, StreamStatus.STOPPED, f"stopped by {user or 'user'}")
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
            await self._spawn(rt, user_initiated=True)
            return self.get_row(stream_id)

    async def delete(self, stream_id: int, expected_version: int | None, user: str | None) -> None:
        rt = self.runtime(stream_id)
        async with rt.lock:
            row = self.get_row(stream_id)
            self._check_version(row, expected_version)
            await self._stop_process(rt, StreamStatus.STOPPED, "deleting")
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
        """Wait until a stream leaves STARTING/RESTARTING (used by ?wait=true)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.get_row(stream_id)
            if row.status not in (StreamStatus.STARTING, StreamStatus.RESTARTING, StreamStatus.STOPPING):
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
                mode=row.playback_mode, force_transcode=bool(row.force_transcode)))
        except Invalid as exc:
            self._update(row.id, status=StreamStatus.ERROR, last_error=exc.message)
            raise
        if fields["source_fingerprint"] != row.source_fingerprint:
            self.events.add("stream.source_rebound", "source file changed; stream re-validated against the "
                            "current file", stream_id=row.id, stream_name=row.name, level="WARNING")
        self._update(row.id, **fields)

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
        )
        plan.entries = loop_entries(plan.period_s, self.s) if row.playback_mode == "loop" else 1
        return plan

    async def _spawn(self, rt: Runtime, user_initiated: bool = False) -> None:
        """Start the publisher. Caller must hold rt.lock."""
        if self._shutting_down:
            return
        row = self.get_row(rt.stream_id)
        if rt.publisher and rt.publisher.alive():  # never two publishers
            await self._stop_process(rt, StreamStatus.RESTARTING, "replacing publisher")
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
                if r.status_code == 200 and (r.json().get("ready") or r.json().get("available")):
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
        """Stop the publisher if any. Caller holds rt.lock."""
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
                   if rt.waiting_for_mediamtx and not (rt.publisher and rt.publisher.alive())]
        if waiting and not self.recovering and self.mtx.state == UP and await self._mediamtx_reachable():
            asyncio.create_task(self.start_many([rt.stream_id for rt in waiting], reason="mediamtx-available"))
        for rt in list(self.runtimes.values()):
            pub = rt.publisher
            if pub is None or not pub.alive() or rt.plan is None:
                continue
            self._persist_loops(rt)
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
            rt.waiting_for_mediamtx = True
            self._update(sid, status=StreamStatus.RESTARTING,
                         status_detail="MediaMTX is down; will republish when it is back")

    async def on_mediamtx_up(self) -> None:
        if self.recovering:
            return
        with self.db.session() as s:
            ids = [r.id for r in s.scalars(select(Stream).where(Stream.desired_state == DesiredState.RUNNING))]
        todo = [sid for sid in ids
                if not (self.runtime(sid).publisher and self.runtime(sid).publisher.alive())]
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
                        await self._spawn(rt)
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
            "suppressed_timestamp_warnings": pub.suppressed if pub else (rt.sink.suppressed if rt else 0),
        }

    def logs(self, stream_id: int, lines: int) -> list[str]:
        self.get_row(stream_id)
        return self.runtime(stream_id).sink.tail(lines)
