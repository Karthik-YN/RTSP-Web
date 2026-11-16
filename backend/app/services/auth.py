"""Internal access control.

* Users live in a small file (``<data_dir>/users`` by default), one per line:
  ``username:role:pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``.
  Roles: ``admin`` (everything incl. users and other people's protected
  streams), ``operator`` (streams/videos) and ``viewer`` (read-only).
  Manage it with ``scripts/manage_users.py``; passwords are never stored or
  logged in clear text.
* The browser logs in once and gets an HMAC-signed, HttpOnly, SameSite=Strict
  session cookie. Scripts may use HTTP Basic auth instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROLES = ("admin", "operator", "viewer")
ITERATIONS = 200_000


@dataclass(frozen=True)
class User:
    username: str
    role: str

    @property
    def can_write(self) -> bool:
        return self.role in ("admin", "operator")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


def hash_password(password: str, iterations: int = ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


class UserStore:
    """Reads the users file, reloading it when it changes on disk."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime = None
        self._users: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime_ns
        except FileNotFoundError:
            self._users, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        users = {}
        for line in self.path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":", 2)
            if len(parts) != 3 or parts[1] not in ROLES:
                continue
            users[parts[0]] = (parts[1], parts[2])
        self._users, self._mtime = users, mtime

    def authenticate(self, username: str, password: str) -> User | None:
        with self._lock:
            self._load()
            entry = self._users.get(username)
        if entry is None:
            verify_password(password, hash_password("x", 1000))  # equalise timing a little
            return None
        role, encoded = entry
        return User(username, role) if verify_password(password, encoded) else None

    def get(self, username: str) -> User | None:
        with self._lock:
            self._load()
            entry = self._users.get(username)
        return User(username, entry[0]) if entry else None

    def count(self) -> int:
        with self._lock:
            self._load()
            return len(self._users)

    def list_users(self) -> list[User]:
        with self._lock:
            self._load()
            return [User(n, r) for n, (r, _) in sorted(self._users.items())]

    def admin_names(self) -> set[str]:
        with self._lock:
            self._load()
            return {n for n, (r, _) in self._users.items() if r == "admin"}

    def set_user(self, username: str, role: str, password: str | None) -> None:
        """Create or update a user. ``password=None`` keeps the current password."""
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if not re.fullmatch(r"[A-Za-z0-9._@-]{1,64}", username or ""):
            raise ValueError("user name may contain letters, digits and . _ @ - (max 64)")
        with self._lock:
            self._load()
            current = self._users.get(username)
        if password is None:
            if current is None:
                raise ValueError("a password is required for a new user")
            encoded = current[1]
        else:
            if len(password) < 8:
                raise ValueError("password must be at least 8 characters")
            encoded = hash_password(password)
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        lines = [ln for ln in lines if not ln.startswith(f"{username}:")]
        lines.append(f"{username}:{role}:{encoded}")
        self._write(lines)

    def delete_user(self, username: str) -> bool:
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        kept = [ln for ln in lines if not ln.startswith(f"{username}:")]
        if len(kept) == len(lines):
            return False
        self._write(kept)
        return True

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + ("\n" if lines else ""))
        os.replace(tmp, self.path)
        with self._lock:
            self._mtime = None  # force reload


ADMIN_UPLOADER = "admin"


def uploader_label(uploaded_by: str | None, admins: set[str] | None = None) -> str:
    """Who added a video, as shown in the library.

    "admin" for anything added by an administrator: uploads by an admin account, uploads made while
    login was off ("anonymous") and files copied straight onto the server (no uploader). Everyone else
    is shown by user name. The real account stays in ``uploaded_by`` for the record.
    """
    if not uploaded_by or uploaded_by == "anonymous" or uploaded_by in (admins or set()):
        return ADMIN_UPLOADER
    return uploaded_by


class LoginThrottle:
    """Slows down password guessing: after ``free`` failures within ``window``
    seconds for the same user name or client address, further attempts are
    refused for an exponentially growing lockout (capped at ``max_lock``)."""

    def __init__(self, free: int = 5, window: float = 600, base_lock: float = 30, max_lock: float = 900):
        self.free, self.window, self.base_lock, self.max_lock = free, window, base_lock, max_lock
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, *keys: str) -> float:
        now = time.time()
        with self._lock:
            return max([self._locked_until.get(k, 0) - now for k in keys] + [0])

    def failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                fails = [t for t in self._fails.get(k, []) if now - t < self.window] + [now]
                self._fails[k] = fails
                extra = len(fails) - self.free
                if extra >= 0:
                    self._locked_until[k] = now + min(self.max_lock, self.base_lock * 2 ** extra)

    def success(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._fails.pop(k, None)
                self._locked_until.pop(k, None)


def load_secret(explicit: str | None, path: Path) -> bytes:
    if explicit:
        return explicit.encode()
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_hex(32).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(secret: bytes, username: str, hours: float) -> str:
    payload = _b64(json.dumps({"u": username, "exp": int(time.time() + hours * 3600)}).encode())
    sig = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def read_token(secret: bytes, token: str) -> str | None:
    try:
        payload, sig = token.split(".", 1)
        expected = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            return None
        data = json.loads(_unb64(payload))
        if data.get("exp", 0) < time.time():
            return None
        return data.get("u")
    except (ValueError, KeyError, json.JSONDecodeError):
        return None


class LoginGuard:
    """Per-user lockout (5 free failures) plus a looser per-address limit (30).

    Per-address alone would lock out every user behind a shared address
    (nginx on localhost, office NAT), so it only stops broad guessing.
    """

    def __init__(self):
        self.users = LoginThrottle(free=5)
        self.addresses = LoginThrottle(free=30)

    def retry_after(self, ip: str, username: str) -> float:
        return max(self.users.retry_after(username.lower()), self.addresses.retry_after(ip))

    def failure(self, ip: str, username: str) -> None:
        self.users.failure(username.lower())
        self.addresses.failure(ip)

    def success(self, ip: str, username: str) -> None:
        self.users.success(username.lower())


def client_ip(request) -> str:
    """Client address; X-Forwarded-For is trusted only from a local reverse proxy."""
    host = request.client.host if request.client else "?"
    fwd = request.headers.get("x-forwarded-for")
    if fwd and host in ("127.0.0.1", "::1"):
        return fwd.split(",")[0].strip()
    return host
