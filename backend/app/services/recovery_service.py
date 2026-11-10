"""Startup (reboot) recovery.

Sequence on backend start (systemd starts MediaMTX first, then us):
 1. reap FFmpeg publishers orphaned by a previous backend instance;
 2. normalise runtime status left in the DB by an unclean shutdown;
 3. wait for the MediaMTX Control API (start it if we manage it);
 4. republish every stream whose desired_state is RUNNING, in parallel with
    bounded concurrency (transcodes further limited);
 5. verify each one through MediaMTX (``ready``), record timings relative to
    OS boot so the 2-minute target can be evidenced.
Stopped streams stay stopped; completed play-once streams stay completed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import datetime, timezone

import psutil
from sqlalchemy import select

from ..database import utcnow
from ..models import DesiredState, Stream, StreamStatus
from .mediamtx_service import UP
from .stream_supervisor import ACTIVE_STATUSES, StreamSupervisor

log = logging.getLogger(__name__)


class RecoveryService:
    def __init__(self, supervisor: StreamSupervisor):
        self.sup = supervisor
        self.report: dict = {"state": "pending"}

    async def run(self) -> dict:
        try:
            return await self._run()
        finally:
            self.sup.recovering = False  # hand over to normal supervision

    async def _run(self) -> dict:
        sup = self.sup
        t0 = time.monotonic()
        backend_started = utcnow()
        boot = datetime.fromtimestamp(psutil.boot_time(), timezone.utc)
        self.report = {"state": "running", "boot_time": boot.isoformat(),
                       "backend_started_at": backend_started.isoformat(),
                       "backend_start_after_boot_s": round((backend_started - boot).total_seconds(), 1)}
        # 1. Normalize the status left by the previous instance at once, so the
        #    dashboard never shows a stale RUNNING while recovery is working.
        with sup.db.session() as s:
            for r in s.scalars(select(Stream)):
                if r.desired_state == DesiredState.RUNNING:
                    r.status = StreamStatus.STARTING
                    r.status_detail = "Recovering after backend start"
                elif r.status in ACTIVE_STATUSES:
                    r.status = StreamStatus.STOPPED
                    r.status_detail = None
                    r.pid = None
                    r.running_since = None
        # 2. Re-attach publishers that kept running; 3. kill everything else.
        adopted = await sup.adopt_running()
        orphans = sup.reap_orphans()
        with sup.db.session() as s:
            ids = [r.id for r in s.scalars(select(Stream).where(Stream.desired_state == DesiredState.RUNNING))
                   if r.id not in adopted]

        mtx = sup.mtx
        await mtx.poll_once()
        if mtx.state != UP and mtx.controller.mode != "external":
            try:
                mtx.last_control_message = await mtx.controller.start()
            except Exception as exc:  # reported, recovery keeps waiting
                mtx.last_control_message = f"start failed: {exc}"
                log.error("could not start MediaMTX: %s", exc)
        mtx_ok = await mtx.wait_up(sup.s.recovery_mediamtx_wait_s)
        mtx_ready = utcnow()
        self.report.update({
            "orphans_reaped": orphans,
            "publishers_adopted": len(adopted),
            "streams_to_recover": len(ids),
            "start_spacing_s": sup.gate.spacing_s,
            "mediamtx_ready": mtx_ok,
            "mediamtx_ready_after_boot_s": round((mtx_ready - boot).total_seconds(), 1) if mtx_ok else None,
        })
        if not mtx_ok:
            log.error("MediaMTX not available after %.0fs; streams will start when it comes up",
                      sup.s.recovery_mediamtx_wait_s)
            sup.events.add("recovery.blocked", "MediaMTX unavailable; recovery deferred", level="ERROR")
            self.report["state"] = "waiting_for_mediamtx"
            return self.report

        results = await sup.start_many(ids, reason="boot-recovery") if ids else []
        # Another path (e.g. the MediaMTX "up" handler) may have started some of
        # these first ("skipped" here): report every stream's actual state.
        for r in results:
            rt = sup.runtimes.get(r["id"])
            if rt and rt.ready_task and not rt.ready_task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(rt.ready_task), sup.s.startup_timeout_s)
            try:
                r["result"] = sup.get_row(r["id"]).status
            except Exception:
                continue
            if rt and rt.ready_at:
                r["ready_at"] = rt.ready_at.isoformat()
            if r.get("ready_at"):
                ready = datetime.fromisoformat(r["ready_at"])
                r["ready_after_boot_s"] = round((ready - boot).total_seconds(), 1)
        # A stream in its lead-in already serves its URL (the slate); the video follows.
        on_air = (StreamStatus.RUNNING, StreamStatus.LEAD_IN, StreamStatus.WAITING_FOR_VIEWER)
        ok = [r for r in results if r.get("result") in on_air]
        self.report.update({
            "state": "done",
            "streams_running": len(ok),
            "results": results,
            "recovery_duration_s": round(time.monotonic() - t0, 2),
            "all_ready_after_boot_s": max((r.get("ready_after_boot_s") or 0) for r in ok) if ok else None,
            "finished_at": utcnow().isoformat(),
        })
        sup.events.add("recovery.done",
                       f"{len(ok)}/{len(ids)} streams running; last ready "
                       f"{self.report['all_ready_after_boot_s']}s after OS boot",
                       data={k: v for k, v in self.report.items() if k != "results"})
        return self.report
