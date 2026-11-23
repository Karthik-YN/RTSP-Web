"""Settings changed from the dashboard at runtime, persisted in SQLite.

Today: the capacity limits. ``RTSPM_MAX_ACTIVE_STREAMS`` /
``RTSPM_MAX_TRANSCODE_STREAMS`` stay the defaults; a value saved here (after a
load test on the actual server) overrides them and survives restarts.
"""

from __future__ import annotations

import threading

from sqlalchemy import text

from ..database import Database, utcnow

LIMITS = {
    # key: (lowest, highest)
    "max_active_streams": (1, 500),
    "max_transcode_streams": (0, 64),
}


class RuntimeSettings:
    def __init__(self, db: Database, settings):
        self.db = db
        self.s = settings
        self._lock = threading.Lock()
        self._cache: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        with self._lock:
            if self._cache is None:
                with self.db.session() as s:
                    rows = s.execute(text("SELECT key, value, updated_at, updated_by FROM app_settings")).all()
                self._cache = {r[0]: {"value": r[1], "updated_at": r[2], "updated_by": r[3]} for r in rows}
            return self._cache

    def get_int(self, key: str) -> int:
        row = self._load().get(key)
        if row is not None:
            try:
                return int(row["value"])
            except (TypeError, ValueError):
                pass
        return int(getattr(self.s, key))

    def get_str(self, key: str) -> str | None:
        row = self._load().get(key)
        return None if row is None else row["value"]

    def set_str(self, key: str, value: str | None, username: str | None) -> None:
        """Save a text value (None = back to the environment default)."""
        with self.db.session() as s:
            if value is None:
                s.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": key})
            else:
                s.execute(text(
                    "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :t, :u) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by"),
                    {"k": key, "v": value, "t": utcnow().isoformat(), "u": username})
        with self._lock:
            self._cache = None

    def info(self, key: str) -> dict:
        row = self._load().get(key)
        return {"value": self.get_int(key), "default": int(getattr(self.s, key)),
                "overridden": row is not None, "updated_at": row and row["updated_at"],
                "updated_by": row and row["updated_by"], "min": LIMITS[key][0], "max": LIMITS[key][1]}

    def set_int(self, key: str, value: int | None, username: str | None) -> None:
        """Save a value (None = back to the environment default)."""
        lo, hi = LIMITS[key]
        if value is not None and not lo <= value <= hi:
            raise ValueError(f"{key} must be between {lo} and {hi}.")
        with self.db.session() as s:
            if value is None:
                s.execute(text("DELETE FROM app_settings WHERE key = :k"), {"k": key})
            else:
                s.execute(text(
                    "INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :t, :u) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at, "
                    "updated_by = excluded.updated_by"),
                    {"k": key, "v": str(value), "t": utcnow().isoformat(), "u": username})
        with self._lock:
            self._cache = None
