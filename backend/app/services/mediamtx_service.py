"""MediaMTX health monitoring, inspection and control.

* Health = the local Control API answers (``/v3/paths/list``). Polled every
  ``health_interval_s``; ``health_fail_threshold`` consecutive failures mark
  MediaMTX DOWN (default 2 s x 2 => detected in <= ~5 s, target is 10 s).
* The path list is the source of truth for "is this stream actually being
  published" (a live FFmpeg PID is not proof of a healthy RTSP path).
* Control modes: ``systemd`` (systemctl via a polkit rule, no root),
  ``process`` (backend owns a MediaMTX child; dev/tests), ``external``.
* MediaMTX restarting does NOT bring publishers back by itself: the
  supervisor is notified through ``on_down``/``on_up`` callbacks and
  republishes every stream whose desired state is RUNNING.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx

from ..config import Settings

log = logging.getLogger(__name__)

UP, DOWN, UNKNOWN = "UP", "DOWN", "UNKNOWN"


@dataclass
class PathInfo:
    name: str
    ready: bool
    ready_time: str | None
    source_type: str | None
    tracks: list[str]
    bytes_received: int
    bytes_sent: int
    readers: list[dict] = field(default_factory=list)
    ingest_bps: float | None = None  # publisher -> MediaMTX
    egress_bps: float | None = None  # MediaMTX -> all readers


class ControlError(RuntimeError):
    pass


class MediaMTXController:
    """Starts/restarts MediaMTX according to ``mediamtx_control``."""

    def __init__(self, settings: Settings):
        self.s = settings
        self.proc: asyncio.subprocess.Process | None = None
        self._log_file = None
        self._lock = asyncio.Lock()

    @property
    def mode(self) -> str:
        return self.s.mediamtx_control

    async def _systemctl(self, *args: str, timeout: float = 30.0) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self.s.systemctl_bin, "--no-ask-password", *args,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ControlError(f"systemctl {' '.join(args)} timed out")
        return proc.returncode or 0, out.decode(errors="replace").strip()

    async def is_active(self) -> bool | None:
        if self.mode == "systemd":
            try:
                rc, out = await self._systemctl("is-active", self.s.mediamtx_service_name, timeout=10)
            except (ControlError, FileNotFoundError):
                return None
            return out.strip() == "active"
        if self.mode == "process":
            return self.proc is not None and self.proc.returncode is None
        return None

    async def start(self) -> str:
        async with self._lock:
            if self.mode == "systemd":
                rc, out = await self._systemctl("start", self.s.mediamtx_service_name)
                if rc != 0:
                    raise ControlError(f"systemctl start failed ({rc}): {out[-300:]}")
                return "start requested via systemd"
            if self.mode == "process":
                if self.proc is not None and self.proc.returncode is None:
                    return "already running"
                await self._spawn()
                return f"started MediaMTX process (pid {self.proc.pid})"
            raise ControlError("MediaMTX is managed externally (RTSPM_MEDIAMTX_CONTROL=external).")

    async def restart(self) -> str:
        async with self._lock:
            if self.mode == "systemd":
                rc, out = await self._systemctl("restart", self.s.mediamtx_service_name)
                if rc != 0:
                    raise ControlError(f"systemctl restart failed ({rc}): {out[-300:]}")
                return "restart requested via systemd"
            if self.mode == "process":
                await self._terminate()
                await self._spawn()
                return f"restarted MediaMTX process (pid {self.proc.pid})"
            raise ControlError("MediaMTX is managed externally (RTSPM_MEDIAMTX_CONTROL=external).")

    async def _spawn(self) -> None:
        if not self.s.mediamtx_config:
            raise ControlError("RTSPM_MEDIAMTX_CONFIG must be set for process control mode.")
        log_path = self.s.log_path / "mediamtx.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if self._log_file is None:
            self._log_file = open(log_path, "ab")
        # Own working directory: MediaMTX may write files (e.g. auto-generated
        # TLS keys for optional servers) into its cwd.
        cwd = self.s.run_path / "mediamtx"
        cwd.mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            self.s.mediamtx_bin, str(self.s.mediamtx_config.resolve()),
            stdin=asyncio.subprocess.DEVNULL, stdout=self._log_file, stderr=self._log_file,
            start_new_session=True, cwd=cwd,
        )

    async def _terminate(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                await self.proc.wait()

    async def shutdown(self) -> None:
        if self.mode == "process":
            await self._terminate()
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    @property
    def pid(self) -> int | None:
        if self.mode == "process" and self.proc and self.proc.returncode is None:
            return self.proc.pid
        return None


Callback = Callable[[], Awaitable[None]]


class MediaMTXService:
    def __init__(self, settings: Settings):
        self.s = settings
        self.controller = MediaMTXController(settings)
        self.client = httpx.AsyncClient(base_url=settings.mediamtx_api_url,
                                        timeout=settings.health_request_timeout_s)
        self.state = UNKNOWN
        self.consecutive_failures = 0
        self.last_ok_at: datetime | None = None
        self.last_ok_mono: float | None = None
        self.down_since: datetime | None = None
        self.down_detected_at: datetime | None = None
        self.last_detection_s: float | None = None
        self.last_error: str | None = None
        self.paths: dict[str, PathInfo] = {}
        self.recoveries = 0
        self.last_recovery: dict | None = None
        self.last_control_message: str | None = None
        self.on_down: list[Callback] = []
        self.on_up: list[Callback] = []
        self._task: asyncio.Task | None = None
        self._last_recover_attempt = 0.0
        self._stopping = False
        self._first_fail_mono: float | None = None
        self.up_event = asyncio.Event()
        self._prev_bytes: dict[str, tuple[float, int, int]] = {}

    # ------------------------------------------------------------ polling
    async def fetch(self) -> dict[str, PathInfo]:
        r = await self.client.get("/v3/paths/list", params={"itemsPerPage": 10000})
        r.raise_for_status()
        items = r.json().get("items", [])
        sessions: dict[str, dict] = {}
        with contextlib.suppress(Exception):
            rs = await self.client.get("/v3/rtspsessions/list", params={"itemsPerPage": 10000})
            if rs.status_code == 200:
                sessions = {x["id"]: x for x in rs.json().get("items", [])}
        paths = {}
        for it in items:
            readers = []
            for rd in it.get("readers") or []:
                sess = sessions.get(rd.get("id"), {})
                readers.append({
                    "type": rd.get("type"), "id": rd.get("id"),
                    "remote_addr": sess.get("remoteAddr"),
                    "since": sess.get("created"),
                    "bytes_sent": sess.get("bytesSent"),
                    "transport": sess.get("transport"),
                })
            src = it.get("source") or {}
            paths[it["name"]] = PathInfo(
                name=it["name"],
                ready=bool(it.get("ready", it.get("available", False))),
                ready_time=it.get("readyTime") or it.get("availableTime"),
                source_type=src.get("type"),
                tracks=list(it.get("tracks") or []),
                bytes_received=int(it.get("bytesReceived") or 0),
                bytes_sent=int(it.get("bytesSent") or 0),
                readers=readers,
            )
        return paths

    async def poll_once(self) -> None:
        try:
            paths = await self.fetch()
        except Exception as exc:  # any failure to talk to the API = unhealthy
            self.consecutive_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            if self._first_fail_mono is None:
                self._first_fail_mono = time.monotonic()
            if self.consecutive_failures >= self.s.health_fail_threshold and self.state != DOWN:
                await self._mark_down()
            if self.state == DOWN:
                await self._maybe_recover()
            return
        now_m = time.monotonic()
        for name, pinfo in paths.items():
            prev = self._prev_bytes.get(name)
            if prev and now_m > prev[0] and pinfo.bytes_received >= prev[1] and pinfo.bytes_sent >= prev[2]:
                dt = now_m - prev[0]
                pinfo.ingest_bps = (pinfo.bytes_received - prev[1]) * 8 / dt
                pinfo.egress_bps = (pinfo.bytes_sent - prev[2]) * 8 / dt
            elif prev and name in self.paths:  # keep last rate between closely spaced polls
                pinfo.ingest_bps, pinfo.egress_bps = self.paths[name].ingest_bps, self.paths[name].egress_bps
            if not prev or now_m - prev[0] >= 1.5:
                self._prev_bytes[name] = (now_m, pinfo.bytes_received, pinfo.bytes_sent)
        self._prev_bytes = {k: v for k, v in self._prev_bytes.items() if k in paths}
        self.paths = paths
        self.consecutive_failures = 0
        self._first_fail_mono = None
        seen_up_before = self.last_ok_at is not None
        self.last_ok_at = datetime.now(timezone.utc)
        self.last_ok_mono = time.monotonic()
        self.last_error = None
        self._last_recover_attempt = 0.0  # a new outage may recover immediately
        if self.state != UP:
            was_down = self.state == DOWN
            self.state = UP
            self.up_event.set()
            if was_down and seen_up_before:  # not merely "not up yet" during boot
                outage = None
                if self.down_since:
                    outage = (datetime.now(timezone.utc) - self.down_since).total_seconds()
                self.recoveries += 1
                self.last_recovery = {"recovered_at": self.last_ok_at.isoformat(), "outage_s": outage}
                log.warning("MediaMTX is back up (outage %.1fs)", outage or -1)
            self.down_since = None
            for cb in self.on_up:
                await _safe(cb)

    async def _mark_down(self) -> None:
        self.state = DOWN
        self.up_event.clear()
        now = datetime.now(timezone.utc)
        # Down since the last successful poll (best estimate of the failure time)
        self.down_since = self.last_ok_at or now
        self.down_detected_at = now
        self.last_detection_s = (now - self.down_since).total_seconds()
        log.error("MediaMTX DOWN (detected after %.1fs): %s", self.last_detection_s, self.last_error)
        for cb in self.on_down:
            await _safe(cb)

    async def _maybe_recover(self) -> None:
        if not self.s.mediamtx_auto_recover or self.controller.mode == "external":
            return
        now = time.monotonic()
        if now - self._last_recover_attempt < 5:  # give a (re)start time to come up
            return
        self._last_recover_attempt = now
        try:
            active = await self.controller.is_active()
            # Process gone / unit inactive -> start. Present but unresponsive
            # for a while -> restart (hung process).
            if active is False or active is None:
                self.last_control_message = await self.controller.start()
            elif self._first_fail_mono and now - self._first_fail_mono > 15:
                self.last_control_message = await self.controller.restart()
            else:
                self.last_control_message = "service active; waiting for API (systemd may be restarting it)"
        except Exception as exc:
            self.last_control_message = f"recovery failed: {exc}"
            log.error("MediaMTX recovery failed: %s", exc)

    async def run(self) -> None:
        while not self._stopping:
            await self.poll_once()
            await asyncio.sleep(self.s.health_interval_s)

    def start_monitor(self) -> None:
        self._task = asyncio.create_task(self.run(), name="mediamtx-monitor")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self.controller.shutdown()
        await self.client.aclose()

    async def wait_up(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.up_event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ------------------------------------------------------------ queries
    def path(self, name: str) -> PathInfo | None:
        return self.paths.get(name)

    def total_bps(self, names: set[str] | None = None) -> tuple[float, float]:
        ps = [p for n, p in self.paths.items() if names is None or n in names]
        return (sum(p.ingest_bps or 0 for p in ps), sum(p.egress_bps or 0 for p in ps))

    def total_readers(self, names: set[str] | None = None) -> int:
        return sum(len(p.readers) for n, p in self.paths.items() if names is None or n in names)

    def status(self) -> dict:
        return {
            "state": self.state,
            "api_url": self.s.mediamtx_api_url,
            "control_mode": self.controller.mode,
            "auto_recover": self.s.mediamtx_auto_recover,
            "consecutive_failures": self.consecutive_failures,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "down_since": self.down_since.isoformat() if self.down_since else None,
            "down_detected_at": self.down_detected_at.isoformat() if self.down_detected_at else None,
            "last_detection_s": self.last_detection_s,
            "last_error": self.last_error,
            "recoveries": self.recoveries,
            "last_recovery": self.last_recovery,
            "last_control_message": self.last_control_message,
            "managed_pid": self.controller.pid,
            "path_count": len(self.paths),
            "ready_paths": sum(1 for p in self.paths.values() if p.ready),
            "total_readers": self.total_readers(),
        }


async def _safe(cb: Callback) -> None:
    try:
        await cb()
    except Exception:  # pragma: no cover - logged, never fatal
        log.exception("MediaMTX callback failed")
