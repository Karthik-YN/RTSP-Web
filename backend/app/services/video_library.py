"""DB-backed video library: scanning, probing, thumbnails, uploads."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path

from sqlalchemy import select

from ..config import Settings
from ..database import Database, utcnow
from ..models import Video, VideoStatus
from . import compatibility, naming, naming_db
from .events import EventLog
from .ffprobe_service import ProbeError, available_decoders, grab_frame_jpeg, has_encoder, probe
from .file_manager import (FileIdentity, Library, LibraryError, UploadReservations, public_error, quick_hash,
                           sanitize_upload_name)

log = logging.getLogger(__name__)


class VideoLibrary:
    def __init__(self, settings: Settings, db: Database, events: EventLog):
        self.s = settings
        self.db = db
        self.events = events
        self.lib = Library(settings)
        self.uploads = UploadReservations()
        self._scan_lock = asyncio.Lock()
        self._probe_sem = asyncio.Semaphore(settings.probe_concurrency)
        self._frame_sem = asyncio.Semaphore(2)
        self._pending_sizes: dict[str, tuple[int, int]] = {}  # rel -> (size, mtime) seen last scan
        self._task: asyncio.Task | None = None
        self.last_scan_at = None
        self.last_scan_summary: dict = {}
        self._sessions: dict[str, dict] = {}
        from .converter import Converter
        from .preview import PreviewCache
        self.converter = Converter(self)
        # codecs sent to edges as-is on top of RTSPM_COPY_CODECS (admin switch; set in main.py)
        self.extra_copy_codecs = lambda: ()
        self.previews = PreviewCache(self)

    # ---------------------------------------------------------- compat
    def compat_for(self, meta: dict, force_transcode: bool = False) -> compatibility.CompatResult:
        return compatibility.evaluate(
            meta, self.s, available_decoders(self.s.ffmpeg_bin),
            has_encoder(self.s.ffmpeg_bin, "libx264"), force_transcode, tuple(self.extra_copy_codecs()),
        )

    def recheck_compat(self) -> int:
        """Re-evaluate every ready video's mode (copy / transcode) after a policy change, such as
        the H.265 pass-through switch. Returns how many changed."""
        changed = 0
        with self.db.session() as s:
            for v in s.scalars(select(Video).where(Video.status == VideoStatus.READY)):
                c = self.compat_for(self.meta_of(v))
                if (v.compat_mode, v.compat_reasons, v.compat_warnings) != (c.mode, c.reasons, c.warnings):
                    v.compat_mode, v.compat_reasons, v.compat_warnings = c.mode, c.reasons, c.warnings
                    changed += 1
        return changed

    @staticmethod
    def meta_of(v: Video) -> dict:
        return {
            "video_codec": v.video_codec, "video_profile": v.video_profile, "pix_fmt": v.pix_fmt,
            "bit_depth": v.bit_depth, "width": v.width, "height": v.height, "fps": v.fps,
            "is_vfr": bool(v.is_vfr), "audio_codec": v.audio_codec, "field_order": v.field_order,
            "keyframe_interval_s": v.keyframe_interval_s,
            "has_b_frames": compatibility.b_frames(v.probe_json),
        }

    # ---------------------------------------------------------- scanning
    async def scan(self, force: bool = False) -> dict:
        async with self._scan_lock:
            return await self._scan(force)

    async def _scan(self, force: bool) -> dict:
        t0 = time.monotonic()
        on_disk: dict[str, os.stat_result] = {}
        settling: list[str] = []
        now = time.time()
        for rel, st in await asyncio.to_thread(lambda: list(self.lib.iter_files())):
            # A file still being copied (recent mtime or size still changing) is
            # not offered yet, so nobody can publish a partially written file.
            prev = self._pending_sizes.get(rel)
            recent = now - st.st_mtime < self.s.settle_seconds
            if recent and (prev is None or prev != (st.st_size, st.st_mtime_ns)):
                self._pending_sizes[rel] = (st.st_size, st.st_mtime_ns)
                settling.append(rel)
                continue
            self._pending_sizes.pop(rel, None)
            on_disk[rel] = st

        added = updated = missing = renamed = 0
        with self.db.session() as s:
            rows = {v.rel_path: v for v in s.scalars(select(Video))}
        by_inode = {(st.st_dev, st.st_ino): rel for rel, st in on_disk.items()}
        settling_now = set(settling)  # still being written: neither missing nor re-probed yet

        # rows whose file disappeared: renamed (same inode) or missing
        claimed: set[str] = set()
        for rel, v in list(rows.items()):
            if rel in on_disk or rel in settling_now:
                continue
            new_rel = by_inode.get((v.device, v.inode))
            if (new_rel and new_rel not in rows and new_rel not in claimed
                    and on_disk[new_rel].st_size == v.size_bytes):
                with self.db.session() as s:
                    row = s.get(Video, v.id)
                    row.rel_path = new_rel
                    row.updated_at = utcnow()
                claimed.add(new_rel)
                renamed += 1
                self.events.add("video.renamed", f"{rel} -> {new_rel}", data={"video_id": v.id})
            elif v.status != VideoStatus.MISSING:
                with self.db.session() as s:
                    row = s.get(Video, v.id)
                    row.status = VideoStatus.MISSING
                    row.error = "File is no longer in the library folder."
                    row.updated_at = utcnow()
                missing += 1
                self.events.add("video.missing", f"{rel} disappeared", level="WARNING", data={"video_id": v.id})

        rows = {}
        with self.db.session() as s:
            rows = {v.rel_path: v for v in s.scalars(select(Video))}
        for rel, st in on_disk.items():
            ident = FileIdentity.of(st)
            v = rows.get(rel)
            if v is not None and not force and v.status != VideoStatus.MISSING and (
                v.size_bytes, v.mtime_ns, v.inode, v.device) == (
                ident.size_bytes, ident.mtime_ns, ident.inode, ident.device):
                with self.db.session() as s:
                    row = s.get(Video, v.id)
                    row.last_seen_at = utcnow()
                continue
            await self._ingest(rel, st, v)
            if v is None:
                added += 1
            else:
                updated += 1

        self.last_scan_at = utcnow()
        self.last_scan_summary = {
            "added": added, "updated": updated, "missing": missing, "renamed": renamed,
            "settling": settling, "files": len(on_disk), "seconds": round(time.monotonic() - t0, 2),
        }
        return self.last_scan_summary

    async def _ingest(self, rel: str, st: os.stat_result, existing: Video | None) -> Video:
        path = self.lib.resolve(rel)
        qh, meta, err = "", None, None
        async with self._probe_sem:
            try:
                qh = await asyncio.to_thread(quick_hash, path, st.st_size)
                meta = await asyncio.to_thread(probe, self.s.ffprobe_bin, path)
            except (ProbeError, OSError) as exc:
                meta, err = None, public_error(exc, path, rel)
        now = utcnow()
        with self.db.session() as s:
            v = s.get(Video, existing.id) if existing else None
            changed = existing is not None and existing.quick_hash != qh
            if v is None:
                v = Video(rel_path=rel, created_at=now)
                s.add(v)
            v.size_bytes, v.mtime_ns, v.inode, v.device = st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev
            v.quick_hash = qh
            v.updated_at = v.last_seen_at = now
            if meta is None or not meta.video_codec:
                v.status = VideoStatus.INVALID
                v.error = err or "No video stream found."
                v.compat_mode = compatibility.REJECT
                v.compat_reasons = [v.error]
                v.compat_warnings = []
            else:
                v.status, v.error = VideoStatus.READY, None
                for k in ("container", "duration_s", "width", "height", "fps", "fps_text", "video_codec",
                          "video_profile", "pix_fmt", "bit_depth", "field_order", "bitrate_bps",
                          "audio_codec", "keyframe_interval_s"):
                    setattr(v, k, getattr(meta, k))
                v.is_vfr = bool(meta.is_vfr)
                v.probe_json = {"start_time_s": meta.start_time_s, **{k: meta.raw.get(k) for k in ("format", "streams")}}
                c = self.compat_for(self.meta_of(v))
                v.compat_mode, v.compat_reasons, v.compat_warnings = c.mode, c.reasons, c.warnings
            s.flush()
            vid = v.id
        if meta is not None and meta.video_codec:
            await self._make_thumbnail(vid, path, meta.duration_s or 0, qh)
        self.events.add("video.changed" if existing and changed else ("video.updated" if existing else "video.added"),
                        f"{rel} ({'ok' if err is None else err})", data={"video_id": vid})
        with self.db.session() as s:
            return s.get(Video, vid)

    async def _make_thumbnail(self, vid: int, path: Path, duration: float, qh: str) -> None:
        at = min(max(duration * 0.1, 0.0), 10.0)
        name = f"{vid}_{qh[:12]}.jpg"
        out = self.s.thumbnail_path / name
        try:
            if not out.exists():
                data = await asyncio.to_thread(grab_frame_jpeg, self.s.ffmpeg_bin, str(path), at, 320, 30.0)
                tmp = out.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, out)
            with self.db.session() as s:
                v = s.get(Video, vid)
                old = v.thumbnail
                v.thumbnail = name
            if old and old != name:
                with contextlib.suppress(OSError):
                    (self.s.thumbnail_path / old).unlink()
        except (ProbeError, OSError) as exc:
            log.warning("thumbnail failed for %s: %s", path, exc)

    async def frame(self, v: Video, at_s: float) -> bytes:
        async with self._frame_sem:
            start = (v.probe_json or {}).get("start_time_s") or 0.0
            return await asyncio.to_thread(grab_frame_jpeg, self.s.ffmpeg_bin,
                                           str(self.lib.resolve(v.rel_path)), start + at_s, 320, 20.0)

    # ---------------------------------------------------------- background
    async def run(self) -> None:
        while True:
            try:
                await self.scan()
                self.expire_sessions()
                if self.purge_trash():
                    log.info("purged old files from the video trash")
            except Exception:
                log.exception("library scan failed")
            await asyncio.sleep(self.s.scan_interval_s)

    def start_scanner(self) -> None:
        self._task = asyncio.create_task(self.run(), name="library-scanner")

    async def stop(self) -> None:
        await self.converter.stop()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    # ---------------------------------------------------------- naming convention
    # Set by main.py to the live setting (enforce | warn | off); default for tools/tests built without main.
    naming_mode = staticmethod(lambda: "off")

    def naming_gate(self, name: str) -> None:
        """Refuse a non-conforming upload name in "enforce" mode, with the reasons and a suggestion."""
        if self.naming_mode() != "enforce":
            return
        known = naming_db.code_set(self.db)
        result = naming.check(name, known)
        if result.ok:
            return
        try:
            hint = naming.suggest(name, known, naming_db.names_in_use(self.db))
        except ValueError:
            hint = None
        raise LibraryError(
            f"'{name}' does not follow the naming convention WHAT_WHERE_NN (for example ALPR_toll-s1_01.mp4): "
            + " ".join(result.errors) + (f" Suggested: {hint}" if hint else ""),
            422, {"naming": result.view(), "suggestion": hint})

    # ---------------------------------------------------------- uploads
    def upload_check(self, filename: str, size: int, quick_hash_hex: str | None = None) -> dict:
        name = sanitize_upload_name(filename, self.s.allowed_extensions)
        if quick_hash_hex:  # same content already here? (checked before the name, so a duplicate needs no renaming)
            dup = self.same_content(size, quick_hash_hex.lower())
            if dup is not None:
                return {"filename": name, "exists": False, **self.uploads.check(self.lib, size),
                        "duplicate_of": {"id": dup.id, "rel_path": dup.rel_path}}
        self.naming_gate(name)
        if size <= 0:
            raise LibraryError("Upload size must be greater than zero.")
        if size > self.s.upload_max_bytes:
            raise LibraryError(f"File is larger than the upload limit ({self.s.upload_max_bytes} bytes).", 413)
        exists = self.lib.resolve(name).exists()
        info = self.uploads.check(self.lib, size)
        return {"filename": name, "exists": exists, **info}

    async def receive_upload(self, filename: str, size: int, chunks, username: str | None,
                             origin: dict | None = None) -> Video:
        """Stream an upload to a temp file, then atomically link it into the library.

        ``chunks`` is an async iterator of bytes. Disk space is reserved
        before the first byte is written.
        """
        name = sanitize_upload_name(filename, self.s.allowed_extensions)
        self.naming_gate(name)
        if size <= 0:
            raise LibraryError("A Content-Length header with the file size is required.", 411)
        if size > self.s.upload_max_bytes:
            raise LibraryError(f"File is larger than the upload limit ({self.s.upload_max_bytes} bytes).", 413)
        final = self.lib.resolve(name)
        if final.exists():
            raise LibraryError(f"A file named '{name}' already exists in the library. Rename the file "
                               "or remove the existing one on the server first.", 409)
        token = self.uploads.reserve(self.lib, size, name)
        tmp = self.s.upload_tmp_path / f"{uuid.uuid4().hex}.part"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
            with os.fdopen(fd, "wb") as f:
                last_check = 0
                async for chunk in chunks:
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > size:
                        raise LibraryError("More data received than the declared file size.", 400)
                    await asyncio.to_thread(f.write, chunk)
                    if written - last_check > 256 * 1024 * 1024:  # re-check disk every 256 MiB
                        last_check = written
                        du = self.lib.disk_usage()
                        if du["free"] < self.lib.reserve_bytes(du["total"]) // 2:
                            raise LibraryError("Disk space fell below the safety reserve during upload; "
                                               "upload aborted.", 507, {"free": du["free"]})
                f.flush()
                os.fsync(f.fileno())
            if written != size:
                raise LibraryError(f"Upload incomplete: received {written} of {size} bytes.", 400)
            await self._verify_and_link(tmp, final, name)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
            self.uploads.release(token)
        return await self._register_upload(final, name, size, username, origin)

    def same_content(self, size: int, qh: str) -> Video | None:
        """A library video with exactly this content fingerprint (size + quick_hash), whatever its name."""
        with self.db.session() as s:
            return s.scalar(select(Video).where(Video.size_bytes == size, Video.quick_hash == qh,
                                                Video.status != VideoStatus.MISSING).limit(1))

    async def _verify_and_link(self, tmp: Path, final: Path, name: str) -> None:
        # Duplicates are refused by CONTENT, not name: a renamed copy of a library video is not stored twice.
        size = tmp.stat().st_size
        dup = self.same_content(size, await asyncio.to_thread(quick_hash, tmp, size))
        if dup is not None:
            raise LibraryError(f"Duplicate video: it is already in the library as '{dup.rel_path}' (same content). "
                               "Not stored again; use the existing one.", 409,
                               {"duplicate_of": {"id": dup.id, "rel_path": dup.rel_path}})
        try:
            meta = await asyncio.to_thread(probe, self.s.ffprobe_bin, tmp)
        except ProbeError as exc:
            raise LibraryError(f"The uploaded file is not a readable video: {public_error(exc, tmp, name)}", 422)
        if not meta.video_codec:
            raise LibraryError("The uploaded file contains no video stream.", 422)
        try:
            os.link(tmp, final)  # atomic, never overwrites an existing file
        except FileExistsError:
            raise LibraryError(f"A file named '{name}' appeared while uploading; not overwritten.", 409)

    async def _register_upload(self, final: Path, name: str, size: int, username: str | None,
                               origin: dict | None = None) -> Video:
        video = await self._ingest(name, final.stat(), None)
        origin = origin or {}
        with self.db.session() as s:
            row = s.get(Video, video.id)
            row.uploaded_by = username
            row.uploaded_at = utcnow()
            row.uploaded_from = (origin.get("ip") or None)
            row.uploaded_agent = (origin.get("agent") or "")[:300] or None
            original = (origin.get("original_name") or "").strip()[:255]
            row.original_name = original if original and original != name else None
        video = row  # committed when the session closes; attributes stay loaded
        self.events.add("video.uploaded", f"{name} ({size} bytes)", username=username, data={"video_id": video.id})
        return video

    # ---------------------------------------------------------- resumable uploads
    # A session reserves disk space up front; chunks are appended at a known
    # offset, so a client whose Wi-Fi drops asks for the offset and continues.
    # Sessions live in memory (a backend restart discards them and their temp
    # files) and expire after ``upload_session_idle_s`` without data.

    def start_session(self, filename: str, size: int, username: str | None, origin: dict | None = None) -> dict:
        name = sanitize_upload_name(filename, self.s.allowed_extensions)
        self.naming_gate(name)
        if size <= 0:
            raise LibraryError("Upload size must be greater than zero.")
        if size > self.s.upload_max_bytes:
            raise LibraryError(f"File is larger than the upload limit ({self.s.upload_max_bytes} bytes).", 413)
        if self.lib.resolve(name).exists():
            raise LibraryError(f"A file named '{name}' already exists in the library.", 409)
        token = self.uploads.reserve(self.lib, size, name)
        sid = uuid.uuid4().hex
        tmp = self.s.upload_tmp_path / f"{sid}.part"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        os.close(fd)
        self._sessions[sid] = {"id": sid, "name": name, "size": size, "offset": 0, "token": token, "tmp": tmp,
                               "user": username, "origin": origin, "touched": time.monotonic(),
                               "lock": asyncio.Lock()}
        return self.session_info(sid)

    def session_info(self, sid: str) -> dict:
        s = self._session(sid)
        return {"id": s["id"], "filename": s["name"], "size": s["size"], "offset": s["offset"]}

    def _session(self, sid: str) -> dict:
        s = self._sessions.get(sid)
        if s is None:
            raise LibraryError("Upload session not found (expired or the server restarted); start again.", 404)
        return s

    async def append_chunk(self, sid: str, offset: int, chunks) -> dict | Video:
        """Append one chunk; returns the session info, or the Video when complete."""
        s = self._session(sid)
        async with s["lock"]:
            if offset != s["offset"]:
                raise LibraryError(f"Wrong offset {offset}; the server has {s['offset']} bytes.", 409,
                                   {"offset": s["offset"]})
            written = s["offset"]
            with open(s["tmp"], "r+b") as f:
                f.seek(written)
                try:
                    async for chunk in chunks:
                        if not chunk:
                            continue
                        if written + len(chunk) > s["size"]:
                            raise LibraryError("More data received than the declared file size.", 400)
                        await asyncio.to_thread(f.write, chunk)
                        written += len(chunk)
                finally:
                    f.flush()
                    os.fsync(f.fileno())
                    s["offset"] = written  # keep what arrived, even if the connection dropped
                    s["touched"] = time.monotonic()
            du = self.lib.disk_usage()
            if du["free"] < self.lib.reserve_bytes(du["total"]) // 2:
                self.abort_session(sid)
                raise LibraryError("Disk space fell below the safety reserve during upload; upload aborted.", 507,
                                   {"free": du["free"]})
            if written < s["size"]:
                return self.session_info(sid)
            final = self.lib.resolve(s["name"])
            try:
                await self._verify_and_link(s["tmp"], final, s["name"])
            finally:
                self.abort_session(sid)
            return await self._register_upload(final, s["name"], s["size"], s["user"], s.get("origin"))

    def abort_session(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if s is None:
            return
        with contextlib.suppress(FileNotFoundError):
            s["tmp"].unlink()
        self.uploads.release(s["token"])

    def expire_sessions(self) -> int:
        now = time.monotonic()
        stale = [sid for sid, s in self._sessions.items()
                 if now - s["touched"] > self.s.upload_session_idle_s and not s["lock"].locked()]
        for sid in stale:
            self.abort_session(sid)
        return len(stale)

    # ---------------------------------------------------------- delete (admin)
    async def delete_video(self, video_id: int, username: str | None) -> str:
        """Move a video to the library's .trash folder (purged after trash_days).

        Refused while any stream uses it, so a running camera never loses its source.
        """
        from ..models import Stream  # local import: avoid a cycle at module load

        with self.db.session() as s:
            v = s.get(Video, video_id)
            if v is None:
                raise LibraryError("Video not found.", 404)
            users = [r.name for r in s.scalars(select(Stream).where(Stream.video_id == video_id))]
        if users:
            raise LibraryError(f"'{v.rel_path}' is used by stream(s): {', '.join(users)}. Delete those streams first.",
                               409, {"streams": users})
        src = self.lib.resolve(v.rel_path)
        trash = self.s.video_dir / ".trash"
        trash.mkdir(exist_ok=True)
        dst = trash / f"{time.strftime('%Y%m%d-%H%M%S')}_{Path(v.rel_path).name}"
        if src.exists():
            await asyncio.to_thread(os.replace, src, dst)  # same filesystem: instant, reversible
        with self.db.session() as s:
            row = s.get(Video, video_id)
            thumb = row.thumbnail
            s.delete(row)
        if thumb:
            with contextlib.suppress(OSError):
                (self.s.thumbnail_path / thumb).unlink()
        self.previews.forget(video_id)
        self.events.add("video.deleted", f"{v.rel_path} moved to .trash/{dst.name} (kept {self.s.trash_days:g} days)",
                        username=username, level="WARNING")
        return dst.name

    def purge_trash(self) -> int:
        trash = self.s.video_dir / ".trash"
        if not trash.is_dir():
            return 0
        cutoff = time.time() - self.s.trash_days * 86400
        n = 0
        for p in trash.iterdir():
            with contextlib.suppress(OSError):
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    n += 1
        return n

    def cleanup_stale_uploads(self) -> int:
        n = 0
        for p in self.s.upload_tmp_path.glob("*.part"):
            with contextlib.suppress(OSError):
                p.unlink()
                n += 1
        return n
