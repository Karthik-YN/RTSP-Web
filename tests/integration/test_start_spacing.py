"""2 s start spacing, end to end: no two cameras of the same video show the same
frame at the same moment (the edge would discard it as a duplicate).

Each reader's decoded frames are matched to frame numbers of the source file,
so what is checked is the content every camera actually sends, not timings
reported by the backend.
"""

from __future__ import annotations

import concurrent.futures as cf
import itertools
import subprocess
import time
from datetime import datetime

import pytest

from helpers import FIXTURES, ffmpeg_publishers, wait_for

pytestmark = pytest.mark.integration

VIDEO = "h264_25fps_audio.mp4"  # 20 s, 25 fps, keyframe every 1 s
FPS = 25.0
SPACING = 2.0


def _framemd5(args: list[str]) -> tuple[list[tuple[float, str]], float]:
    out = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *args, "-map", "0:v", "-f", "framemd5", "-"],
                         capture_output=True, text=True, timeout=60).stdout
    tb = 1 / 90000
    frames = []
    for ln in out.splitlines():
        if ln.startswith("#tb 0:"):
            num, den = ln.split(":", 1)[1].strip().split("/")
            tb = int(num) / int(den)
        if ln.startswith("#") or not ln.strip():
            continue
        p = [x.strip() for x in ln.split(",")]
        frames.append((int(p[2]) * tb, p[5]))
    return frames, tb


_SOURCE: dict[str, dict[str, int]] = {}


def source_index(filename: str) -> dict[str, int]:
    if filename not in _SOURCE:
        frames, _ = _framemd5(["-i", str(FIXTURES / filename)])
        _SOURCE[filename] = {m: i for i, (_, m) in enumerate(frames)}
    return _SOURCE[filename]


def content_positions(rig, names: list[str], filename: str, seconds: float = 2.5) -> dict[str, float]:
    """Read every stream at the same moment and return, per stream, which second
    of the source it was showing when the readers started (frame index of the
    first decoded frame minus how long after the start that frame arrived)."""
    idx = source_index(filename)
    start = time.monotonic() + 0.3

    def read(name):
        time.sleep(max(0.0, start - time.monotonic()))
        frames, _ = _framemd5(["-rtsp_transport", "tcp", "-i", rig.url(name), "-t", str(seconds)])
        for pts, md5 in frames:
            if md5 in idx:
                return name, idx[md5] / FPS - pts
        raise AssertionError(f"{name}: no frame of the reader matched the source ({len(frames)} frames)")

    with cf.ThreadPoolExecutor(len(names)) as ex:
        return dict(ex.map(read, names))


def circular(a: float, b: float, period: float) -> float:
    d = (a - b) % period
    return min(d, period - d)


def assert_apart(pos: dict[str, float], period: float, minimum: float) -> float:
    closest = min(circular(pos[a], pos[b], period) for a, b in itertools.combinations(pos, 2))
    detail = ", ".join(f"{n} {p % period:.2f}s" for n, p in sorted(pos.items(), key=lambda kv: kv[1] % period))
    print(f"\ncontent positions: {detail}; closest pair {closest:.2f}s apart")
    assert closest >= minimum, f"two cameras only {closest:.2f}s apart in content ({detail})"
    return closest


# The reader-side measurement has about 0.1 s of jitter (process start, first
# packet); the backend guarantees >= spacing between frames going on air.
TOLERANCE = 0.15


