"""Health test, capacity limits and database backup (the dashboard's Health page)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..services.auth import User
from ..services.runtime_settings import LIMITS
from .deps import Context, current_user, get_ctx, require_admin, require_operator

router = APIRouter(tags=["health"])


@router.post("/api/health/test")
async def run_health_test(quick: bool = False, ctx: Context = Depends(get_ctx), user: User = Depends(current_user)):
    """Run every check now. ``quick=true`` skips reading the streams over RTSP."""
    return await ctx.health.run(quick=quick, username=user.username)


@router.get("/api/health/test/last")
def last_health_test(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """The newest full report and the newest automatic (quick) report, either may be null."""
    return {"full": ctx.health.last, "quick": ctx.health.last_quick,
            "auto_interval_s": ctx.settings.health_auto_interval_s}


class CapacityIn(BaseModel):
    # null = back to the RTSPM_* default
    max_active_streams: int | None = Field(default=None)
    max_transcode_streams: int | None = Field(default=None)
    reset: list[str] = Field(default_factory=list)


def _capacity(ctx: Context) -> dict:
    return {k: ctx.supervisor.limits.info(k) for k in LIMITS}


@router.get("/api/settings/capacity")
def get_capacity(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    return _capacity(ctx)


@router.put("/api/settings/capacity")
def set_capacity(body: CapacityIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    changes = []
    try:
        for key in LIMITS:
            if key in body.reset:
                ctx.supervisor.limits.set_int(key, None, user.username)
                changes.append(f"{key} back to default {getattr(ctx.settings, key)}")
            elif getattr(body, key) is not None:
                ctx.supervisor.limits.set_int(key, getattr(body, key), user.username)
                changes.append(f"{key} = {getattr(body, key)}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if changes:
        ctx.events.add("settings.capacity", "capacity limits changed: " + ", ".join(changes), username=user.username)
    return _capacity(ctx)


class CodecsIn(BaseModel):
    hevc_passthrough: bool


def _codecs(ctx: Context) -> dict:
    return {"copy_codecs": list(ctx.settings.copy_codecs) + list(ctx.library.extra_copy_codecs()),
            "hevc_passthrough": ctx.supervisor.limits.get_str("hevc_passthrough") == "on"}


@router.get("/api/settings/codecs")
def get_codecs(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Codecs sent to edges as they are (no transcoding)."""
    return _codecs(ctx)


@router.put("/api/settings/codecs")
def set_codecs(body: CodecsIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_admin)):
    """H.265 pass-through: send H.265 videos to edges unchanged (only if the edges decode H.265).
    Applies to streams started or changed after this; running streams keep their mode until restarted."""
    ctx.supervisor.limits.set_str("hevc_passthrough", "on" if body.hevc_passthrough else None, user.username)
    n = ctx.library.recheck_compat()
    ctx.events.add("settings.codecs", f"H.265 pass-through {'on' if body.hevc_passthrough else 'off'} "
                   f"({n} video(s) re-checked)", username=user.username)
    return _codecs(ctx)


@router.post("/api/backup")
async def backup_now(ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    import asyncio
    dest = await asyncio.to_thread(ctx.backups.backup_now)
    ctx.events.add("backup.done", f"database backed up to {dest.name}", username=user.username)
    return {"message": f"Database backed up ({dest.name}).", "name": dest.name}
