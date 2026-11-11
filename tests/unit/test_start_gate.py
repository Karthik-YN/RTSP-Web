"""Start queue (2 s spacing) and same-video phase rule."""

import asyncio
import time

import pytest

from app.services.start_gate import StartGate, phase_delay

GAP = 2.25


# ------------------------------------------------------------------ phase_delay

def test_no_peers_no_delay():
    assert phase_delay(100.0, [], GAP) == (0.0, None, True)


def test_peer_far_away_no_delay():
    d, dist, ok = phase_delay(100.0, [(90.0, 30.0, True)], GAP)  # peer shows second 10
    assert ok and d == 0 and dist == pytest.approx(10.0)


def test_peer_on_same_frame_is_delayed_by_gap():
    d, dist, ok = phase_delay(100.0, [(100.0, 30.0, True)], GAP)
    assert ok and d == pytest.approx(GAP, abs=0.01) and dist >= GAP - 0.01


def test_peer_just_ahead_or_behind():
    # peer is 1 s into the clip: the new stream must wait until the peer is GAP ahead
    d, _, ok = phase_delay(100.0, [(99.0, 30.0, True)], GAP)
    assert ok and d == pytest.approx(GAP - 1.0, abs=0.01)
    # peer is about to wrap (29.5 s into a 30 s loop): its frame 0 comes back in 0.5 s
    d, _, ok = phase_delay(100.0, [(70.5, 30.0, True)], GAP)
    assert ok and d == pytest.approx(0.5 + GAP, abs=0.01)


def test_restart_landing_on_a_multiple_of_the_loop_is_delayed():
    # camera A started at t=0 on a 30 s loop; camera B is restarted at t=60
    d, _, ok = phase_delay(60.0, [(0.0, 30.0, True)], GAP)
    assert ok and d == pytest.approx(GAP, abs=0.01)


def test_several_peers_chain():
    # peer A is at frame 0, peer B wraps to frame 0 in 2 s: waiting GAP for A lands
    # right on B, so the first free moment is GAP after B's wrap
    d, dist, ok = phase_delay(100.0, [(100.0, 60.0, True), (42.0, 60.0, True)], GAP)
    assert ok and dist >= GAP - 0.01
    assert d == pytest.approx(2.0 + GAP, abs=0.01)


def test_clip_too_short_for_all_cameras_is_best_effort():
    # 10 s loop, 5 cameras 2 s apart: there is no place GAP away from all of them
    peers = [(100.0 - 2 * i, 10.0, True) for i in range(5)]
    d, dist, ok = phase_delay(100.0, peers, GAP)
    assert not ok
    assert dist == pytest.approx(1.0, abs=0.01)  # best possible: half-way between two cameras
    assert 0 <= d < 10
    # bounded best effort: never hold the queue longer than asked
    d, dist, ok = phase_delay(100.0, peers, GAP, max_best_effort_s=0.6)
    assert not ok and d <= 0.6 and dist == pytest.approx(0.6, abs=0.06)


def test_play_once_peers():
    # finished play-once peer shows nothing
    assert phase_delay(100.0, [(50.0, 20.0, False)], GAP)[0] == 0
    # play-once peer 1 s in: wait until it is GAP in
    d, _, ok = phase_delay(100.0, [(99.0, 20.0, False)], GAP)
    assert ok and d == pytest.approx(GAP - 1.0, abs=0.01)


# ------------------------------------------------------------------ StartGate

class Recorder:
    """launch() callback: records when each stream launched and comes on air after ``startup``."""

    def __init__(self, startup: float = 0.05, never_ready: set[int] = (), refuse: set[int] = ()):
        self.startup = startup
        self.never_ready = set(never_ready)
        self.refuse = set(refuse)
        self.launched: list[tuple[int, float]] = []
        self.on_air: dict[int, float] = {}

    async def __call__(self, t):
        if t.stream_id in self.refuse:
            return None
        self.launched.append((t.stream_id, time.monotonic()))
        if t.stream_id in self.never_ready:
            return asyncio.get_running_loop().create_future()

        async def ready():
            await asyncio.sleep(self.startup)
            self.on_air[t.stream_id] = time.monotonic()

        return asyncio.create_task(ready())