def test_six_cameras_of_one_video_are_spaced_and_queue_is_visible(rig_factory):
    with rig_factory([VIDEO], start_spacing_s=SPACING) as rig:
        names = [f"cam-{i}" for i in range(1, 7)]
        t0 = time.monotonic()
        ids = []
        for n in names:
            t = time.monotonic()
            ids.append(rig.create(n, VIDEO)["id"])
            assert time.monotonic() - t < 2.0, "create must not wait for the start queue"
        # later cameras are visibly queued, with a position and an estimate
        last = rig.stream(ids[-1])
        assert last["status"] == "STARTING"
        assert last["status_detail"].startswith("Queued for a start slot")
        assert last["runtime"]["start_queue"]["position"] >= 1
        assert last["runtime"]["start_queue"]["eta_s"] > 0
        sysq = rig.client.get("/api/system").json()["start_queue"]
        assert sysq["spacing_s"] == SPACING and sysq["queued"] >= 1
        for i in ids:
            rig.wait_status(i, "RUNNING", 40)
        took = time.monotonic() - t0
        print(f"\n6 cameras RUNNING {took:.1f}s after the first create")
        assert took >= 5 * SPACING
        pos = content_positions(rig, names, VIDEO)
        assert_apart(pos, 20.0, SPACING - TOLERANCE)
        assert rig.client.get("/api/system").json()["start_queue"]["queued"] == 0


def test_restart_on_a_multiple_of_the_loop_is_moved_off_the_other_camera(rig_factory):
    period = 8.0
    with rig_factory([VIDEO], start_spacing_s=SPACING) as rig:
        a = rig.create("loopA", VIDEO, end="00:00:08")["id"]
        b = rig.create("loopB", VIDEO, end="00:00:08")["id"]
        rig.wait_status(a, "RUNNING", 20)
        rig.wait_status(b, "RUNNING", 20)
        rig.client.post(f"/api/streams/{b}/stop")
        rig.wait_status(b, "STOPPED")
        # restart B when A is just about to wrap to frame 0: without the phase
        # rule B's first frame would go on air with A's first frame
        pos_a = content_positions(rig, ["loopA"], VIDEO, seconds=1.5)["loopA"]
        read_at = time.monotonic() - 0.3  # positions refer to when the reader started (~0.3 s after the call)
        startup = 0.5
        to_wrap = (period - (pos_a % period) - startup) % period
        time.sleep(max(0.0, to_wrap - (time.monotonic() - read_at)))
        r = rig.client.post(f"/api/streams/{b}/start")
        assert r.status_code == 200, r.text
        rig.wait_status(b, "RUNNING", 20)
        pos = content_positions(rig, ["loopA", "loopB"], VIDEO)
        assert_apart(pos, period, SPACING - TOLERANCE)
        log = "\n".join(rig.client.get(f"/api/streams/{b}/logs?lines=50").json()["lines"])
        assert "start delayed" in log


def test_clip_too_short_for_all_cameras_warns_and_still_starts(rig_factory):
    with rig_factory([VIDEO], start_spacing_s=SPACING) as rig:
        ids = [rig.create(f"short{i}", VIDEO, end="00:00:04")["id"] for i in range(3)]
        for i in ids:
            rig.wait_status(i, "RUNNING", 40)
        warned = [rig.stream(i)["runtime"]["spacing_warning"] for i in ids]
        assert warned[0] is None
        assert warned[2] and "too short" in warned[2]
        kinds = [e["kind"] for e in rig.client.get("/api/events?limit=100").json()]
        assert "stream.spacing_short" in kinds


def test_stopping_a_queued_stream_takes_it_out_at_once(rig_factory):
    with rig_factory([VIDEO], start_spacing_s=SPACING) as rig:
        ids = [rig.create(f"q{i}", VIDEO)["id"] for i in range(4)]
        last = ids[-1]
        assert rig.stream(last)["runtime"]["start_queue"] is not None
        t = time.monotonic()
        r = rig.client.post(f"/api/streams/{last}/stop")
        assert r.status_code == 200
        assert time.monotonic() - t < 1.5, "stop must not wait behind the queue"
        s = rig.stream(last)
        assert s["status"] == "STOPPED" and s["runtime"]["start_queue"] is None
        for i in ids[:-1]:
            rig.wait_status(i, "RUNNING", 30)
        time.sleep(SPACING + 1)
        assert rig.stream(last)["status"] == "STOPPED"
        assert len(ffmpeg_publishers(rig.run_dir_marker())) == 3
        # deleting a queued stream works the same way
        extra = rig.create("q-del", VIDEO)["id"]
        assert rig.client.delete(f"/api/streams/{extra}").status_code in (200, 204)
        time.sleep(SPACING + 1)
        assert len(ffmpeg_publishers(rig.run_dir_marker())) == 3


