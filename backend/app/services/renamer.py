"""Rename videos to the naming convention without interrupting the streams that play them.

A plain ``mv`` breaks running streams: each stream keeps the old path (``streams.source_rel_path``),
FFmpeg reopens the file at every loop, and the source watchdog checks the old path. So a rename here:

1. holds the library scan lock (the scanner must not see a half-done rename) and the lock of every
   stream of the video (no publisher starts with a stale path meanwhile);
2. hard-links the new name (both names exist; same inode, size and mtime, so fingerprints stay valid);
3. re-points each running publisher's source symlink and in-memory plan to the new name;
4. updates ``videos.rel_path`` (keeping the old name in ``previous_names``) and every stream's
   ``source_rel_path`` in one transaction;
5. removes the old name.

Stream names, and so every RTSP URL, never change. If a step fails, the earlier ones are undone.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from ..database import utcnow
from ..models import Stream, Video
from . import naming, naming_db
from .file_manager import LibraryError


def _target(old_rel: str, name: str) -> tuple[str, str]:
    """(new file name, new rel_path): same folder and extension as the old file."""
    name = name.strip()
    old = PurePosixPath(old_rel)
    if "/" in name or "\\" in name:
        raise LibraryError("A new name is a file name only, without folders.")
    stem, ext = naming.split(name)
    if not ext:
        name, ext = name + old.suffix.lower(), old.suffix.lower()
    if ext != old.suffix.lower():
        raise LibraryError(f"The extension must stay {old.suffix.lower()} (renaming does not convert the file).")
    rel = name if str(old.parent) in ("", ".") else str(old.parent / name)
    return name, rel


class Renamer:
    def __init__(self, ctx):
        self.ctx = ctx
        self.lib = ctx.library
        self.sup = ctx.supervisor
        self.db = ctx.db

    def _validate(self, v: Video, name: str, taken: set[str], codes: set[str]) -> tuple[str, str]:
        new_name, new_rel = _target(v.rel_path, name)
        result = naming.check(new_name, codes)
        if not result.ok:
            raise LibraryError(" ".join(result.errors), 422, {"naming": result.view()})
        if new_rel.lower() != v.rel_path.lower() and new_rel.lower() in taken:
            raise LibraryError(f"'{new_name}' is already used by another video.", 409)
        if new_rel != v.rel_path and new_rel.lower() != v.rel_path.lower() and self.lib.lib.resolve(new_rel).exists():
            raise LibraryError(f"A file named '{new_name}' already exists in the video folder.", 409)
        if self.lib.converter.active_for(v.id):
            raise LibraryError("This video is being converted; rename it when the conversion has finished.", 409)
        return new_name, new_rel

    def _streams(self, s, video_id: int) -> list[tuple[int, str]]:
        return [(r.id, r.name) for r in s.scalars(select(Stream).where(Stream.video_id == video_id).order_by(Stream.id))]

    def _blocked(self, streams: list[tuple[int, str]]) -> list[str]:
        return [f"{name}: {why}" for sid, name in streams if (why := self.sup.uses_old_path(sid))]

    async def rename(self, video_id: int, name: str, username: str | None, dry_run: bool = False) -> dict:
        async with self.lib._scan_lock:
            with self.db.session() as s:
                v = s.get(Video, video_id)
                if v is None:
                    raise LibraryError("Video not found.", 404)
                s.expunge(v)
                taken = {r.lower() for r in s.scalars(select(Video.rel_path).where(Video.id != video_id))}
                streams = self._streams(s, video_id)
            new_name, new_rel = self._validate(v, name, taken, naming_db.code_set(self.db))
            out = {"video_id": video_id, "old": v.rel_path, "new": new_rel, "streams": [n for _, n in streams],
                   "changed": new_rel != v.rel_path}
            blocked = self._blocked(streams)
            if blocked:
                raise LibraryError("A running stream cannot follow the rename: " + "; ".join(blocked), 409,
                                   {"streams": blocked})
            if dry_run or not out["changed"]:
                return out
            await self._apply(v, new_rel, [sid for sid, _ in streams], username)
        return out

    async def _apply(self, v: Video, new_rel: str, stream_ids: list[int], username: str | None) -> None:
        src: Path = self.lib.lib.resolve(v.rel_path)
        dst: Path = self.lib.lib.resolve(new_rel)
        case_only = src.parent == dst.parent and src.name.lower() == dst.name.lower()
        async with contextlib.AsyncExitStack() as stack:
            for sid in stream_ids:  # sorted ids: no lock-order deadlock between two renames
                await stack.enter_async_context(self.sup.runtime(sid).lock)
            linked = False
            if not case_only:
                try:
                    await asyncio.to_thread(os.link, src, dst)
                    linked = True
                except FileExistsError:
                    raise LibraryError(f"A file named '{dst.name}' already exists in the video folder.", 409)
                except OSError:
                    linked = False  # filesystem without hard links (some NAS shares): rename in place below
            if not linked:
                await asyncio.to_thread(os.rename, src, dst)
            followed: list[int] = []
            try:
                for sid in stream_ids:
                    why = self.sup.follow_rename(sid, dst)
                    if why:
                        raise LibraryError(f"Stream {sid} cannot follow the rename: {why}", 409)
                    followed.append(sid)
                with self.db.session() as s:
                    row = s.get(Video, v.id)
                    row.previous_names = [*(row.previous_names or []), v.rel_path][-20:]
                    row.rel_path = new_rel
                    row.updated_at = utcnow()
                    for st in s.scalars(select(Stream).where(Stream.id.in_(stream_ids))):
                        st.source_rel_path = new_rel
            except Exception:
                # undo: publishers back to the old name, file back to the old name
                for sid in followed:
                    with contextlib.suppress(Exception):
                        self.sup.follow_rename(sid, src)
                if linked:
                    with contextlib.suppress(OSError):
                        os.unlink(dst)
                else:
                    with contextlib.suppress(OSError):
                        os.rename(dst, src)
                raise
            if linked:
                await asyncio.to_thread(os.unlink, src)
        self.ctx.events.add("video.renamed", f"{v.rel_path} -> {new_rel}", username=username,
                            data={"video_id": v.id, "old": v.rel_path, "new": new_rel, "streams": stream_ids})

    async def batch(self, items: list[dict], username: str | None, dry_run: bool = True) -> dict:
        """Rename many videos. ``dry_run`` checks everything (including clashes inside the batch) and changes nothing."""
        with self.db.session() as s:
            names = {vid: rel for vid, rel in s.execute(select(Video.id, Video.rel_path))}
        live = {rel.lower() for rel in names.values()}
        results = []
        for it in items:
            vid, name = int(it["video_id"]), str(it["name"])
            try:
                if vid not in names:
                    raise LibraryError("Video not found.", 404)
                _, new_rel = _target(names[vid], name)
                old_l, new_l = names[vid].lower(), new_rel.lower()
                if new_l != old_l and new_l in live:
                    raise LibraryError(f"'{new_rel}' clashes with another video (or an earlier row of this batch).", 409)
                r = await self.rename(vid, name, username, dry_run=True)
                if not dry_run:
                    r = await self.rename(vid, name, username)
                live.discard(old_l)
                live.add(new_l)
                names[vid] = new_rel
                results.append({**r, "ok": True})
            except LibraryError as exc:
                results.append({"video_id": vid, "old": names.get(vid), "new": name, "ok": False,
                                "error": str(exc), "status": exc.status_code})
        ok = sum(r["ok"] for r in results)
        if not dry_run:
            self.ctx.events.add("video.rename_batch", f"renamed {ok} of {len(results)} video(s)", username=username)
        return {"dry_run": dry_run, "ok": ok, "failed": len(results) - ok, "results": results}

