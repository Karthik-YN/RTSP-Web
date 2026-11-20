"""Video naming convention: the rules and codes for the dashboard, name checks, and safe renames."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services import naming, naming_db
from ..services.auth import User
from ..services.file_manager import LibraryError
from ..services.renamer import Renamer
from .deps import Context, current_user, get_ctx, require_admin, require_operator
from .videos import _get, video_out

router = APIRouter(tags=["naming"])

EXAMPLES = [
    {"name": "ALPR_toll-s1_01.mp4", "reads": "ALPR · toll plaza camera s1 · clip 1"},
    {"name": "FALL_office_01.mp4", "reads": "slip-and-fall · office · clip 1"},
    {"name": "FIRE_forest_01.mp4", "reads": "fire/smoke · forest · clip 1"},
    {"name": "JAM_dhaka_01_web.mp4", "reads": "traffic jam · Dhaka · clip 1 · downloaded from the internet"},
    {"name": "CROWD_railway_01_h264.mp4", "reads": "crowd · railway station · clip 1 · H.264 copy"},
]


def _mode(ctx: Context) -> str:
    return ctx.supervisor.limits.get_str("naming_mode") or ctx.settings.naming_mode


@router.get("/api/naming")
def convention(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Everything the Naming guide and the upload name builder show."""
    return {"format": "WHAT_WHERE_NN[_tag].ext", "pattern": naming.PATTERN, "where_max": naming.WHERE_MAX,
            "tags": list(naming.TAGS) + ["<n>fps"], "mode": _mode(ctx), "codes": naming_db.codes(ctx.db),
            "places": naming_db.places(ctx.db), "examples": EXAMPLES}


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)


@router.post("/api/naming/check")
def check(body: NameIn, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Is this a valid name? With the reasons, and a conforming suggestion when it is not."""
    codes = naming_db.code_set(ctx.db)
    result = naming.check(body.name, codes).view()
    taken = {n.lower() for n in naming_db.names_in_use(ctx.db)}
    result["taken"] = body.name.lower() in taken
    if not result["ok"]:
        try:
            result["suggestion"] = naming.suggest(body.name, codes, naming_db.names_in_use(ctx.db))
        except ValueError:
            result["suggestion"] = None
    return result


class SuggestIn(BaseModel):
    what: str = Field(min_length=2, max_length=8)
    where: str = Field(min_length=1, max_length=80)
    tags: list[str] = []
    ext: str = Field(default=".mp4", max_length=10)
    reserve: list[str] = Field(default=[], description="names already picked in the same upload batch")


@router.post("/api/naming/suggest")
def suggest(body: SuggestIn, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """The full name for WHAT + WHERE (+ tags), with the next free clip number."""
    where = naming.slug(body.where)
    if not where:
        raise HTTPException(422, "WHERE needs at least one letter or digit.")
    try:
        nn = naming.next_number(body.what, where, naming_db.names_in_use(ctx.db) + body.reserve)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    name = naming.build(body.what, where, nn, body.tags, body.ext)
    return {"name": name, **naming.check(name, naming_db.code_set(ctx.db)).view()}


class ModeIn(BaseModel):
    mode: Literal["enforce", "warn", "off"] | None = None  # None = back to RTSPM_NAMING_MODE


@router.put("/api/settings/naming")
def set_mode(body: ModeIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_admin)):
    """enforce: uploads with other names are refused; warn: accepted and flagged; off: no check. Live, no restart."""
    ctx.supervisor.limits.set_str("naming_mode", body.mode, user.username)
    ctx.events.add("settings.naming", f"upload naming check set to {_mode(ctx)}", username=user.username)
    return {"mode": _mode(ctx)}


def _lib(exc: LibraryError) -> HTTPException:
    return HTTPException(exc.status_code, {"message": str(exc), **exc.details} if exc.details else str(exc))


class RenameIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    dry_run: bool = False


@router.post("/api/videos/{video_id}/rename")
async def rename(video_id: int, body: RenameIn, ctx: Context = Depends(get_ctx),
                 user: User = Depends(require_operator)):
    """Rename a video to a convention name. Streams using it keep running; their names and URLs don't change."""
    try:
        result = await Renamer(ctx).rename(video_id, body.name, user.username, body.dry_run)
    except LibraryError as exc:
        raise _lib(exc)
    return {**result, "video": video_out(_get(ctx, video_id), admins=ctx.users.admin_names())}


class BatchItem(BaseModel):
    video_id: int
    name: str = Field(min_length=1, max_length=255)


class BatchIn(BaseModel):
    items: list[BatchItem] = Field(min_length=1, max_length=1000)
    dry_run: bool = True


@router.post("/api/videos/rename-batch")
async def rename_batch(body: BatchIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_admin)):
    """Rename many videos (e.g. from docs/naming/video-renames-proposed.csv). Dry run by default."""
    return await Renamer(ctx).batch([i.model_dump() for i in body.items], user.username, body.dry_run)