@pytest.mark.parametrize("mode", ["copy", "passthrough", "cfr"])
def test_publishers_drop_and_duplicate_no_frames(rig_factory, mode):
    """No frame is dropped or repeated while queued starts run side by side,
    including across a loop boundary (5 s loop, 12 s observed).

    Known, pre-existing: transcoding with output_fps_mode=cfr (not the
    default) repeats the very first frame once at publisher start; it must
    not grow afterwards."""
    kw = {} if mode == "copy" else {"output_fps_mode": mode}
    with rig_factory([VIDEO], start_spacing_s=SPACING, **kw) as rig:
        body = {"end": "00:00:05"} | ({} if mode == "copy" else {"force_transcode": True})
        ids = [rig.create(f"df{i}", VIDEO, **body)["id"] for i in range(2)]
        for i in ids:
            rig.wait_status(i, "RUNNING", 30)
        time.sleep(12)
        allowed_dup = 1 if mode == "cfr" else 0
        for i in ids:
            prog = rig.stream(i)["runtime"]["progress"]
            print(f"\n{mode}: frame {prog['frame']} drop {prog['drop_frames']} dup {prog['dup_frames']} "
                  f"loops {rig.stream(i)['runtime']['loop_count']}")
            assert prog["out_time_s"] >= 10, prog
            # FFmpeg 6.1 (Ubuntu 24.04) reports frame=0 for stream copy; 5.1 and 7+ count them
            assert prog["frame"] > 250 or (mode == "copy" and prog["frame"] == 0), prog
            assert rig.stream(i)["runtime"]["loop_count"] >= 2
            assert prog["drop_frames"] == 0 and prog["dup_frames"] <= allowed_dup, (mode, prog)


def test_reboot_recovery_of_16_streams_is_spaced_and_within_two_minutes(rig_factory, tmp_path):
    # 8 cameras on the 20 s clip (fits 8 x 2.25 s) + 8 on the 2-hour file
    base = tmp_path / "persist16"
    vids = [VIDEO, "long_2h.mp4"]
    with rig_factory(vids, base=base, start_spacing_s=0) as rig:
        ids = [rig.create(f"r{i:02d}", vids[i // 8])["id"] for i in range(16)]
        for i in ids:
            rig.wait_status(i, "RUNNING", 40)
    assert ffmpeg_publishers(str(base / "data" / "run" / "streams")) == []
    t0 = time.monotonic()
    with rig_factory([], base=base, start_spacing_s=SPACING) as rig2:  # videos already in place
        for i in ids:
            rig2.wait_status(i, "RUNNING", 120)
        elapsed = time.monotonic() - t0
        rep = wait_for(lambda: (j := rig2.client.get("/api/system/recovery").json())["state"] == "done" and j, 60,
                       desc="recovery report")
        ready = sorted(datetime.fromisoformat(r["ready_at"]).timestamp() for r in rep["results"])
        gaps = [b - a for a, b in zip(ready, ready[1:])]
        print(f"\nreboot: 16/16 RUNNING {elapsed:.1f}s after app start; recovery {rep['recovery_duration_s']}s; "
              f"on-air gaps min {min(gaps):.2f}s max {max(gaps):.2f}s")
        assert rep["streams_running"] == 16 and rep["start_spacing_s"] == SPACING
        assert min(gaps) >= SPACING - 0.05
        assert rep["recovery_duration_s"] < 120
        assert max(gaps) < SPACING + 3.0  # no stream held the queue
        pos = content_positions(rig2, [f"r{i:02d}" for i in range(8)], VIDEO)
        assert_apart(pos, 20.0, SPACING - TOLERANCE)
