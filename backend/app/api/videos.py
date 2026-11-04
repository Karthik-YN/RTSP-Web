from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from starlette.requests import ClientDisconnect

from ..models import Stream, Video
from ..schemas import ScanOut, UploadCheckIn, UploadCheckOut, VideoOut
from ..services.auth import User
from ..services.ffprobe_service import ProbeError
from ..services.file_manager import LibraryError
from .deps import Context, current_user, get_ctx, require_admin, require_operator

router = APIRouter(prefix="/api/videos", tags=["videos"])


def video_out(v: Video, streams_using: int = 0) -> VideoOut:
    return VideoOut(
        id=v.id, rel_path=v.rel_path, filename=PurePosixPath(v.rel_path).name, size_bytes=v.size_bytes,
        status=v.status, error=v.error, container=v.container, duration_s=v.duration_s, width=v.width,
        height=v.height, fps=v.fps, fps_text=v.fps_text, is_vfr=bool(v.is_vfr), video_codec=v.video_codec,
        video_profile=v.video_profile, pix_fmt=v.pix_fmt, bit_depth=v.bit_depth, audio_codec=v.audio_codec,
        bitrate_bps=v.bitrate_bps, keyframe_interval_s=v.keyframe_interval_s, compat_mode=v.compat_mode,
        compat_reasons=v.compat_reasons or [], compat_warnings=v.compat_warnings or [],
        thumbnail_url=f"/api/videos/{v.id}/thumbnail?h={v.quick_hash[:8]}" if v.thumbnail else None,
        streams_using=streams_using, updated_at=v.updated_at, last_seen_at=v.last_seen_at,
    )


def _get(ctx: Context, video_id: int) -> Video:
    with ctx.db.session() as s:
        v = s.get(Video, video_id)
    if v is None:
        raise HTTPException(404, f"Video {video_id} not found.")
    return v


@router.get("", response_model=list[VideoOut])
def list_videos(search: str | None = Query(default=None, max_length=200),
                status: str | None = Query(default=None), include_missing: bool = False,
                ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    with ctx.db.session() as s:
        q = select(Video).order_by(Video.rel_path)
        if search:
            q = q.where(Video.rel_path.ilike(f"%{search}%"))
        if status:
            q = q.where(Video.status == status.upper())
        elif not include_missing:
            q = q.where(Video.status != "MISSING")
        videos = list(s.scalars(q))
        usage = dict(s.execute(select(Stream.video_id, func.count()).group_by(Stream.video_id)).all())
    return [video_out(v, usage.get(v.id, 0)) for v in videos]


@router.post("/refresh", response_model=ScanOut)
async def refresh(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Rescan the library folder (files copied directly onto the server)."""
    return await ctx.library.scan()


@router.post("/upload/check", response_model=UploadCheckOut)
def upload_check(body: UploadCheckIn, ctx: Context = Depends(get_ctx), _: User = Depends(require_operator)):
    """Pre-flight: is there room for this upload? Called before sending bytes."""
    try:
        return ctx.library.upload_check(body.filename, body.size)
    except LibraryError as exc:
        raise HTTPException(exc.status_code, str(exc))


@router.post("/upload", response_model=VideoOut, status_code=201,
             openapi_extra={"requestBody": {"content": {"application/octet-stream": {
                 "schema": {"type": "string", "format": "binary"}}}, "required": True}})
async def upload(request: Request, filename: str = Query(..., max_length=512),
                 ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Upload a video as the raw request body (streamed to disk, never held in memory).

    Example: ``curl -u user:pass -T clip.mp4 "http://server/api/videos/upload?filename=clip.mp4"``
    """
    try:
        size = int(request.headers.get("content-length", "0"))
    except ValueError:
        size = 0
    try:
        video = await ctx.library.receive_upload(filename, size, request.stream(), user.username)
    except ClientDisconnect:
        raise HTTPException(400, "Upload cancelled by the client; partial data was discarded.")
    except LibraryError as exc:
        if exc.details:
            raise HTTPException(exc.status_code, {"message": str(exc), **exc.details})
        raise HTTPException(exc.status_code, str(exc))
    return video_out(video)


def _lib_error(exc: LibraryError) -> HTTPException:
    return HTTPException(exc.status_code, {"message": str(exc), **exc.details} if exc.details else str(exc))


@router.post("/uploads", status_code=201)
def start_upload(body: UploadCheckIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Start a resumable upload: reserves disk space, returns a session id and offset 0.

    Then ``PUT /api/videos/uploads/{id}?offset=N`` with the next bytes as the raw body.
    After a network failure, ``GET`` the session to learn the offset and continue.
    """
    try:
        return ctx.library.start_session(body.filename, body.size, user.username)
    except LibraryError as exc:
        raise _lib_error(exc)


@router.get("/uploads/{session_id}")
def upload_status(session_id: str, ctx: Context = Depends(get_ctx), _: User = Depends(require_operator)):
    try:
        return ctx.library.session_info(session_id)
    except LibraryError as exc:
        raise _lib_error(exc)


@router.put("/uploads/{session_id}")
async def upload_chunk(session_id: str, request: Request, offset: int = Query(ge=0), ctx: Context = Depends(get_ctx),
                       _: User = Depends(require_operator)):
    """Append bytes at ``offset``. Returns the session (more to send) or the finished video (201)."""
    try:
        result = await ctx.library.append_chunk(session_id, offset, request.stream())
    except ClientDisconnect:
        raise HTTPException(400, "Connection lost; ask for the offset and continue.")
    except LibraryError as exc:
        raise _lib_error(exc)
    if isinstance(result, Video):
        return Response(video_out(result).model_dump_json(), status_code=201, media_type="application/json")
    return result


@router.delete("/uploads/{session_id}", status_code=204)
def abort_upload(session_id: str, ctx: Context = Depends(get_ctx), _: User = Depends(require_operator)):
    ctx.library.abort_session(session_id)
    return Response(status_code=204)


@router.delete("/{video_id}", status_code=200)
async def delete_video(video_id: int, ctx: Context = Depends(get_ctx), admin: User = Depends(require_admin)):
    """Admin: move a video to the library's .trash (purged after RTSPM_TRASH_DAYS). Refused while in use."""
    try:
        name = await ctx.library.delete_video(video_id, admin.username)
    except LibraryError as exc:
        raise _lib_error(exc)
    return {"message": f"Moved to .trash/{name}; it is purged after {ctx.settings.trash_days:g} days."}


@router.get("/{video_id}", response_model=VideoOut)
def get_video(video_id: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    v = _get(ctx, video_id)
    with ctx.db.session() as s:
        n = s.scalar(select(func.count()).select_from(Stream).where(Stream.video_id == video_id)) or 0
    return video_out(v, n)


@router.get("/{video_id}/thumbnail")
def thumbnail(video_id: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    v = _get(ctx, video_id)
    if not v.thumbnail:
        raise HTTPException(404, "No thumbnail available.")
    path = ctx.settings.thumbnail_path / v.thumbnail
    if not path.is_file():
        raise HTTPException(404, "No thumbnail available.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/{video_id}/frame")
async def frame(video_id: int, t: float = Query(ge=0), ctx: Context = Depends(get_ctx),
                _: User = Depends(current_user)):
    """JPEG preview of the frame at ``t`` seconds (for the start-time slider)."""
    v = _get(ctx, video_id)
    if v.duration_s is not None and t > v.duration_s:
        raise HTTPException(422, "t is beyond the end of the video.")
    try:
        data = await ctx.library.frame(v, t)
    except (ProbeError, LibraryError) as exc:
        raise HTTPException(422, f"Could not extract a frame: {exc}")
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})
