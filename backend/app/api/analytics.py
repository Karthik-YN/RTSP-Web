"""Analytics use cases (left taskbar) and their video placeholders.

An analytics (e.g. "PPE") belongs to a category (e.g. "People") and holds
video placeholders ("slots"): a title, a description and optionally a library
video. Streams run from a slot are linked to it, so each analytics page lists
its videos and the RTSP streams running them. Deleting is refused while
something still depends on the item, so nothing is lost by accident.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..database import utcnow
from ..models import Analytics, AnalyticsCategory, AnalyticsSlot, Stream, Video
from ..schemas import TimeValue
from ..services.auth import User
from ..services.errors import ServiceError
from .deps import Context, current_user, get_ctx, require_operator
from .groups import run_bulk
from .streams import _cfg, _proc_index, stream_out
from .videos import video_out

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

MAX_CAMERAS_PER_RUN = 20


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sort: int | None = None


class AnalyticsIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    category_id: int
    description: str | None = Field(default=None, max_length=2000)


class AnalyticsPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    category_id: int | None = None
    description: str | None = Field(default=None, max_length=2000)
    sort: int | None = None


class SlotIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    video_id: int | None = None


class SlotPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    video_id: int | None = None
    clear_video: bool = False


class ReorderIn(BaseModel):
    slot_ids: list[int] = Field(min_length=1, max_length=500)


class RunIn(BaseModel):
    count: int = Field(default=1, ge=1, le=MAX_CAMERAS_PER_RUN)
    name_prefix: str | None = Field(default=None, max_length=50,
                                    description="Names become <prefix>-1, <prefix>-2, ... (default: the analytics name)")
    start: TimeValue = None
    end: TimeValue = None
    mode: Literal["loop", "once"] = "loop"
    force_transcode: bool = False
    start_now: bool = True


def slugify(name: str) -> str:
    """'Charging station' -> 'charging-station' (a valid stream-name prefix)."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "camera"


def next_names(prefix: str, taken: set[str], count: int) -> list[str]:
    """The next `count` free names <prefix>-1, <prefix>-2, ... (case-insensitive)."""
    taken = {t.lower() for t in taken}
    out, n = [], 1
    while len(out) < count:
        cand = f"{prefix}-{n}"
        if cand.lower() not in taken:
            out.append(cand)
        n += 1
    return out


def _problem(st: Stream) -> bool:
    return st.status == "ERROR" or (st.desired_state == "RUNNING" and st.status != "RUNNING")


def _viewers(ctx: Context, st: Stream) -> int:
    p = ctx.mediamtx.paths.get(st.name)
    return len(p.readers) if p else 0


def _counts(ctx: Context, streams: list[Stream]) -> dict:
    return {"streams": len(streams), "running": sum(st.status == "RUNNING" for st in streams),
            "problems": sum(_problem(st) for st in streams), "viewers": sum(_viewers(ctx, st) for st in streams)}


def _get(s, model, oid: int, what: str):
    obj = s.get(model, oid)
    if obj is None:
        raise HTTPException(404, f"{what} not found.")
    return obj


def _commit_unique(s, what: str) -> None:
    try:
        s.flush()
    except IntegrityError:
        raise HTTPException(409, f"{what} with that name already exists.")


