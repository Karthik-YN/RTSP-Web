"""FastAPI application factory and process lifecycle.

Run (development):  uvicorn app.main:app --port 8080   (from backend/)
Production: deployment/rtsp-manager.service (one worker; this process owns
the FFmpeg publishers, so it must never run with several workers).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware

from . import __version__
from .api import routers
from .api.deps import Context
from .config import Settings, get_settings
from .database import Database
from .services.auth import LoginGuard, UserStore, load_secret
from .services.errors import ServiceError
from .services.alerts import AlertSender
from .services.cf_access import AccessError, CloudflareAccess
from .services.health_test import BackupService, HealthTester
from .services.network_info import AddressWatcher
from .services.events import EventLog
from .services.file_manager import LibraryError
from .services.ffprobe_service import ffmpeg_version
from .services.mediamtx_service import MediaMTXService
from .services.recovery_service import RecoveryService
from .services.stream_supervisor import StreamSupervisor
from .services.system_monitor import SystemMonitor
from .services.video_library import VideoLibrary

log = logging.getLogger("rtspm")


class InstanceLock:
    """Only one backend may own the publishers for a data directory."""

    def __init__(self, path: Path):
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise RuntimeError(f"Another rtsp-manager backend is already running ({self.path}).")
        os.ftruncate(self.fd, 0)
        os.write(self.fd, str(os.getpid()).encode())

    def release(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
                os.close(self.fd)
            self.fd = None


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs every MediaMTX health poll (every 2 s) at INFO: keep the journal readable
    logging.getLogger("httpx").setLevel(logging.WARNING)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        os.umask(0o027)
        settings.ensure_dirs()
        lock = InstanceLock(settings.data_dir / "backend.lock")
        lock.acquire()
        db = Database(settings.database_path)
        db.migrate()
        db.secure_permissions()
        alerts = AlertSender(settings.alert_webhook_url, settings.alert_min_interval_s,
                             settings.dashboard_url, f"RIG-02 {settings.public_rtsp_host}")
        events = EventLog(db, alerts)
        library = VideoLibrary(settings, db, events)
        removed = library.cleanup_stale_uploads()
        mtx = MediaMTXService(settings)
        sup = StreamSupervisor(settings, db, mtx, library, events)
        recovery = RecoveryService(sup)
        sysmon = SystemMonitor(settings)
        users = UserStore(settings.users_path)
        secret = load_secret(settings.secret_key, settings.data_dir / "secret_key")
        network = AddressWatcher(settings, events, settings.address_check_interval_s)
        app.state.ctx = Context(settings, db, events, library, mtx, sup, recovery, sysmon, users, secret,
                                LoginGuard(), network)
        ctx = app.state.ctx
        ctx.backups = BackupService(settings, events)
        ctx.health = HealthTester(ctx)

        if settings.auth_enabled and users.count() == 0:
            log.warning("Authentication is enabled but %s has no users; create one with "
                        "scripts/manage_users.py", settings.users_path)
        log.info("RTSP Stream Manager %s starting; %s; public RTSP host %s",
                 __version__, ffmpeg_version(settings.ffmpeg_bin), settings.public_rtsp_host)
        events.add("backend.start", f"backend {__version__} starting (removed {removed} stale upload(s))")

        sysmon.start()
        network.start()
        ctx.backups.start()
        ctx.health.start(settings.health_auto_interval_s)
        mtx.start_monitor()
        sup.start_monitor()
        library.start_scanner()
        recovery_task = asyncio.create_task(recovery.run(), name="recovery")

        async def warn_loop():
            # Turn new resource warnings (CPU, RAM, disk, capacity) into events -> alerts.
            seen: set[str] = set()
            while True:
                await asyncio.sleep(30)
                current = set(sysmon.warnings())
                for w in sorted(current - seen):
                    events.add("system.warning", w, level="WARNING")
                seen = current

        warn_task = asyncio.create_task(warn_loop(), name="warnings")
        try:
            yield
        finally:
            log.info("shutting down: stopping publishers (desired state is kept for the next start)")
            recovery_task.cancel()
            warn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await recovery_task
            await sup.shutdown()
            await library.stop()
            await sysmon.stop()
            await network.stop()
            await ctx.health.stop()
            await ctx.backups.stop()
            await mtx.stop()
            events.add("backend.stop", "backend stopped")
            alerts.close()
            db.dispose()
            lock.release()

    app = FastAPI(
        title="RIG-02 RTSP Stream Manager",
        version=__version__,
        description="Publishes recorded videos as live RTSP cameras through MediaMTX.",
        lifespan=lifespan,
    )

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message, **exc.details}, status_code=exc.status_code)

    @app.exception_handler(LibraryError)
    async def _library_error(_: Request, exc: LibraryError):
        return JSONResponse({"detail": str(exc), **exc.details}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
        return JSONResponse({"detail": "; ".join(msgs), "errors": exc.errors()}, status_code=422)

    cf = CloudflareAccess(settings.cf_access_team, settings.cf_access_aud, settings.cf_access_mode)
    app.state.cf_access = cf

    @app.middleware("http")
    async def _cloudflare_access(request: Request, call_next):
        try:
            ident = await cf.check(request.headers, request.cookies)
        except AccessError as exc:
            log.warning("refused %s %s from %s: %s", request.method, request.url.path,
                        request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?"), exc)
            return JSONResponse({"detail": str(exc)}, status_code=403)
        request.state.cf_email = ident.email if ident else None
        return await call_next(request)

    @app.middleware("http")
    async def _headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        if request.url.path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    # Compress JSON and the dashboard's assets (the stream list is ~2 KB per stream,
    # polled every 2 s: matters through Cloudflare); never the live-preview video.
    app.add_middleware(_SelectiveGZip, minimum_size=1024)

    for r in routers:
        app.include_router(r)

    dist = settings.frontend_dist or (Path(__file__).resolve().parents[2] / "frontend" / "dist")
    if (dist / "index.html").is_file():
        if (dist / "assets").is_dir():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        async def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "Not found")
            candidate = (dist / full_path).resolve()
            if full_path and candidate.is_file() and candidate.is_relative_to(dist.resolve()):
                return FileResponse(candidate)
            return FileResponse(dist / "index.html", headers={"Cache-Control": "no-cache"})

    return app


class _SelectiveGZip:
    """GZip for everything except video (live preview and video files): already
    compressed, and gzip would break HTTP Range requests used for seeking."""

    def __init__(self, app, minimum_size: int = 1024):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=minimum_size)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and "/live/" not in path and not path.endswith(("/file", "/preview")):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


# Creating the app does no I/O; directories, DB and processes are set up in lifespan.
app = create_app()