def run(coro):
    return asyncio.run(coro)


def test_fifo_and_spacing_from_on_air():
    async def go():
        rec = Recorder(startup=0.05)
        gate = StartGate(0.2, 1.0, launch=rec)
        tickets = [gate.submit(i) for i in range(4)]
        assert [gate.position(t) for t in tickets] == [1, 2, 3, 4]
        assert all(await asyncio.gather(*(t.launched for t in tickets)))
        await asyncio.sleep(0.1)
        await gate.close()
        return rec

    rec = run(go())
    assert [sid for sid, _ in rec.launched] == [0, 1, 2, 3]
    for i in range(1, 4):
        # next launch is at least `spacing` after the previous one came on air
        assert rec.launched[i][1] - rec.on_air[i - 1] >= 0.2 - 0.01
        # so frames go on air at least spacing + start-up apart
        assert rec.on_air[i] - rec.on_air[i - 1] >= 0.2


def test_queue_keeps_the_order_starts_were_requested_in():
    """Cameras created together end their lead-ins ms apart in any order; they
    still go live in the order they were asked for. No order = FIFO at the back."""
    async def go():
        rec = Recorder(startup=0.02)
        gate = StartGate(0.05, 1.0, launch=rec)
        base = time.monotonic() - 1.0
        asked = {sid: base + sid * 0.001 for sid in (1, 2, 3)}  # created 1, 2, 3 (a second ago)
        gate.submit(1, True, order=asked[1])  # launches at once
        await asyncio.sleep(0.01)
        t3 = gate.submit(3, True, order=asked[3])  # 3's lead-in ended first...
        t2 = gate.submit(2, True, order=asked[2])  # ...then 2's
        t9 = gate.submit(9)  # a crash retry, no order: at the back
        assert [gate.position(t) for t in (t2, t3, t9)] == [1, 2, 3]
        await asyncio.gather(t2.launched, t3.launched, t9.launched)
        await gate.close()
        return rec

    assert [sid for sid, _ in run(go()).launched] == [1, 2, 3, 9]


def test_stuck_start_does_not_block_queue():
    async def go():
        rec = Recorder(never_ready={0})
        gate = StartGate(0.1, 0.3, launch=rec)
        a, b = gate.submit(0), gate.submit(1)
        await asyncio.gather(a.launched, b.launched)
        await gate.close()
        return rec

    rec = run(go())
    gap = rec.launched[1][1] - rec.launched[0][1]
    assert 0.39 <= gap < 1.0  # ready_wait (0.3) + spacing (0.1)


def test_cancel_queued_ticket_and_refused_launch():
    async def go():
        rec = Recorder(refuse={1})
        gate = StartGate(0.2, 1.0, launch=rec)
        t0, t1, t2, t3 = (gate.submit(i) for i in range(4))
        gate.cancel(t2)
        assert gate.position(t2) is None and gate.position(t3) == 3
        assert gate.eta_s(t3) is not None
        res = await asyncio.gather(t0.launched, t1.launched, t2.launched, t3.launched)
        await gate.close()
        return rec, res

    rec, res = run(go())
    assert res == [True, False, False, True]
    assert [sid for sid, _ in rec.launched] == [0, 3]


def test_phase_delay_is_applied_before_launch():
    calls = []

    async def phase(t):
        calls.append(time.monotonic())
        return 0.3 if len(calls) == 1 else 0.0

    async def go():
        rec = Recorder()
        gate = StartGate(0.1, 1.0, launch=rec, phase=phase)
        t0 = time.monotonic()
        t = gate.submit(7)
        await t.launched
        await gate.close()
        return rec.launched[0][1] - t0

    assert run(go()) >= 0.29
    assert len(calls) == 2


def test_close_cancels_pending():
    async def go():
        rec = Recorder(startup=0.05)
        gate = StartGate(5.0, 1.0, launch=rec)
        a, b = gate.submit(0), gate.submit(1)
        await a.launched
        await gate.close()
        return await b.launched, gate.submit(2)

    second, late = run(go())
    assert second is False and late.cancelled
