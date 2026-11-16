"""On-demand and periodic health test of the whole rig.

One source of truth for "is this server fit to run the test cameras":
the dashboard's Health page, ``POST /api/health/test`` and
``scripts/healthcheck.sh`` all use it. Each check returns PASS / WARN / FAIL /
SKIP with a short human explanation.

The automatic run (every ``health_auto_interval_s``, default 10 min) is the
*quick* variant: it does not open RTSP sessions, so it never shows up as a
viewer and never wakes a hold-until-viewer stream. A full run (button / script)
also reads 2 s from every RUNNING stream over loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select

from ..database import utcnow
from ..models import DesiredState, Stream, StreamStatus
from .ffprobe_service import ffmpeg_version
from .mediamtx_service import UP
from .network_info import interface_addresses

log = logging.getLogger(__name__)

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
RANK = {PASS: 0, SKIP: 0, WARN: 1, FAIL: 2}
# MediaMTX servers that must never be reachable from the network
INTERNAL_PORTS = {9997: "MediaMTX API", 9998: "MediaMTX metrics", 9999: "pprof", 8888: "HLS",
                  8889: "WebRTC", 8890: "SRT", 1935: "RTMP", 8892: "MoQ"}


@dataclass
class Check:
    id: str
    name: str
    status: str
    detail: str
    ms: int = 0
    data: dict = field(default_factory=dict)


def _overall(checks: list[Check]) -> str:
    worst = max((RANK[c.status] for c in checks), default=0)
    return {0: PASS, 1: WARN, 2: FAIL}[worst]


def _tcp_open(host: str, port: int, timeout: float = 0.3) -> bool:
    with contextlib.suppress(OSError), socket.create_connection((host, port), timeout=timeout):
        return True
    return False


def _cmd_first_line(argv: list[str], timeout: float = 5.0) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (p.stdout or p.stderr).decode(errors="replace").strip().splitlines()
    return out[0] if out else None


class HealthTester:
    def __init__(self, ctx):
        self.ctx = ctx
        self.s = ctx.settings
        self.last: dict | None = None
        self.last_quick: dict | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._versions: dict | None = None

    # ------------------------------------------------------------ checks
    async def _timed(self, fn, *a) -> Check | list[Check]:
        t0 = time.monotonic()
        try:
            res = await fn(*a)
        except Exception as exc:  # a broken check is a failed check, never a crash
            log.exception("health check %s crashed", getattr(fn, "__name__", fn))
            res = Check(fn.__name__.strip("_"), fn.__name__.strip("_"), FAIL, f"check crashed: {exc}")
        for c in res if isinstance(res, list) else [res]:
            c.ms = c.ms or int((time.monotonic() - t0) * 1000)
        return res

    async def _mediamtx(self) -> Check:
        mtx = self.ctx.mediamtx
        if mtx.state == UP:
            return Check("mediamtx", "MediaMTX running", PASS, f"Control API answers ({mtx.controller.mode} mode)")
        return Check("mediamtx", "MediaMTX running", FAIL,
                     f"MediaMTX is {mtx.state}: streams cannot be served. {mtx.last_control_message or ''}".strip())

    async def _recovery(self) -> Check:
        rep = self.ctx.recovery.report
        st = rep.get("state")
        if st == "done":
            return Check("recovery", "Start-up recovery", PASS,
                         f"{rep.get('streams_running', 0)}/{rep.get('streams_to_recover', 0)} streams restored")
        if st in ("pending", "running"):
            return Check("recovery", "Start-up recovery", WARN, "Still restoring streams after start-up")
        return Check("recovery", "Start-up recovery", FAIL, f"Recovery state: {st}")

    async def _exposure(self) -> Check:
        addrs = await asyncio.to_thread(interface_addresses)
        v4 = [a for a in addrs if ":" not in a]
        if not v4:
            return Check("exposure", "Internal ports not exposed", SKIP, "No network address to test from")

        # the port MediaMTX listens on here (behind Docker it differs from the published one)
        rtsp_port = urlsplit(self.s.publish_rtsp_base).port or 8554

        def scan():
            bad = []
            for a in v4:
                for port, what in INTERNAL_PORTS.items():
                    if _tcp_open(a, port):
                        bad.append(f"{what} ({a}:{port})")
            rtsp = any(_tcp_open(a, rtsp_port) for a in v4)
            return bad, rtsp
        bad, rtsp = await asyncio.to_thread(scan)
        if bad:
            return Check("exposure", "Internal ports not exposed", FAIL,
                         "Reachable from the network: " + ", ".join(bad) + ". Keep them on 127.0.0.1 in mediamtx.yml.")
        detail = f"Only RTSP {rtsp_port} is open on {', '.join(v4)}" if rtsp else \
            f"MediaMTX internals are loopback-only; RTSP {rtsp_port} not reachable on {', '.join(v4)}"
        return Check("exposure", "Internal ports not exposed", PASS if rtsp else WARN, detail)

    async def _versions_check(self) -> Check:
        if self._versions is None:
            ff = await asyncio.to_thread(ffmpeg_version, self.s.ffmpeg_bin)
            mtx = await asyncio.to_thread(_cmd_first_line, [self.s.mediamtx_bin, "--version"])
            self._versions = {"ffmpeg": ff, "mediamtx": mtx}
        v = self._versions
        if not v["ffmpeg"]:
            return Check("versions", "FFmpeg / MediaMTX", FAIL, f"FFmpeg not found ({self.s.ffmpeg_bin})", data=v)
        detail = f"{v['ffmpeg'].removeprefix('ffmpeg version ').split(' Copyright')[0]}; MediaMTX {v['mediamtx'] or 'version unknown'}"
        return Check("versions", "FFmpeg / MediaMTX", PASS, detail, data=v)

    async def _disk(self) -> Check:
        du = await asyncio.to_thread(shutil.disk_usage, self.s.video_dir)
        reserve = max(self.s.disk_reserve_bytes, int(du.total * self.s.disk_reserve_percent / 100))
        gib = du.free / 1024**3
        data = {"free": du.free, "total": du.total, "reserve": reserve}
        if du.free < reserve:
            return Check("disk", "Disk space", FAIL, f"{gib:.1f} GiB free, below the {reserve / 1024**3:.1f} GiB "
                         "reserve: uploads are refused", data=data)
        if du.free < 2 * reserve:
            return Check("disk", "Disk space", WARN, f"{gib:.1f} GiB free (reserve {reserve / 1024**3:.1f} GiB)", data=data)
        return Check("disk", "Disk space", PASS, f"{gib:.1f} GiB free of {du.total / 1024**3:.0f} GiB", data=data)

    async def _writable(self) -> Check:
        def probe():
            for label, d in (("videos", self.s.video_dir), ("data", self.s.data_dir)):
                fd, p = tempfile.mkstemp(prefix=".health-", dir=d)
                os.write(fd, b"ok")
                os.close(fd)
                os.unlink(p)
                yield label
        try:
            done = await asyncio.to_thread(lambda: list(probe()))
        except OSError as exc:
            return Check("writable", "Folders writable", FAIL, f"Cannot write: {exc.strerror or exc}")
        return Check("writable", "Folders writable", PASS, "Video and data folders accept writes (" + ", ".join(done) + ")")

    async def _database(self) -> Check:
        def q():
            con = sqlite3.connect(f"file:{self.s.database_path}?mode=ro", uri=True, timeout=5)
            try:
                return con.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                con.close()
        res = await asyncio.to_thread(q)
        return Check("database", "Database integrity", PASS if res == "ok" else FAIL,
                     "SQLite quick_check ok" if res == "ok" else f"SQLite quick_check: {res}")

    async def _backup(self) -> Check:
        newest = None
        for root in self.ctx.backups.locations():
            with contextlib.suppress(OSError):
                for p in root.glob("*/rtsp-manager.db"):
                    m = p.stat().st_mtime
                    newest = max(newest or 0, m)
        if newest is None:
            return Check("backup", "Recent backup", WARN, "No database backup found yet")
        age_h = (time.time() - newest) / 3600
        st = PASS if age_h <= 36 else WARN
        return Check("backup", "Recent backup", st, f"Newest database backup is {age_h:.1f} h old")

    async def _clock(self) -> Check:
        out = await asyncio.to_thread(_cmd_first_line, ["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        if out == "yes":
            return Check("clock", "Clock synchronised", PASS, "NTP synchronised (timedatectl)")
        if out == "no":
            return Check("clock", "Clock synchronised", WARN, "NTP is not synchronised: event times and the slate "
                         "clock may drift. Enable systemd-timesyncd or chrony.")
        return Check("clock", "Clock synchronised", SKIP, "Not checkable here (container or non-systemd host: "
                     "the host's clock is used)")

    async def _headroom(self) -> Check:
        smp = self.ctx.sysmon.sample or {}
        cpu = smp.get("cpu_percent", 0.0)
        mem = (smp.get("memory") or {}).get("percent", 0.0)
        with self.ctx.db.session() as s:
            desired = sum(1 for r in s.scalars(select(Stream)) if r.desired_state == DesiredState.RUNNING)
        cap = self.ctx.supervisor.max_active()
        data = {"cpu_percent": cpu, "mem_percent": mem, "streams": desired, "max_active_streams": cap}
        msg = f"CPU {cpu:.0f}%, memory {mem:.0f}%, {desired}/{cap} streams"
        if cpu >= self.s.cpu_warn_percent or mem >= self.s.mem_warn_percent:
            return Check("headroom", "CPU / memory headroom", WARN, msg + " (above the warning threshold)", data=data)
        if cap and desired >= cap:
            return Check("headroom", "CPU / memory headroom", WARN, msg + " (stream limit reached)", data=data)
        return Check("headroom", "CPU / memory headroom", PASS, msg, data=data)

    async def _streams(self) -> list[Check]:
        with self.ctx.db.session() as s:
            rows = [(r.name, r.status) for r in s.scalars(select(Stream).order_by(Stream.name))
                    if r.desired_state == DesiredState.RUNNING]
        if not rows:
            return [Check("streams", "Streams readable", SKIP, "No stream is meant to be running")]
        sem = asyncio.Semaphore(8)

        async def read(name: str) -> Check:
            async with sem:
                t0 = time.monotonic()
                proc = await asyncio.create_subprocess_exec(
                    self.s.ffprobe_bin, "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", "%+2",
                    "-select_streams", "v:0", "-show_entries", "packet=pts_time", "-of", "csv=p=0",
                    self.s.publish_url(name), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                try:
                    out, _ = await asyncio.wait_for(proc.communicate(), 8)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    await proc.wait()
                    out = b""
                n = len([ln for ln in out.splitlines() if ln.strip()])
                ms = int((time.monotonic() - t0) * 1000)
                if n > 10:
                    return Check(f"stream:{name}", f"Stream {name}", PASS, f"{n} video packets in 2 s", ms, {"packets": n})
                return Check(f"stream:{name}", f"Stream {name}", FAIL, "No media received over RTSP in 2 s", ms,
                             {"packets": n})

        out: list[Check] = []
        # Reading a hold-until-viewer stream would start its video: never touch those.
        skip = {StreamStatus.WAITING_FOR_VIEWER}
        todo = []
        for name, status in rows:
            if status in skip:
                out.append(Check(f"stream:{name}", f"Stream {name}", SKIP, "Waiting for its first viewer: not read"))
            elif status == StreamStatus.STARTING:
                # queued for its start slot (starts are spaced) or just launched; bounded by the
                # start-up timeout, after which it becomes RESTARTING (a failure)
                out.append(Check(f"stream:{name}", f"Stream {name}", SKIP, "Starting (camera starts are spaced): not read yet"))
            elif status not in (StreamStatus.RUNNING, StreamStatus.LEAD_IN):
                out.append(Check(f"stream:{name}", f"Stream {name}", FAIL, f"Should be running but is {status}"))
            else:
                todo.append(name)
        out.extend(await asyncio.gather(*(read(n) for n in todo)))
        return sorted(out, key=lambda c: c.name)

    # ------------------------------------------------------------ runs
    async def run(self, quick: bool = False, username: str | None = None) -> dict:
        async with self._lock:
            t0 = time.monotonic()
            fns = [self._mediamtx, self._recovery, self._exposure, self._versions_check, self._disk,
                   self._writable, self._database, self._backup, self._clock, self._headroom]
            if not quick:
                fns.append(self._streams)
            results = await asyncio.gather(*(self._timed(f) for f in fns))
            checks: list[Check] = []
            for r in results:
                checks.extend(r if isinstance(r, list) else [r])
            report = {
                "mode": "quick" if quick else "full",
                "overall": _overall(checks),
                "started_at": utcnow().isoformat(),
                "duration_s": round(time.monotonic() - t0, 2),
                "server": self.s.public_rtsp_host,
                "summary": {k: sum(1 for c in checks if c.status == k) for k in (PASS, WARN, FAIL, SKIP)},
                "checks": [asdict(c) for c in checks],
            }
            prev = (self.last_quick if quick else self.last) or {}
            if quick:
                self.last_quick = report
            else:
                self.last = report
            self._record(report, prev, username)
            return report

    def _record(self, report: dict, prev: dict, username: str | None) -> None:
        bad = [c for c in report["checks"] if c["status"] in (WARN, FAIL)]
        text = "; ".join(f"{c['name']}: {c['detail']}" for c in bad[:6]) or "all checks passed"
        level = {PASS: "INFO", WARN: "WARNING", FAIL: "ERROR"}[report["overall"]]
        if report["mode"] == "full":
            self.ctx.events.add("health.test", f"Health test {report['overall']}: {text}", level=level,
                                username=username, data=report["summary"])
        elif report["overall"] != prev.get("overall", PASS) or (
                report["overall"] == FAIL and {c["id"] for c in bad} != {
                    c["id"] for c in prev.get("checks", []) if c["status"] == FAIL}):
            # automatic run: only changes are news (no event every 10 minutes)
            kind = "health.recovered" if report["overall"] == PASS else "health.degraded"
            self.ctx.events.add(kind, f"Automatic health check {report['overall']}: {text}", level=level,
                                data=report["summary"])

    async def loop(self, interval_s: float) -> None:
        await asyncio.sleep(min(60.0, interval_s))  # let start-up recovery finish first
        while True:
            with contextlib.suppress(Exception):
                await self.run(quick=True)
            await asyncio.sleep(interval_s)

    def start(self, interval_s: float) -> None:
        if interval_s > 0:
            self._task = asyncio.create_task(self.loop(interval_s), name="health-auto")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class BackupService:
    """Daily consistent copy of the SQLite database (also in Docker, where no timer runs).

    Videos are not copied (too big); they are re-discoverable from the video folder.
    """

    def __init__(self, settings, events):
        self.s = settings
        self.events = events
        self._task: asyncio.Task | None = None

    @property
    def root(self) -> Path:
        return self.s.backup_path

    def locations(self) -> list[Path]:
        out = [self.root]
        sysd = Path("/var/backups/rtsp-manager")  # scripts/backup.sh via the systemd timer
        try:
            if sysd != self.root and sysd.is_dir() and os.access(sysd, os.R_OK | os.X_OK):
                out.append(sysd)
        except OSError:
            pass
        return out

    def backup_now(self) -> Path:
        dest = self.root / time.strftime("%Y%m%d-%H%M%S")
        dest.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(f"file:{self.s.database_path}?mode=ro", uri=True, timeout=30)
        dst = sqlite3.connect(dest / "rtsp-manager.db")
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        os.chmod(dest / "rtsp-manager.db", 0o600)
        self.prune()
        return dest

    def prune(self) -> int:
        dirs = sorted((p for p in self.root.glob("*") if (p / "rtsp-manager.db").is_file()), reverse=True)
        removed = 0
        for old in dirs[self.s.backup_keep:]:
            shutil.rmtree(old, ignore_errors=True)
            removed += 1
        return removed

    def newest_age_h(self) -> float | None:
        ages = [time.time() - p.stat().st_mtime for p in self.root.glob("*/rtsp-manager.db")]
        return min(ages) / 3600 if ages else None

    async def loop(self) -> None:
        while True:
            age = await asyncio.to_thread(self.newest_age_h)
            due = age is None or age >= self.s.backup_interval_h
            if due:
                try:
                    dest = await asyncio.to_thread(self.backup_now)
                    self.events.add("backup.done", f"database backed up to {dest.name}")
                except Exception as exc:
                    log.exception("database backup failed")
                    self.events.add("backup.failed", f"database backup failed: {exc}", level="ERROR")
            await asyncio.sleep(3600)

    def start(self) -> None:
        if self.s.backup_interval_h > 0:
            self._task = asyncio.create_task(self.loop(), name="backup")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
