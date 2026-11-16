"""Webhook alerts for events that need a person (Teams / Slack / generic JSON).

Configure ``RTSPM_ALERT_WEBHOOK_URL``. The body is ``{"text": ..., "event": {...}}``,
which Slack and Microsoft Teams incoming webhooks both accept. Alerts are
rate-limited per (kind, stream) and sent from a background thread so a slow
or unreachable webhook never blocks the backend.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

log = logging.getLogger(__name__)

ALERT_KINDS = {
    "stream.error", "stream.source_invalid", "stream.stalled", "mediamtx.down", "mediamtx.up",
    "recovery.blocked", "recovery.done", "system.warning", "network.address_changed", "health.degraded", "backup.failed",
}


class AlertSender:
    def __init__(self, url: str | None, min_interval_s: float = 300, app_url: str | None = None,
                 server_name: str = "RIG-02"):
        self.url = url
        self.min_interval_s = min_interval_s
        self.app_url = app_url
        self.server_name = server_name
        self._last: dict[tuple, float] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="alerts") if url else None
        self.sent = 0
        self.failed = 0

    def wants(self, kind: str, level: str) -> bool:
        return bool(self.url) and (kind in ALERT_KINDS or level == "ERROR")

    def notify(self, kind: str, level: str, message: str, stream_name: str | None = None) -> bool:
        """Queue an alert; returns False if filtered or rate-limited."""
        if not self.wants(kind, level):
            return False
        key = (kind, stream_name)
        now = time.monotonic()
        with self._lock:
            if now - self._last.get(key, -1e9) < self.min_interval_s:
                return False
            self._last[key] = now
        icon = {"ERROR": "🔴", "WARNING": "🟠"}.get(level, "🟢")
        subject = f"[{self.server_name}] {stream_name + ': ' if stream_name else ''}{message}"
        text = f"{icon} {subject}" + (f"\n{self.app_url}" if self.app_url else "")
        body = {"text": text, "event": {"kind": kind, "level": level, "stream": stream_name, "message": message}}
        self._pool.submit(self._post, body)
        return True

    def _post(self, body: dict) -> None:
        try:
            r = httpx.post(self.url, json=body, timeout=10)
            r.raise_for_status()
            self.sent += 1
        except Exception as exc:  # never break the app because of alerting
            self.failed += 1
            log.warning("alert webhook failed: %s", exc)

    def close(self) -> None:
        if self._pool:
            self._pool.shutdown(wait=True, cancel_futures=False)
