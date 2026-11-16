"""Video library: discovery, identity, safe paths, uploads and disk space.

Security rules enforced here:
* The API never accepts filesystem paths; videos are referenced by DB id.
* Every resolved path must stay inside ``video_dir`` (no traversal, symlinks
  are ignored).
* Upload names are sanitised; uploads are written to a temp file on the same
  filesystem and atomically linked into place only when complete, so a
  partially written file is never visible to the library or a publisher.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..config import Settings

QUICK_HASH_CHUNK = 1024 * 1024


class LibraryError(ValueError):
    def __init__(self, message: str, status_code: int = 400, details: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details or {}


@dataclass(frozen=True)
class FileIdentity:
    size_bytes: int
    mtime_ns: int
    inode: int
    device: int

    @classmethod
    def of(cls, st: os.stat_result) -> "FileIdentity":
        return cls(st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)


def quick_hash(path: Path, size: int | None = None) -> str:
    """SHA-256 over size + first and last MiB: cheap content identity for huge files."""
    h = hashlib.sha256()
    size = path.stat().st_size if size is None else size
    h.update(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(QUICK_HASH_CHUNK))
        if size > 2 * QUICK_HASH_CHUNK:
            f.seek(size - QUICK_HASH_CHUNK)
            h.update(f.read(QUICK_HASH_CHUNK))
    return h.hexdigest()


def fingerprint(path: Path) -> dict:
    st = path.stat()
    ident = FileIdentity.of(st)
    return {
        "size_bytes": ident.size_bytes, "mtime_ns": ident.mtime_ns,
        "inode": ident.inode, "device": ident.device,
        "quick_hash": quick_hash(path, st.st_size),
    }


def public_error(exc: BaseException, path: Path, shown_as: str) -> str:
    """Error text safe for the UI: never reveal absolute server paths."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    msg = str(exc)
    for p in {str(path), str(path.resolve())}:
        msg = msg.replace(p, shown_as)
    return msg


def describe_change(expected: dict, path: Path) -> str | None:
    """Return a human description if the file no longer matches ``expected``."""
    try:
        st = path.stat()
    except FileNotFoundError:
        return "Source file was deleted or moved."
    except PermissionError:
        return "Source file is no longer readable (permission denied)."
    except OSError as exc:
        return f"Source file is inaccessible: {exc.strerror or exc}."
    if st.st_ino != expected.get("inode") or st.st_dev != expected.get("device"):
        return "Source file was replaced by a different file."
    if st.st_size != expected.get("size_bytes"):
        return "Source file size changed while streaming (modified or truncated)."
    if st.st_mtime_ns != expected.get("mtime_ns"):
        return "Source file was modified while streaming."
    if not os.access(path, os.R_OK):
        return "Source file is no longer readable (permission denied)."
    return None


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.video_dir.resolve()

    # ------------------------------------------------------------ paths
    def resolve(self, rel_path: str) -> Path:
        """Map a library-relative path to an absolute path inside the library."""
        if not rel_path or rel_path.startswith(("/", "\\")) or "\x00" in rel_path:
            raise LibraryError("Invalid library path.")
        candidate = (self.root / rel_path)
        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(self.root):
            raise LibraryError("Path escapes the video library.", 403)
        return resolved

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    def is_candidate(self, path: Path) -> bool:
        name = path.name
        if name.startswith(".") or name.endswith((".part", ".tmp", ".partial", ".crdownload")):
            return False
        return path.suffix.lower() in {e.lower() for e in self.settings.allowed_extensions}

    def iter_files(self):
        """Yield (rel_path, stat) for candidate video files (no symlinks)."""
        root_depth = len(self.root.parts)
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            d = Path(dirpath)
            depth = len(d.parts) - root_depth
            dirnames[:] = [n for n in dirnames if not n.startswith(".") and depth < self.settings.max_scan_depth]
            for fn in filenames:
                p = d / fn
                if not self.is_candidate(p):
                    continue
                try:
                    st = p.lstat()
                except OSError:
                    continue
                if not os.path.isfile(p) or os.path.islink(p):
                    continue
                yield p.relative_to(self.root).as_posix(), st

    # ------------------------------------------------------------ disk
    def disk_usage(self) -> dict:
        du = shutil.disk_usage(self.root)
        return {"path": str(self.root), "total": du.total, "used": du.used, "free": du.free}

    def reserve_bytes(self, total: int) -> int:
        return max(self.settings.disk_reserve_bytes, int(total * self.settings.disk_reserve_percent / 100))


_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._ ()+-]")


def sanitize_upload_name(raw: str, allowed_ext: list[str]) -> str:
    if not raw:
        raise LibraryError("A file name is required.")
    name = unicodedata.normalize("NFKC", raw)
    name = name.replace("\\", "/").split("/")[-1]  # drop any client-side directory part
    name = _SAFE_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"_+", "_", name)
    if not name or name in (".", ".."):
        raise LibraryError("Invalid file name.")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        raise LibraryError("The file name has no extension.")
    if "." + ext.lower() not in {e.lower() for e in allowed_ext}:
        raise LibraryError(
            f"Unsupported file type '.{ext}'. Allowed: {', '.join(sorted(allowed_ext))}.", 415
        )
    if len(name) > 180:
        name = stem[: 180 - len(ext) - 1] + "." + ext
    return name


class UploadReservations:
    """Tracks bytes promised to in-flight uploads so concurrent uploads
    cannot jointly exhaust the disk."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[str, int] = {}
        self._names: dict[str, str] = {}

    def total(self) -> int:
        with self._lock:
            return sum(self._items.values())

    def check(self, library: Library, size: int) -> dict:
        du = library.disk_usage()
        reserve = library.reserve_bytes(du["total"])
        in_flight = self.total()
        available = du["free"] - reserve - in_flight
        return {
            "required": size, "free": du["free"], "reserve": reserve,
            "in_flight_uploads": in_flight, "available": max(0, available),
            "ok": size <= available,
        }

    def reserve(self, library: Library, size: int, final_name: str) -> str:
        with self._lock:
            if final_name in self._names.values():
                raise LibraryError(f"'{final_name}' is already being uploaded by someone else.", 409)
            du = library.disk_usage()
            reserve = library.reserve_bytes(du["total"])
            in_flight = sum(self._items.values())
            available = du["free"] - reserve - in_flight
            if size > available:
                raise LibraryError(
                    "Not enough disk space for this upload.", 507,
                    {"required": size, "free": du["free"], "reserve": reserve,
                     "in_flight_uploads": in_flight, "available": max(0, available),
                     "reason": "Upload refused before copying: free space minus the safety reserve "
                               "and other in-progress uploads is smaller than the file."},
                )
            token = uuid.uuid4().hex
            self._items[token] = size
            self._names[token] = final_name
            return token

    def release(self, token: str) -> None:
        with self._lock:
            self._items.pop(token, None)
            self._names.pop(token, None)
