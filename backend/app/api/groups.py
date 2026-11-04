"""Stream groups ("demo sets") and bulk actions.

A group is a named set of streams (e.g. "Store demo — 16 cameras") that can be
started, stopped or restarted together, and whose RTSP URLs can be exported
for the portal in one go. Bulk actions respect protected streams and report a
result per stream instead of failing as a whole.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..database import utcnow
from ..models import Stream, StreamGroup
from ..services.auth import User
from ..services.errors import ServiceError
from .deps import Context, current_user, get_ctx, require_operator
from .streams import guard

router = APIRouter(tags=["groups"])


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=500)
    stream_ids: list[int] = []


class BulkIn(BaseModel):
    stream_ids: list[int] = Field(min_length=1, max_length=200)
    action: Literal["start", "stop", "restart", "delete"]


class MembersIn(BaseModel):
    stream_ids: list[int]
    group_id: int | None  # None removes them from their group


def _group_out(g: StreamGroup, members: list[Stream]) -> dict:
    return {"id": g.id, "name": g.name, "description": g.description, "created_by": g.created_by,
            "created_at": g.created_at, "stream_ids": [m.id for m in members],
            "running": sum(1 for m in members if m.status == "RUNNING"), "total": len(members)}


@router.get("/api/groups")
def list_groups(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    with ctx.db.session() as s:
        groups = list(s.scalars(select(StreamGroup).order_by(StreamGroup.name)))
        streams = list(s.scalars(select(Stream)))
    return [_group_out(g, [x for x in streams if x.group_id == g.id]) for g in groups]


@router.post("/api/groups", status_code=201)
def create_group(body: GroupIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    try:
        with ctx.db.session() as s:
            g = StreamGroup(name=body.name.strip(), description=body.description, created_by=user.username,
                            created_at=utcnow())
            s.add(g)
            s.flush()
            for st in s.scalars(select(Stream).where(Stream.id.in_(body.stream_ids))):
                st.group_id = g.id
            gid = g.id
    except IntegrityError:
        raise HTTPException(409, f"A group named '{body.name}' already exists.")
    ctx.events.add("group.created", f"group '{body.name}' with {len(body.stream_ids)} stream(s)", username=user.username)
    return _get(ctx, gid)


def _get(ctx: Context, gid: int) -> dict:
    with ctx.db.session() as s:
        g = s.get(StreamGroup, gid)
        if g is None:
            raise HTTPException(404, "Group not found.")
        return _group_out(g, list(s.scalars(select(Stream).where(Stream.group_id == gid))))


@router.post("/api/groups/members")
def set_members(body: MembersIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Add streams to a group (or remove them from their group with group_id=null)."""
    with ctx.db.session() as s:
        if body.group_id is not None and s.get(StreamGroup, body.group_id) is None:
            raise HTTPException(404, "Group not found.")
        n = 0
        for st in s.scalars(select(Stream).where(Stream.id.in_(body.stream_ids))):
            st.group_id = body.group_id
            n += 1
    return {"updated": n}


@router.delete("/api/groups/{gid}", status_code=204)
def delete_group(gid: int, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Deletes the group only; its streams keep running and become ungrouped."""
    with ctx.db.session() as s:
        g = s.get(StreamGroup, gid)
        if g is None:
            raise HTTPException(404, "Group not found.")
        for st in s.scalars(select(Stream).where(Stream.group_id == gid)):
            st.group_id = None
        name = g.name
        s.delete(g)
    ctx.events.add("group.deleted", f"group '{name}' deleted (streams kept)", username=user.username)


@router.get("/api/groups/{gid}/urls", response_class=PlainTextResponse)
def group_urls(gid: int, format: Literal["txt", "csv"] = Query(default="txt"), ctx: Context = Depends(get_ctx),
               _: User = Depends(current_user)):
    """RTSP URLs of a group, one per line (txt) or name,url (csv), for the portal or measure_recovery.py."""
    with ctx.db.session() as s:
        if s.get(StreamGroup, gid) is None:
            raise HTTPException(404, "Group not found.")
        rows = list(s.scalars(select(Stream).where(Stream.group_id == gid).order_by(Stream.name)))
    if format == "csv":
        return "name,rtsp_url\n" + "".join(f"{r.name},{ctx.settings.public_rtsp_url(r.name)}\n" for r in rows)
    return "".join(f"{ctx.settings.public_rtsp_url(r.name)}\n" for r in rows)


async def run_bulk(ctx: Context, ids: list[int], action: str, user: User) -> list[dict]:
    sup = ctx.supervisor
    sem = asyncio.Semaphore(ctx.settings.recovery_concurrency)

    async def one(sid: int) -> dict:
        async with sem:
            try:
                row = sup.get_row(sid)
                guard(row, user, action)
                if action == "start":
                    if row.desired_state == "RUNNING":
                        return {"id": sid, "name": row.name, "ok": True, "result": "already running"}
                    await sup.start(sid, None, user.username)
                elif action == "stop":
                    await sup.stop(sid, None, user.username)
                elif action == "restart":
                    await sup.restart(sid, None, user.username)
                else:
                    await sup.delete(sid, None, user.username)
                return {"id": sid, "name": row.name, "ok": True, "result": action}
            except HTTPException as exc:
                return {"id": sid, "ok": False, "error": exc.detail}
            except ServiceError as exc:
                return {"id": sid, "ok": False, "error": exc.message}

    return list(await asyncio.gather(*(one(i) for i in ids)))


@router.post("/api/streams/bulk")
async def bulk(body: BulkIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    results = await run_bulk(ctx, body.stream_ids, body.action, user)
    ok = sum(r["ok"] for r in results)
    ctx.events.add("streams.bulk", f"{body.action} on {len(results)} stream(s): {ok} ok", username=user.username)
    return {"ok": ok, "failed": len(results) - ok, "results": results}


@router.post("/api/groups/{gid}/{action}")
async def group_action(gid: int, action: Literal["start", "stop", "restart"], ctx: Context = Depends(get_ctx),
                       user: User = Depends(require_operator)):
    with ctx.db.session() as s:
        g = s.get(StreamGroup, gid)
        if g is None:
            raise HTTPException(404, "Group not found.")
        ids = [r.id for r in s.scalars(select(Stream).where(Stream.group_id == gid))]
        name = g.name
    results = await run_bulk(ctx, ids, action, user)
    ok = sum(r["ok"] for r in results)
    ctx.events.add("group.action", f"{action} group '{name}': {ok}/{len(results)} ok", username=user.username)
    return {"ok": ok, "failed": len(results) - ok, "results": results}
