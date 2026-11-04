"""Publishers survive a backend restart (upgrade/crash) and are re-adopted.

MediaMTX runs as its own process (like mediamtx.service), the backend runs
with mediamtx_control=external and keep_publishers_on_restart=True.
"""

import subprocess
import threading
import time

import pytest

from helpers import ffmpeg_publishers, packet_times, rtsp_packets, wait_for

pytestmark = pytest.mark.integration


@pytest.fixture
def external_mediamtx_rig(rig_factory, tmp_path):
    base = tmp_path / "survive"
    rig = rig_factory(["h264_25fps_audio.mp4"], base=base, mediamtx_control="external",
                      keep_publishers_on_restart=True)
    mtx = subprocess.Popen(["mediamtx", str(base / "mediamtx.yml")], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL)
    yield rig, base
    mtx.terminate()
    mtx.wait(5)
    for pid in ffmpeg_publishers(str(base / "data" / "run" / "streams")):  # cleanup survivors
        subprocess.run(["kill", str(pid)])


def test_backend_restart_does_not_interrupt_readers(external_mediamtx_rig, rig_factory):
    rig, base = external_mediamtx_rig
    with rig:
        s = rig.create("survivor", "h264_25fps_audio.mp4", start="00:00:02", end="00:00:06")
        rig.wait_status(s["id"], "RUNNING", 15)
        before = rig.stream(s["id"])
        url = rig.url("survivor")

        result = {}

        def reader():  # one continuous RTSP session across the backend restart
            pkts, wall, _ = rtsp_packets(url, 16, timeout=60)
            result.update(ts=sorted(packet_times(pkts)), wall=wall)

        th = threading.Thread(target=reader)
        th.start()
        time.sleep(3)
    # backend #1 is gone; the publisher must still be running
    survivors = ffmpeg_publishers(str(base / "data" / "run" / "streams"))
    assert survivors == [before["pid"]], survivors

    with rig_factory(["h264_25fps_audio.mp4"], base=base, mediamtx_control="external",
                     keep_publishers_on_restart=True) as rig2:
        d = rig2.wait_status(s["id"], "RUNNING", 15)
        assert d["pid"] == before["pid"]  # adopted, not restarted
        assert d["restart_count"] == before["restart_count"]
        rep = rig2.client.get("/api/system/recovery").json()
        assert rep.get("publishers_adopted") == 1
        th.join(60)
        ts = result["ts"]
        span = ts[-1] - ts[0]
        steps = [b - a for a, b in zip(ts, ts[1:])]
        print(f"\nreader across backend restart: media {span:.1f}s, max gap {max(steps):.3f}s, wall {result['wall']:.1f}s")
        assert span == pytest.approx(16, abs=0.6) and max(steps) < 0.2  # no interruption at all
        time.sleep(2)
        d = rig2.stream(s["id"])
        assert d["runtime"]["loop_count"] >= 4  # counter continued from the adopted process
        assert d["log_tail"] and any("adopted" in ln for ln in d["log_tail"])
        # user actions still work on an adopted publisher
        r = rig2.client.post(f"/api/streams/{s['id']}/stop")
        assert r.json()["status"] == "STOPPED"
        assert ffmpeg_publishers(rig2.run_dir_marker()) == []


def test_changed_config_publisher_is_replaced_not_adopted(external_mediamtx_rig, rig_factory):
    """After an upgrade that changes the publish command, the old publisher is
    replaced (never two publishers, never a stale command kept running)."""
    import shutil
    rig, base = external_mediamtx_rig
    with rig:
        s = rig.create("changed", "h264_25fps_audio.mp4")
        rig.wait_status(s["id"], "RUNNING", 15)
        old_pid = rig.stream(s["id"])["pid"]
    # new backend builds a different argv (absolute ffmpeg path instead of "ffmpeg")
    with rig_factory(["h264_25fps_audio.mp4"], base=base, mediamtx_control="external",
                     keep_publishers_on_restart=True, ffmpeg_bin=shutil.which("ffmpeg")) as rig2:
        t0 = time.monotonic()
        wait_for(lambda: rig2.client.get("/api/system/recovery").json()["state"] == "done", 30, desc="recovery")
        d = rig2.wait_status(s["id"], "RUNNING", 30)
        print(f"\nreplaced outdated publisher; RUNNING {time.monotonic() - t0:.1f}s after start")
        assert d["pid"] != old_pid
        assert d["ffmpeg_command"][0] == shutil.which("ffmpeg")
        assert ffmpeg_publishers(rig2.run_dir_marker()) == [d["pid"]]
