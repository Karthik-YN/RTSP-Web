from __future__ import annotations

import asyncio
import re
import shlex

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import select

from ..models import Stream, StreamStatus, Video
from ..schemas import LogsOut, StreamCreate, StreamDetailOut, StreamOut, StreamUpdate, StreamValidateOut
from ..services.auth import User
from ..services.errors import Invalid, ServiceError
from ..services.ffprobe_service import ProbeError, grab_frame_jpeg
from ..services.stream_supervisor import StreamConfig
from ..services.validation import ValidationError, parse_timestamp, validate_stream_name
from .deps import Context, current_user, get_ctx, if_match, require_operator
from .videos import video_out

router = APIRouter(prefix="/api/streams", tags=["streams"])

_snapshot_sem = asyncio.Semaphore(2)
_HLS_FILE = re.compile(r"[A-Za-z0-9_.-]{1,128}")


def _proc_index(ctx: Context) -> dict[int, dict]:
    return {p["pid"]: p for p in (ctx.sysmon.sample or {}).get("ffmpeg", [])}


def stream_out(ctx: Context, row: Stream, proc_index: dict | None = None) -> StreamOut:
    return StreamOut(
        id=row.id, name=row.name, rtsp_url=ctx.settings.public_rtsp_url(row.name), video_id=row.video_id,
        source_rel_path=row.source_rel_path, start_offset_s=row.start_offset_s,
        effective_start_s=row.effective_start_s, end_offset_s=row.end_offset_s,
        effective_end_s=(row.config_json or {}).get("segment_end_s"),
        playback_mode=row.playback_mode, playback_speed=row.playback_speed,
        force_transcode=bool(row.force_transcode), output_mode=row.output_mode,
        desired_state=row.desired_state, status=row.status, status_detail=row.status_detail, pid=row.pid,
        last_error=row.last_error, loop_count=row.loop_count, restart_count=row.restart_count,
        version=row.version, created_at=row.created_at, created_by=row.created_by, updated_at=row.updated_at,
        updated_by=row.updated_by, last_start_at=row.last_start_at, last_stop_at=row.last_stop_at,
        running_since=row.running_since, owner=row.owner, note=row.note, protected=bool(row.protected),
        group_id=row.group_id, warnings=(row.config_json or {}).get("compat_warnings", []),
        runtime=ctx.supervisor.runtime_view(row, proc_index if proc_index is not None else _proc_index(ctx)),
    )


def guard(row: Stream, user: User, action: str) -> None:
    """Protected streams can only be changed by their owner or an admin."""
    if row.protected and not (user.is_admin or user.username == row.owner):
        note = f" Note: {row.note}" if row.note else ""
        raise HTTPException(403, f"Stream '{row.name}' is protected by {row.owner or 'its owner'}; "
                                 f"only they or an admin can {action} it.{note}")


def _cfg(body, row: Stream | None = None) -> StreamConfig:
    try:
        start = parse_timestamp(body.start, "start")
        end = parse_timestamp(body.end, "end")
    except ValidationError as exc:
        raise Invalid(str(exc), {"field": exc.field})
    if row is None:
        return StreamConfig(video_id=body.video_id, start_s=start, end_s=end, mode=body.mode,
                            force_transcode=body.force_transcode)
    return StreamConfig(
        video_id=body.video_id if body.video_id is not None else row.video_id,
        start_s=start if body.start is not None else row.start_offset_s,
        end_s=None if body.clear_end else (end if body.end is not None else row.end_offset_s),
        mode=body.mode or row.playback_mode,
        force_transcode=body.force_transcode if body.force_transcode is not None else bool(row.force_transcode),
    )


