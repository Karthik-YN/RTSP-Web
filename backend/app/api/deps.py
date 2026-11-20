"""Shared API dependencies: application context and authentication."""

from __future__ import annotations

import base64
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Request, status

from ..config import Settings
from ..database import Database
from ..services.auth import LoginGuard, User, UserStore, client_ip, read_token
from ..services.events import EventLog
from ..services.mediamtx_service import MediaMTXService
from ..services.network_info import AddressWatcher, host_from_header, rtsp_hosts
from ..services.recovery_service import RecoveryService
from ..services.stream_supervisor import StreamSupervisor
from ..services.system_monitor import SystemMonitor
from ..services.video_library import VideoLibrary

SESSION_COOKIE = "rtspm_session"


@dataclass
class Context:
    settings: Settings
    db: Database
    events: EventLog
    library: VideoLibrary
    mediamtx: MediaMTXService
    supervisor: StreamSupervisor
    recovery: RecoveryService
    sysmon: SystemMonitor
    users: UserStore
    secret: bytes
    throttle: LoginGuard
    network: AddressWatcher | None = None
    health: object = None   # services.health_test.HealthTester
    backups: object = None  # services.health_test.BackupService
    live_previews: object = None  # services.live_preview.LivePreviews


def get_ctx(request: Request) -> Context:
    return request.app.state.ctx


def current_user(request: Request, ctx: Context = Depends(get_ctx)) -> User:
    cf_email = getattr(request.state, "cf_email", None)
    if not ctx.settings.auth_enabled:
        # No app login: a verified Cloudflare Access identity still names the actor.
        return User(cf_email or "anonymous", "admin")
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        username = read_token(ctx.secret, token)
        if username:
            user = ctx.users.get(username)
            if user:
                return user
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            raw = base64.b64decode(auth[6:]).decode()
            username, _, password = raw.partition(":")
        except (ValueError, UnicodeDecodeError):
            username = password = ""
        keys = (client_ip(request), username)
        if ctx.throttle.retry_after(*keys) > 0:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many failed logins; try again later.")
        user = ctx.users.authenticate(username, password) if username else None
        if user:
            ctx.throttle.success(*keys)
            return user
        if username:
            ctx.throttle.failure(*keys)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Login required.")


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only administrators can do this.")
    return user


def require_operator(user: User = Depends(current_user)) -> User:
    if not user.can_write:
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            f"User '{user.username}' has the read-only 'viewer' role.")
    return user


def if_match(if_match: str | None = Header(default=None, alias="If-Match")) -> int | None:
    """Optimistic concurrency: the stream version the client last saw."""
    if if_match is None or if_match.strip() in ("", "*"):
        return None
    try:
        return int(if_match.strip().strip('"').removeprefix("W/").strip('"'))
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "If-Match must be the stream version number.")


def request_rtsp_hosts(request: Request, ctx: Context) -> list[dict]:
    """Address choices for this request (see services/network_info.py)."""
    via_tunnel = "cf-ray" in request.headers or "cf-connecting-ip" in request.headers
    return rtsp_hosts(ctx.settings, host_from_header(request.headers.get("host")), via_tunnel)


def chosen_host(request: Request, ctx: Context, host: str | None) -> str | None:
    """Validate an export's ``?host=`` against the offered choices (never an arbitrary host)."""
    if not host:
        return None
    if host.lower().rstrip(".") not in {h["host"].lower().rstrip(".") for h in request_rtsp_hosts(request, ctx)}:
        raise HTTPException(400, f"'{host}' is not one of this server's RTSP addresses.")
    return host
