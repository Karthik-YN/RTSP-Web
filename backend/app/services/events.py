"""Audit/event log persisted in SQLite (who did what, failures, recoveries)."""

from __future__ import annotations

import logging

from sqlalchemy import delete, select

from ..database import Database, utcnow
from ..models import Event

log = logging.getLogger("rtspm.events")

MAX_EVENTS = 20_000


class EventLog:
    def __init__(self, db: Database, alerts=None):
        self.db = db
        self.alerts = alerts  # optional AlertSender
        self._count = 0

    def add(self, kind: str, message: str, level: str = "INFO", stream_id: int | None = None,
            stream_name: str | None = None, username: str | None = None, data: dict | None = None) -> None:
        getattr(log, level.lower(), log.info)("%s %s%s", kind, f"[{stream_name}] " if stream_name else "", message)
        if self.alerts is not None:
            try:
                self.alerts.notify(kind, level, message, stream_name)
            except Exception:  # pragma: no cover - alerting must never break callers
                log.exception("alert failed")
        try:
            with self.db.session() as s:
                s.add(Event(ts=utcnow(), level=level, kind=kind, stream_id=stream_id,
                            stream_name=stream_name, username=username, message=message, data_json=data))
            self._count += 1
            if self._count % 500 == 0:
                self.prune()
        except Exception:  # pragma: no cover - event logging must never break callers
            log.exception("could not persist event")

    def prune(self) -> None:
        with self.db.session() as s:
            cutoff = s.scalar(select(Event.id).order_by(Event.id.desc()).offset(MAX_EVENTS).limit(1))
            if cutoff:
                s.execute(delete(Event).where(Event.id <= cutoff))

    def recent(self, limit: int = 100, stream_id: int | None = None, kind: str | None = None) -> list[Event]:
        with self.db.session() as s:
            q = select(Event).order_by(Event.id.desc()).limit(limit)
            if stream_id is not None:
                q = q.where(Event.stream_id == stream_id)
            if kind:
                q = q.where(Event.kind == kind)
            return list(s.scalars(q))