@router.get("", response_model=list[StreamOut])
def list_streams(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    with ctx.db.session() as s:
        rows = list(s.scalars(select(Stream).order_by(Stream.name)))
    idx = _proc_index(ctx)
    return [stream_out(ctx, r, idx) for r in rows]


@router.post("/validate", response_model=StreamValidateOut)
async def validate(body: StreamCreate, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Dry run of stream creation: checks everything, starts nothing."""
    errors = []
    try:
        validate_stream_name(body.name, ctx.settings.stream_name_pattern, ctx.settings.reserved_names)
        with ctx.db.session() as s:
            if s.scalar(select(Stream.id).where(Stream.name == body.name)) is not None:
                errors.append({"field": "name", "message": f"Stream name '{body.name}' is already in use."})
    except ValidationError as exc:
        errors.append({"field": "name", "message": str(exc)})
    try:
        fields = await ctx.supervisor.prepare(_cfg(body))
    except ServiceError as exc:
        errors.append({"field": exc.details.get("field"), "message": exc.message})
        return StreamValidateOut(ok=False, errors=errors)
    cfg = fields["config_json"]
    return StreamValidateOut(
        ok=not errors, errors=errors, start_s=fields["start_offset_s"],
        effective_start_s=fields["effective_start_s"], end_s=fields["end_offset_s"],
        effective_end_s=cfg["segment_end_s"], period_s=cfg["period_s"],
        output_mode=fields["output_mode"], warnings=cfg["compat_warnings"],
        rtsp_url=ctx.settings.public_rtsp_url(body.name) if not errors else None,
    )


@router.post("", response_model=StreamOut, status_code=201)
async def create_stream(body: StreamCreate, wait: bool = False, ctx: Context = Depends(get_ctx),
                        user: User = Depends(require_operator)):
    row = await ctx.supervisor.create(body.name, _cfg(body), body.start_now, user.username,
                                      note=body.note, protected=body.protected)
    if wait and body.start_now:
        row = await ctx.supervisor.wait_until_settled(row.id, ctx.settings.startup_timeout_s + 5)
    return stream_out(ctx, row)


@router.get("/{stream_id}", response_model=StreamDetailOut)
def get_stream(stream_id: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    row = ctx.supervisor.get_row(stream_id)
    base = stream_out(ctx, row).model_dump()
    video = None
    if row.video_id is not None:
        with ctx.db.session() as s:
            v = s.get(Video, row.video_id)
            video = video_out(v) if v else None
    events = [
        {"ts": e.ts.isoformat(), "level": e.level, "kind": e.kind, "user": e.username, "message": e.message}
        for e in ctx.events.recent(30, stream_id=stream_id)
    ]
    cmd = row.ffmpeg_command
    return StreamDetailOut(
        **base, ffmpeg_command=cmd, ffmpeg_command_text=shlex.join(cmd) if cmd else None,
        config=row.config_json or {}, video=video, log_tail=ctx.supervisor.logs(stream_id, 20), events=events,
    )


@router.patch("/{stream_id}", response_model=StreamOut)
async def update_stream(stream_id: int, body: StreamUpdate, version: int | None = Depends(if_match),
                        ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    row = ctx.supervisor.get_row(stream_id)
    guard(row, user, "edit")
    if body.owner is not None and not user.is_admin:
        raise HTTPException(403, "Only an admin can transfer ownership.")
    if body.protected is not None and not (user.is_admin or user.username == row.owner):
        raise HTTPException(403, "Only the owner or an admin can protect or unprotect a stream.")
    touches_cfg = any(v is not None for v in (body.video_id, body.start, body.end, body.mode,
                                              body.force_transcode)) or body.clear_end
    meta = {k: v for k, v in (("note", body.note), ("protected", body.protected), ("owner", body.owner))
            if v is not None}
    row = await ctx.supervisor.update(stream_id, version, user.username, name=body.name,
                                      cfg=_cfg(body, row) if touches_cfg else None, meta=meta)
    return stream_out(ctx, row)


@router.post("/{stream_id}/start", response_model=StreamOut)
async def start_stream(stream_id: int, wait: bool = False, version: int | None = Depends(if_match),
                       ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    guard(ctx.supervisor.get_row(stream_id), user, "start")
    row = await ctx.supervisor.start(stream_id, version, user.username)
    if wait:
        row = await ctx.supervisor.wait_until_settled(stream_id, ctx.settings.startup_timeout_s + 5)
    return stream_out(ctx, row)


@router.post("/{stream_id}/stop", response_model=StreamOut)
async def stop_stream(stream_id: int, version: int | None = Depends(if_match),
                      ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    guard(ctx.supervisor.get_row(stream_id), user, "stop")
    return stream_out(ctx, await ctx.supervisor.stop(stream_id, version, user.username))


@router.post("/{stream_id}/restart", response_model=StreamOut)
async def restart_stream(stream_id: int, wait: bool = False, version: int | None = Depends(if_match),
                         ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    guard(ctx.supervisor.get_row(stream_id), user, "restart")
    row = await ctx.supervisor.restart(stream_id, version, user.username)
    if wait:
        row = await ctx.supervisor.wait_until_settled(stream_id, ctx.settings.startup_timeout_s + 5)
    return stream_out(ctx, row)


@router.delete("/{stream_id}", status_code=204)
async def delete_stream(stream_id: int, version: int | None = Depends(if_match),
                        ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Stops the publisher and removes the stream. The source video is never deleted."""
    guard(ctx.supervisor.get_row(stream_id), user, "delete")
    await ctx.supervisor.delete(stream_id, version, user.username)
    return Response(status_code=204)


@router.get("/{stream_id}/history")
def stream_history(stream_id: int, hours: float = Query(default=24, gt=0, le=168), ctx: Context = Depends(get_ctx),
                   _: User = Depends(current_user)):
    """Status timeline (segments) and percentage of time RUNNING."""
    ctx.supervisor.get_row(stream_id)
    return ctx.supervisor.history(stream_id, hours)


@router.get("/{stream_id}/logs", response_model=LogsOut)
def stream_logs(stream_id: int, lines: int = Query(default=20, ge=1, le=200), ctx: Context = Depends(get_ctx),
                _: User = Depends(current_user)):
    return LogsOut(stream_id=stream_id, lines=ctx.supervisor.logs(stream_id, lines))


_preview_cache: dict[int, tuple[float, bytes]] = {}


@router.get("/{stream_id}/preview")
async def preview(stream_id: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Thumbnail of what the stream is publishing right now, taken from the source
    file at the current playback position (no RTSP reader, so viewer counts stay
    accurate). Cached for 20 s per stream."""
    import time as _t
    row = ctx.supervisor.get_row(stream_id)
    cached = _preview_cache.get(stream_id)
    if cached and _t.monotonic() - cached[0] < 20:
        return Response(cached[1], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=20"})
    view = ctx.supervisor.runtime_view(row)
    pos = view.get("playback_position_s")
    at = pos if pos is not None else row.effective_start_s
    if row.video_id is None:
        raise HTTPException(404, "No source video.")
    with ctx.db.session() as s:
        v = s.get(Video, row.video_id)
    if v is None:
        raise HTTPException(404, "No source video.")
    async with _snapshot_sem:
        try:
            data = await ctx.library.frame(v, at)
        except (ProbeError, Exception) as exc:  # noqa: BLE001 - shown as a broken thumbnail
            raise HTTPException(503, f"No preview: {exc}")
    _preview_cache[stream_id] = (_t.monotonic(), data)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=20"})


@router.get("/{stream_id}/live/{file}")
async def live_preview(stream_id: int, file: str, request: Request, ctx: Context = Depends(get_ctx),
                       _: User = Depends(current_user)):
    """HLS files of a stream for the dashboard's live preview.

    Proxies MediaMTX's loopback-only HLS server, so the preview needs no extra
    open port and sits behind the dashboard login.
    """
    if not _HLS_FILE.fullmatch(file):
        raise HTTPException(404, "Not found")
    row = ctx.supervisor.get_row(stream_id)
    if row.status != StreamStatus.RUNNING:
        raise HTTPException(409, "The stream is not live.")
    url = f"{ctx.settings.mediamtx_hls_url.rstrip('/')}/{row.name}/{file}"
    try:
        # follow_redirects: MediaMTX answers the first request with a cookie-check 302.
        # The first playlist is held until 3 segments exist; a segment can't be shorter
        # than the keyframe interval, so 10 s keyframes take about 30 s.
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            r = await client.get(url, params=dict(request.query_params))
    except httpx.HTTPError as exc:
        raise HTTPException(503, f"Preview unavailable: {type(exc).__name__}")
    headers = {"Cache-Control": "no-store"}
    return Response(r.content, status_code=r.status_code, headers=headers,
                    media_type=r.headers.get("content-type", "application/octet-stream"))


@router.get("/{stream_id}/snapshot")
async def snapshot(stream_id: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Grab one frame by reading the stream over RTSP (end-to-end check).

    Note: this briefly appears as a reader on the path.
    """
    row = ctx.supervisor.get_row(stream_id)
    async with _snapshot_sem:
        try:
            data = await asyncio.to_thread(grab_frame_jpeg, ctx.settings.ffmpeg_bin,
                                           ctx.settings.publish_url(row.name), None, 640, 20.0, True)
        except ProbeError as exc:
            raise HTTPException(503, f"Could not read a frame from the RTSP stream: {exc}")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
