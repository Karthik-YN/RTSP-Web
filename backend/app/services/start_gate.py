"""Start queue that keeps publisher starts apart (edge de-duplication).

The edge discards identical frames that arrive together from several cameras,
so two streams must never show the same frame at the same moment. Two rules
enforce that:

* **spacing**: every publisher start (manual, bulk, retries, reboot recovery)
  goes through one first-in-first-out queue. A start is launched only once the
  previous one is on air (MediaMTX reports the path ready) and ``spacing_s``
  has passed since. Measuring from "on air" instead of from the launch makes
  the gap independent of FFmpeg's variable start-up time. If the previous
  start does not come up within ``ready_wait_s`` the queue moves on;
* **phase**: streams of the same video and segment loop with the same period,
  so a stream restarted later can land on the very frame another camera is
  showing. Before launching, the caller works out which frame each such
  stream shows (``phase_delay``) and the start is delayed until the new
  stream would be at least ``gap`` seconds of content away from all of them.

The queue never blocks the caller: ``submit`` returns a ticket at once and
the caller can await ``ticket.launched``. ``spacing_s == 0`` disables the
queue (callers launch directly).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

# (origin_wall_s, period_s, loops): when a peer showed its first frame, its
# loop period, and whether it loops (play-once streams do not wrap).
Peer = tuple[float, float, bool]


@dataclass(eq=False)
class Ticket:
    stream_id: int
    user_initiated: bool
    enqueued_mono: float
    order: float = 0.0  # queue order: when the start was requested (lead-ins end in any order)
    cancelled: bool = False
    phase_tries: int = 0
    launched: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def resolve(self, value: bool) -> None:
        if not self.launched.done():
            self.launched.set_result(value)


def _distance(t_wall: float, peer: Peer) -> float:
    """Content distance (s) between frame 0 shown at ``t_wall`` and what the peer shows then."""
    origin, period, loops = peer
    pos = t_wall - origin
    if loops and period > 0:
        pos %= period
        return min(pos, period - pos)
    if pos >= period > 0:  # a play-once peer that has finished shows nothing
        return math.inf
    return abs(pos)


def phase_delay(start_wall: float, peers: list[Peer], gap: float,
                max_best_effort_s: float | None = None) -> tuple[float, float | None, bool]:
    """Smallest delay so a stream showing frame 0 at ``start_wall + delay`` is at
    least ``gap`` seconds of content away from every peer.

    Returns ``(delay, distance, ok)``: ``distance`` is the closest peer after
    the delay (None without peers). When no delay can satisfy every peer (the
    clip is too short for that many cameras), ``ok`` is False and the delay
    that keeps the closest peer farthest away is returned (best effort),
    looking no further ahead than ``max_best_effort_s`` so a crowded short
    clip cannot hold the whole start queue for a full loop per camera.
    """
    if not peers or gap <= 0:
        return 0.0, None, True

    def worst(d: float) -> float:
        return min(_distance(start_wall + d, p) for p in peers)

    eps = 1e-3
    cands = {0.0}
    for origin, period, loops in peers:
        pos = start_wall - origin
        if loops and period > 0:
            cands.add((gap - pos) % period + eps)
        elif gap - pos > 0:
            cands.add(gap - pos + eps)
    ordered = sorted(cands)
    for d in ordered:
        w = worst(d)
        if w >= gap - eps:
            return d, w, True
    # Best effort: halfway between two looping cameras' frame 0 is as far as one can get.
    loop_periods = [period for _, period, loops in peers if loops and period > 0]
    if loop_periods:
        period = min(loop_periods)
        zs = sorted((origin - start_wall) % period for origin, p, loops in peers if loops and p > 0)
        for z0, z1 in zip(zs, zs[1:] + [zs[0] + period]):
            ordered.append(((z0 + z1) / 2) % period)
    limit = math.inf if max_best_effort_s is None else max(0.0, max_best_effort_s)
    pool = [d for d in ordered if d <= limit]
    if math.isfinite(limit):
        pool += [i * 0.05 for i in range(int(limit / 0.05) + 1)]
    best = max(pool, key=worst)
    return best, worst(best), False


class StartGate:
    def __init__(self, spacing_s: float, ready_wait_s: float,
                 launch: Callable[[Ticket], Awaitable[asyncio.Future | None]],
                 phase: Callable[[Ticket], Awaitable[float]] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable] = asyncio.sleep):
        self.spacing_s = max(0.0, float(spacing_s))
        self.ready_wait_s = max(0.1, float(ready_wait_s))
        self._launch = launch
        self._phase = phase
        self._clock = clock
        self._sleep = sleep
        self.queue: deque[Ticket] = deque()
        self.current: Ticket | None = None
        self.last_on_air: float | None = None
        self.startup_estimate_s = 0.5  # moving average of launch -> on air
        self.launches = 0
        self._wake: asyncio.Event | None = None
        self._task: asyncio.Task | None = None
        self._closed = False

    @property
    def enabled(self) -> bool:
        return self.spacing_s > 0

    # ------------------------------------------------------------ public
    def submit(self, stream_id: int, user_initiated: bool = False, order: float | None = None) -> Ticket:
        """Queue a start. ``order`` (monotonic time the start was requested) keeps
        cameras in the order they were asked for, e.g. when their lead-ins end
        a few milliseconds apart in a different order; default: now (FIFO)."""
        now = self._clock()
        t = Ticket(stream_id=stream_id, user_initiated=user_initiated, enqueued_mono=now,
                   order=now if order is None else order)
        if self._closed:
            t.cancelled = True
            t.resolve(False)
            return t
        i = len(self.queue)
        while i > 0 and self.queue[i - 1].order > t.order:
            i -= 1
        self.queue.insert(i, t)
        if self._wake is None:
            self._wake = asyncio.Event()
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="start-gate")
        self._wake.set()
        return t

    def cancel(self, ticket: Ticket) -> None:
        ticket.cancelled = True
        ticket.resolve(False)
        with contextlib.suppress(ValueError):
            self.queue.remove(ticket)

    def position(self, ticket: Ticket) -> int | None:
        """1-based place in the queue (1 = next), None once launched or cancelled."""
        if ticket.cancelled:
            return None
        n = 0
        for t in self.queue:
            if t.cancelled:
                continue
            n += 1
            if t is ticket:
                return n
        return None

    def eta_s(self, ticket: Ticket) -> float | None:
        """Rough seconds until this ticket launches."""
        pos = self.position(ticket)
        if pos is None:
            return None
        step = self.spacing_s + self.startup_estimate_s
        now = self._clock()
        head = max(0.0, (self.last_on_air + self.spacing_s - now)) if self.last_on_air is not None else 0.0
        if self.current is not None:
            head = step
        return round(head + (pos - 1) * step, 1)

    def pending(self) -> int:
        return sum(1 for t in self.queue if not t.cancelled)

    async def close(self) -> None:
        self._closed = True
        for t in list(self.queue):
            self.cancel(t)
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    # ------------------------------------------------------------ worker
    async def _run(self) -> None:
        while not self._closed:
            if not self.queue:
                self._wake.clear()
                await self._wake.wait()
                continue
            t = self.queue[0]
            if t.cancelled:
                self.queue.popleft()
                continue
            if self.last_on_air is not None:
                wait = self.last_on_air + self.spacing_s - self._clock()
                if wait > 0:
                    await self._sleep(wait)
                    continue
            if self._phase is not None and t.phase_tries < 3:
                try:
                    delay = await self._phase(t)
                except Exception:
                    log.exception("phase check failed for stream %s", t.stream_id)
                    delay = 0.0
                if delay > 0.01:
                    t.phase_tries += 1
                    await self._sleep(delay)
                    continue
            self.queue.popleft()
            if t.cancelled:
                continue
            self.current = t
            launched_at = self._clock()
            try:
                ready = await self._launch(t)
            except Exception:
                log.exception("launch failed for stream %s", t.stream_id)
                ready = None
            t.resolve(ready is not None)
            if ready is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait({ready}, timeout=self.ready_wait_s)
                now = self._clock()
                self.last_on_air = now
                self.launches += 1
                if now - launched_at < self.ready_wait_s:
                    self.startup_estimate_s = 0.7 * self.startup_estimate_s + 0.3 * (now - launched_at)
            self.current = None
