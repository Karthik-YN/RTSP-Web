"""Real FFmpeg -> MediaMTX -> RTSP reader tests for the core lifecycle."""

import subprocess
import time

import pytest

from helpers import ffmpeg_publishers, packet_times, rtsp_packets, wait_for

pytestmark = pytest.mark.integration


def test_full_lifecycle(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        c = rig.client
        t0 = time.monotonic()
        s = rig.create("cam1", "h264_25fps_audio.mp4", start="00:00:05", mode="loop")
        assert s["rtsp_url"] == f"rtsp://rig.test:{rig.ports['rtsp']}/cam1"
        assert s["status"] in ("STARTING", "RUNNING") and s["desired_state"] == "RUNNING"
        s = rig.wait_status(s["id"], "RUNNING", 15)
        startup = time.monotonic() - t0
        assert startup < 10, f"stream took {startup:.1f}s to become RUNNING"
        assert s["runtime"]["path_ready"] is True and s["pid"]
        assert s["output_mode"] == "copy" and s["effective_start_s"] == pytest.approx(5.0, abs=0.01)

        # The published stream: H.264 video only (audio dropped)
        pkts, wall, info = rtsp_packets(rig.url("cam1"), 3)
        types = [st["codec_type"] for st in info["streams"]]
        assert types == ["video"], types
        assert info["streams"][0]["codec_name"] == "h264"
        assert len(pkts) > 50

        # Viewer count is reported from MediaMTX while a reader is connected
        reader = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i",
                                   rig.url("cam1"), "-f", "null", "-"])
        try:
            wait_for(lambda: rig.stream(s["id"])["runtime"]["viewer_count"] == 1, 10, desc="1 viewer")
            d = rig.stream(s["id"])
            assert d["runtime"]["readers"][0]["remote_addr"].startswith("127.0.0.1")
            assert c.get("/api/system").json()["viewers"] == 1
        finally:
            reader.terminate()
            reader.wait()
        wait_for(lambda: rig.stream(s["id"])["runtime"]["viewer_count"] == 0, 10, desc="viewer gone")
        assert rig.stream(s["id"])["runtime"]["viewer_peak_today"] == 1  # the day's peak is kept

        # Detail panel data
        d = rig.stream(s["id"])
        assert d["ffmpeg_command"][0] == "ffmpeg" and "-re" in d["ffmpeg_command"]
        assert "rtsp://127.0.0.1" in d["ffmpeg_command_text"]
        assert 1 <= len(d["log_tail"]) <= 20
        assert d["video"]["filename"] == "h264_25fps_audio.mp4"
        assert c.get(f"/api/streams/{s['id']}/logs?lines=5").json()["lines"]
        snap = c.get(f"/api/streams/{s['id']}/snapshot")
        assert snap.status_code == 200 and snap.content[:2] == b"\xff\xd8"

        # Stop keeps the configuration
        pid = d["pid"]
        r = c.post(f"/api/streams/{s['id']}/stop")
        assert r.status_code == 200 and r.json()["status"] == "STOPPED" and r.json()["desired_state"] == "STOPPED"
        assert ffmpeg_publishers(rig.run_dir_marker()) == []
        wait_for(lambda: not rig.stream(s["id"])["runtime"]["path_ready"], 5, desc="path gone")
        d = rig.stream(s["id"])
        assert d["start_offset_s"] == 5.0 and d["pid"] is None

        # Start again from the saved configuration
        r = c.post(f"/api/streams/{s['id']}/start?wait=true")
        assert r.json()["status"] == "RUNNING"
        new_pid = r.json()["pid"]
        assert new_pid != pid

        # Restart replaces the publisher: exactly one process afterwards
        r = c.post(f"/api/streams/{s['id']}/restart?wait=true")
        assert r.json()["status"] == "RUNNING" and r.json()["pid"] != new_pid
        assert ffmpeg_publishers(rig.run_dir_marker()) == [r.json()["pid"]]
        assert c.post(f"/api/streams/{s['id']}/start").status_code == 409  # already running

        # Delete stops the publisher and keeps the source file
        assert c.delete(f"/api/streams/{s['id']}").status_code == 204
        assert ffmpeg_publishers(rig.run_dir_marker()) == []
        assert (rig.video_dir / "h264_25fps_audio.mp4").exists()
        kinds = [e["kind"] for e in c.get("/api/events?limit=50").json()]
        for k in ("stream.created", "stream.stop", "stream.restart", "stream.deleted"):
            assert k in kinds


def test_realtime_loop_continuity_and_loop_count(rig_factory):
    """EC-04 (short clip): 4 s loop read for 14 s: real time, monotonic, no gaps."""
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        s = rig.create("loop4", "h264_25fps_audio.mp4", start="00:00:03", end="00:00:07", mode="loop")
        rig.wait_status(s["id"], "RUNNING", 15)
        pkts, wall, _ = rtsp_packets(rig.url("loop4"), 14)
        ts = packet_times(pkts)
        span = max(ts) - min(ts)
        assert span == pytest.approx(14, abs=0.6)
        # real time: 14 s of media must take ~14 s of wall clock (not faster/slower)
        assert wall == pytest.approx(span, rel=0.08), (wall, span)
        dts = sorted(ts)
        steps = [b - a for a, b in zip(dts, dts[1:])]
        assert max(steps) < 0.2, f"timestamp gap {max(steps):.3f}s at a loop boundary"
        assert len(set(round(t, 4) for t in ts)) == len(ts), "duplicate timestamps"
        d = rig.stream(s["id"])
        assert d["runtime"]["loop_period_s"] == pytest.approx(4.0, abs=0.01)
        assert d["runtime"]["loop_count"] >= 3, d["runtime"]
        assert d["restart_count"] == 0 and d["status"] == "RUNNING"


def test_play_once_completes_and_stays_completed(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        s = rig.create("once", "h264_25fps_audio.mp4", start="00:00:16", mode="once")
        rig.wait_status(s["id"], "RUNNING", 15)
        d = rig.wait_status(s["id"], "COMPLETED", 15)
        assert d["desired_state"] == "STOPPED" and d["pid"] is None
        assert ffmpeg_publishers(rig.run_dir_marker()) == []
        time.sleep(2)
        assert rig.stream(s["id"])["status"] == "COMPLETED"  # not restarted


def test_same_source_two_independent_streams(rig_factory):
    """EC-05: one file, two names, two start offsets."""
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        a = rig.create("sameA", "h264_25fps_audio.mp4", start="00:00:02")
        b = rig.create("sameB", "h264_25fps_audio.mp4", start="00:00:12")
        rig.wait_status(a["id"], "RUNNING")
        rig.wait_status(b["id"], "RUNNING")
        pa, pb = rig.stream(a["id"])["pid"], rig.stream(b["id"])["pid"]
        assert pa != pb
        assert sorted(ffmpeg_publishers(rig.run_dir_marker())) == sorted([pa, pb])
        # independent playback positions
        time.sleep(1.5)
        posa = rig.stream(a["id"])["runtime"]["playback_position_s"]
        posb = rig.stream(b["id"])["runtime"]["playback_position_s"]
        assert posa < 8 and posb > 12, (posa, posb)
        # stopping one leaves the other untouched
        rig.client.post(f"/api/streams/{a['id']}/stop")
        time.sleep(2)
        db = rig.stream(b["id"])
        assert db["status"] == "RUNNING" and db["pid"] == pb and db["restart_count"] == 0
        pkts, _, _ = rtsp_packets(rig.url("sameB"), 2)
        assert len(pkts) > 20