# ------------------------------------------------------------------ tree
@router.get("")
def tree(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Categories -> analytics with counts, for the left taskbar."""
    with ctx.db.session() as s:
        cats = list(s.scalars(select(AnalyticsCategory).order_by(AnalyticsCategory.sort, AnalyticsCategory.id)))
        items = list(s.scalars(select(Analytics).order_by(Analytics.sort, Analytics.id)))
        slots = list(s.scalars(select(AnalyticsSlot)))
        streams = list(s.scalars(select(Stream).where(Stream.slot_id.is_not(None))))
    slot_owner = {sl.id: sl.analytics_id for sl in slots}
    out = []
    for c in cats:
        rows = []
        for a in (x for x in items if x.category_id == c.id):
            mine = [st for st in streams if slot_owner.get(st.slot_id) == a.id]
            rows.append({"id": a.id, "name": a.name, "description": a.description, "sort": a.sort,
                         "slots": sum(1 for sl in slots if sl.analytics_id == a.id),
                         "slot_ids": [sl.id for sl in slots if sl.analytics_id == a.id], **_counts(ctx, mine)})
        out.append({"id": c.id, "name": c.name, "sort": c.sort, "analytics": rows})
    return out


# ------------------------------------------------------------ categories
@router.post("/categories", status_code=201)
def create_category(body: CategoryIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        sort = (s.scalar(select(func.max(AnalyticsCategory.sort))) or 0) + 1
        c = AnalyticsCategory(name=body.name.strip(), sort=sort, created_at=utcnow())
        s.add(c)
        _commit_unique(s, "A category")
        out = {"id": c.id, "name": c.name, "sort": c.sort}
    ctx.events.add("analytics.category", f"category '{out['name']}' created", username=user.username)
    return out


@router.patch("/categories/{cid}")
def update_category(cid: int, body: CategoryPatch, ctx: Context = Depends(get_ctx),
                    user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        c = _get(s, AnalyticsCategory, cid, "Category")
        if body.name is not None:
            c.name = body.name.strip()
        if body.sort is not None:
            c.sort = body.sort
        _commit_unique(s, "A category")
        return {"id": c.id, "name": c.name, "sort": c.sort}


@router.delete("/categories/{cid}", status_code=204)
def delete_category(cid: int, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        c = _get(s, AnalyticsCategory, cid, "Category")
        n = s.scalar(select(func.count()).select_from(Analytics).where(Analytics.category_id == cid))
        if n:
            raise HTTPException(409, f"Category '{c.name}' still has {n} analytics; move or delete them first.")
        name = c.name
        s.delete(c)
    ctx.events.add("analytics.category", f"category '{name}' deleted", username=user.username)
    return Response(status_code=204)


# ----------------------------------------------------------------- slots
def _slot_out(ctx: Context, sl: AnalyticsSlot, video: Video | None, streams: list[Stream], proc: dict) -> dict:
    return {"id": sl.id, "analytics_id": sl.analytics_id, "title": sl.title, "description": sl.description,
            "sort": sl.sort, "video": video_out(video).model_dump(mode="json") if video else None,
            "streams": [stream_out(ctx, st, proc).model_dump(mode="json") for st in streams],
            "created_by": sl.created_by, "created_at": sl.created_at, "updated_at": sl.updated_at}


def _check_video(s, video_id: int | None) -> None:
    if video_id is not None and s.get(Video, video_id) is None:
        raise HTTPException(404, "Video not found in the library.")


@router.patch("/slots/{sid}")
def update_slot(sid: int, body: SlotPatch, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        sl = _get(s, AnalyticsSlot, sid, "Video placeholder")
        if body.title is not None:
            sl.title = body.title.strip()
        if body.description is not None:
            sl.description = body.description.strip() or None
        if body.clear_video:
            sl.video_id = None
        elif body.video_id is not None:
            _check_video(s, body.video_id)
            sl.video_id = body.video_id
        sl.updated_at = utcnow()
        s.flush()
        video = s.get(Video, sl.video_id) if sl.video_id else None
        streams = list(s.scalars(select(Stream).where(Stream.slot_id == sid).order_by(Stream.name)))
        return _slot_out(ctx, sl, video, streams, _proc_index(ctx))


@router.delete("/slots/{sid}", status_code=204)
def delete_slot(sid: int, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        sl = _get(s, AnalyticsSlot, sid, "Video placeholder")
        n = s.scalar(select(func.count()).select_from(Stream).where(Stream.slot_id == sid))
        if n:
            raise HTTPException(409, f"'{sl.title}' still has {n} stream(s); delete them first.")
        title = sl.title
        s.delete(sl)
    ctx.events.add("analytics.slot", f"video placeholder '{title}' deleted", username=user.username)
    return Response(status_code=204)


@router.post("/slots/{sid}/streams", status_code=201)
async def run_slot(sid: int, body: RunIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Create `count` streams (cameras) from the placeholder's video, named <prefix>-1..N."""
    with ctx.db.session() as s:
        sl = _get(s, AnalyticsSlot, sid, "Video placeholder")
        if sl.video_id is None:
            raise HTTPException(409, f"'{sl.title}' has no video yet; upload or choose one first.")
        a = s.get(Analytics, sl.analytics_id)
        video_id, prefix = sl.video_id, slugify(body.name_prefix or a.name)
        taken = set(s.scalars(select(Stream.name)))
    names = next_names(prefix, taken, body.count)
    cfg = _cfg(SimpleNamespace(start=body.start, end=body.end, video_id=video_id, mode=body.mode,
                               force_transcode=body.force_transcode))
    created, errors = [], []
    for name in names:
        try:
            row = await ctx.supervisor.create(name, cfg, body.start_now, user.username, slot_id=sid)
            created.append(stream_out(ctx, row))
        except ServiceError as exc:
            errors.append({"name": name, "error": exc.message})
            if not created:
                raise  # nothing created: report the error as the response
            break  # e.g. capacity reached: keep what was created, report the rest
    return {"created": created, "errors": errors, "skipped": names[len(created) + len(errors):]}


# ------------------------------------------------------------- analytics
@router.post("", status_code=201)
def create_analytics(body: AnalyticsIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        _get(s, AnalyticsCategory, body.category_id, "Category")
        sort = (s.scalar(select(func.max(Analytics.sort)).where(Analytics.category_id == body.category_id)) or 0) + 1
        a = Analytics(name=body.name.strip(), category_id=body.category_id, description=body.description,
                      sort=sort, created_at=utcnow())
        s.add(a)
        _commit_unique(s, "An analytics")
        out = {"id": a.id, "name": a.name, "category_id": a.category_id, "description": a.description}
    ctx.events.add("analytics.created", f"analytics '{out['name']}' created", username=user.username)
    return out


@router.get("/{aid}")
def detail(aid: int, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """One analytics page: its placeholders, their videos and the streams running them."""
    proc = _proc_index(ctx)
    with ctx.db.session() as s:
        a = _get(s, Analytics, aid, "Analytics")
        cat = s.get(AnalyticsCategory, a.category_id)
        slots = list(s.scalars(select(AnalyticsSlot).where(AnalyticsSlot.analytics_id == aid)
                               .order_by(AnalyticsSlot.sort, AnalyticsSlot.id)))
        ids = [sl.id for sl in slots]
        streams = list(s.scalars(select(Stream).where(Stream.slot_id.in_(ids)).order_by(Stream.name))) if ids else []
        vids = {v.id: v for v in s.scalars(select(Video).where(Video.id.in_([sl.video_id for sl in slots
                                                                              if sl.video_id])))}
        return {"id": a.id, "name": a.name, "description": a.description,
                "category": {"id": cat.id, "name": cat.name}, **_counts(ctx, streams),
                "slots": [_slot_out(ctx, sl, vids.get(sl.video_id), [st for st in streams if st.slot_id == sl.id], proc)
                          for sl in slots]}


@router.patch("/{aid}")
def update_analytics(aid: int, body: AnalyticsPatch, ctx: Context = Depends(get_ctx),
                     user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        a = _get(s, Analytics, aid, "Analytics")
        if body.name is not None:
            a.name = body.name.strip()
        if body.category_id is not None:
            _get(s, AnalyticsCategory, body.category_id, "Category")
            a.category_id = body.category_id
        if body.description is not None:
            a.description = body.description.strip() or None
        if body.sort is not None:
            a.sort = body.sort
        _commit_unique(s, "An analytics")
        return {"id": a.id, "name": a.name, "category_id": a.category_id, "description": a.description,
                "sort": a.sort}


@router.delete("/{aid}", status_code=204)
def delete_analytics(aid: int, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        a = _get(s, Analytics, aid, "Analytics")
        n = s.scalar(select(func.count()).select_from(AnalyticsSlot).where(AnalyticsSlot.analytics_id == aid))
        if n:
            raise HTTPException(409, f"'{a.name}' still has {n} video placeholder(s); remove them first.")
        name = a.name
        s.delete(a)
    ctx.events.add("analytics.deleted", f"analytics '{name}' deleted", username=user.username)
    return Response(status_code=204)


@router.post("/{aid}/slots", status_code=201)
def create_slot(aid: int, body: SlotIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        a = _get(s, Analytics, aid, "Analytics")
        _check_video(s, body.video_id)
        sort = (s.scalar(select(func.max(AnalyticsSlot.sort)).where(AnalyticsSlot.analytics_id == aid)) or 0) + 1
        now = utcnow()
        sl = AnalyticsSlot(analytics_id=aid, title=body.title.strip(), description=(body.description or "").strip()
                           or None, video_id=body.video_id, sort=sort, created_by=user.username,
                           created_at=now, updated_at=now)
        s.add(sl)
        s.flush()
        video = s.get(Video, sl.video_id) if sl.video_id else None
        out = _slot_out(ctx, sl, video, [], {})
        aname = a.name
    ctx.events.add("analytics.slot", f"video placeholder '{out['title']}' added to {aname}", username=user.username)
    return out


@router.post("/{aid}/slots/reorder")
def reorder_slots(aid: int, body: ReorderIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        _get(s, Analytics, aid, "Analytics")
        slots = {sl.id: sl for sl in s.scalars(select(AnalyticsSlot).where(AnalyticsSlot.analytics_id == aid))}
        if set(body.slot_ids) != set(slots):
            raise HTTPException(422, "slot_ids must list every placeholder of this analytics exactly once.")
        for i, sid in enumerate(body.slot_ids):
            slots[sid].sort = i
    return {"ok": True}


def _stream_ids(ctx: Context, aid: int) -> tuple[str, list[Stream]]:
    with ctx.db.session() as s:
        a = _get(s, Analytics, aid, "Analytics")
        q = (select(Stream).join(AnalyticsSlot, Stream.slot_id == AnalyticsSlot.id)
             .where(AnalyticsSlot.analytics_id == aid).order_by(Stream.name))
        return a.name, list(s.scalars(q))


@router.get("/{aid}/urls", response_class=PlainTextResponse)
def analytics_urls(aid: int, format: Literal["txt", "csv"] = Query(default="txt"), ctx: Context = Depends(get_ctx),
                   _: User = Depends(current_user)):
    """RTSP URLs of an analytics' streams, one per line (txt) or name,url (csv), for the portal."""
    _, rows = _stream_ids(ctx, aid)
    if format == "csv":
        return "name,rtsp_url\n" + "".join(f"{r.name},{ctx.settings.public_rtsp_url(r.name)}\n" for r in rows)
    return "".join(f"{ctx.settings.public_rtsp_url(r.name)}\n" for r in rows)


@router.post("/{aid}/{action}")
async def analytics_action(aid: int, action: Literal["start", "stop", "restart"], ctx: Context = Depends(get_ctx),
                           user: User = Depends(require_operator)):
    name, rows = _stream_ids(ctx, aid)
    results = await run_bulk(ctx, [r.id for r in rows], action, user)
    ok = sum(r["ok"] for r in results)
    ctx.events.add("analytics.action", f"{action} all of '{name}': {ok}/{len(results)} ok", username=user.username)
    return {"ok": ok, "failed": len(results) - ok, "results": results}
