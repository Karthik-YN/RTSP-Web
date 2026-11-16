"""Mandatory edge cases EC-01..EC-13 that can be verified without the 5.x edge.

Each test records measurements in its assertion messages / prints so the
results can be copied into docs/ACCEPTANCE_REPORT.md. Edge-device-specific
verification is in docs/EDGE_VALIDATION.md.
"""

import os
import shutil
import signal
import subprocess
import threading
import time

import psutil
import pytest

from helpers import ffmpeg_publishers, packet_times, rtsp_packets, wait_for

pytestmark = pytest.mark.integration


def _realtime_check(rig, name, seconds=10):
    pkts, wall, info = rtsp_packets(rig.url(name), seconds)
    ts = sorted(packet_times(pkts))
    span = ts[-1] - ts[0]
    steps = [b - a for a, b in zip(ts, ts[1:])]
    dup = len(ts) - len(set(round(t, 5) for t in ts))
    return {"span": span, "wall": wall, "ratio": wall / span, "max_step": max(steps), "dups": dup,
            "frames": len(ts), "stream": info["streams"][0]}


# ---------------------------------------------------------------- EC-01
@pytest.mark.parametrize("filename", ["hevc.mp4", "hevc_10bit.mkv", "vp9.webm", "av1.mkv", "h264_10bit.mp4"])
def test_ec01_incompatible_codecs_are_transcoded_to_h264(rig_factory, filename):
    with rig_factory([filename]) as rig:
        s = rig.create("tc", filename)
        assert s["output_mode"] == "transcode"
        assert any("CPU" in w for w in s["warnings"])
        rig.wait_status(s["id"], "RUNNING", 20)
        r = _realtime_check(rig, "tc", 5)
        st = r["stream"]
        print(f"\nEC-01 {filename}: out={st['codec_name']} {st.get('profile')} {st.get('pix_fmt')} ratio={r['ratio']:.3f}")
        assert st["codec_name"] == "h264" and st["codec_type"] == "video"
        assert st.get("pix_fmt") in (None, "yuv420p")  # 8-bit 4:2:0 output
        assert r["ratio"] == pytest.approx(1.0, abs=0.12)


def test_ec01_transcode_limit_enforced(rig_factory):
    with rig_factory(["hevc.mp4"], max_transcode_streams=1) as rig:
        rig.create("tc1", "hevc.mp4")
        r = rig.client.post("/api/streams", json={"name": "tc2", "video_id": rig.video_id("hevc.mp4")})
        assert r.status_code == 409 and "Transcoding capacity" in r.json()["detail"]


# ---------------------------------------------------------------- EC-03
@pytest.mark.parametrize("filename,fps", [("h264_23976.mp4", 23.976), ("h264_2997.mp4", 29.97),
                                          ("h264_5994.mp4", 59.94), ("h264_vfr.mkv", None)])
def test_ec03_odd_and_variable_frame_rates_play_in_real_time(rig_factory, filename, fps):
    with rig_factory([filename]) as rig:
        s = rig.create("fps", filename, mode="loop")
        assert s["output_mode"] == "copy"
        rig.wait_status(s["id"], "RUNNING", 15)
        r = _realtime_check(rig, "fps", 12)
        print(f"\nEC-03 {filename}: media {r['span']:.2f}s over wall {r['wall']:.2f}s ratio={r['ratio']:.3f} "
              f"frames={r['frames']} max_step={r['max_step']:.3f} dups={r['dups']}")
        assert r["ratio"] == pytest.approx(1.0, abs=0.06)  # not faster or slower
        assert r["dups"] == 0
        if fps:
            assert r["frames"] / r["span"] == pytest.approx(fps, rel=0.03)  # no frame-rate change
            assert r["max_step"] < 2.5 / fps
        else:
            assert r["max_step"] < 0.1  # VFR timestamps preserved (20/60 ms pattern)


def test_whole_file_loop_with_bframes_is_seamless(rig_factory):
    """EC-04 short clip: 20 s 29.97 fps B-frame file looped from 00:00:15 (period ~5 s)."""
    with rig_factory(["h264_2997.mp4"]) as rig:
        s = rig.create("wl", "h264_2997.mp4", start="00:00:15")
        rig.wait_status(s["id"], "RUNNING", 15)
        r = _realtime_check(rig, "wl", 16)
        print(f"\nEC-04 loop: ratio={r['ratio']:.3f} max_step={r['max_step']:.3f} dups={r['dups']}")
        assert r["dups"] == 0 and r["max_step"] < 2.5 / 29.97
        assert r["ratio"] == pytest.approx(1.0, abs=0.06)
        d = rig.stream(s["id"])
        # start 15 s snaps to the 14.014 s keyframe (2 s GOP): period ~5.97 s
        assert d["effective_start_s"] == pytest.approx(14.014, abs=0.01)
        assert d["runtime"]["loop_count"] >= 2


def test_ntsc_loop_with_end_has_no_duplicate_timestamps(rig_factory):
    """EC-04 regression: 29.97 fps loop with an end offset (non-decimal DTS, found by the soak test)."""
    with rig_factory(["h264_2997.mp4"]) as rig:
        s = rig.create("ntsc", "h264_2997.mp4", start="00:00:02.002", end="00:00:06.006")
        rig.wait_status(s["id"], "RUNNING", 15)
        r = _realtime_check(rig, "ntsc", 14)  # ~3.5 loop points of 4.004 s
        print(f"\nEC-04 NTSC loop with end: dups={r['dups']} max_step={r['max_step']:.3f} ratio={r['ratio']:.3f}")
        assert r["dups"] == 0 and r["max_step"] < 2.5 / 29.97


# ---------------------------------------------------------------- EC-07
def test_ec07_source_deleted_while_streaming(rig_factory, fixtures_dir):
    with rig_factory(["h264_25fps_audio.mp4", "h264_2997.mp4"]) as rig:
        a = rig.create("victim", "h264_25fps_audio.mp4")
        b = rig.create("bystander", "h264_2997.mp4")
        rig.wait_status(a["id"], "RUNNING")
        rig.wait_status(b["id"], "RUNNING")
        pid_b = rig.stream(b["id"])["pid"]
        t0 = time.monotonic()
        os.unlink(rig.video_dir / "h264_25fps_audio.mp4")
        d = rig.wait_status(a["id"], "ERROR", 10)
        print(f"\nEC-07 delete detected in {time.monotonic() - t0:.1f}s: {d['last_error']}")
        assert "deleted" in d["last_error"] and d["desired_state"] == "STOPPED" and d["pid"] is None
        assert rig.client.get("/api/streams").status_code == 200  # page still loads
        time.sleep(2)
        db = rig.stream(b["id"])
        assert db["status"] == "RUNNING" and db["pid"] == pid_b  # unrelated stream untouched
        # restart refuses with a clear message instead of starting a dead stream
        r = rig.client.post(f"/api/streams/{a['id']}/restart")
        assert r.status_code == 422 and "cannot be read" in r.json()["detail"]


def test_ec07_source_replaced_while_streaming(rig_factory, fixtures_dir):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        a = rig.create("rep", "h264_25fps_audio.mp4")
        rig.wait_status(a["id"], "RUNNING")
        tmp = rig.video_dir / ".incoming.mp4"
        shutil.copy(fixtures_dir / "h264_2997.mp4", tmp)
        os.replace(tmp, rig.video_dir / "h264_25fps_audio.mp4")  # atomic replace, new inode
        d = rig.wait_status(a["id"], "ERROR", 10)
        assert "replaced" in d["last_error"]
        # explicit restart re-validates against the new file and runs again
        r = rig.client.post(f"/api/streams/{a['id']}/restart?wait=true")
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"


def test_ec07_source_renamed_then_restart_follows_the_file(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        a = rig.create("ren", "h264_25fps_audio.mp4")
        rig.wait_status(a["id"], "RUNNING")
        os.rename(rig.video_dir / "h264_25fps_audio.mp4", rig.video_dir / "renamed on server.mp4")
        d = rig.wait_status(a["id"], "ERROR", 10)
        assert "deleted or moved" in d["last_error"]
        r = rig.client.post(f"/api/streams/{a['id']}/restart?wait=true")  # no manual refresh first
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"
        assert r.json()["source_rel_path"] == "renamed on server.mp4"


def test_ec07_source_truncated_while_streaming(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        a = rig.create("trunc", "h264_25fps_audio.mp4")
        rig.wait_status(a["id"], "RUNNING")
        with open(rig.video_dir / "h264_25fps_audio.mp4", "r+b") as f:
            f.truncate(10_000)
        d = rig.wait_status(a["id"], "ERROR", 10)
        assert "size changed" in d["last_error"]


# ---------------------------------------------------------------- EC-09
def test_ec09_mediamtx_killed_detected_restarted_and_republished(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4", "h264_2997.mp4"]) as rig:
        ids = [rig.create(n, f)["id"] for n, f in (("m1", "h264_25fps_audio.mp4"), ("m2", "h264_2997.mp4"))]
        for i in ids:
            rig.wait_status(i, "RUNNING")
        mtx_pid = rig.client.get("/api/mediamtx/status").json()["managed_pid"]
        t_kill = time.monotonic()
        os.kill(mtx_pid, signal.SIGKILL)
        st = wait_for(lambda: (j := rig.client.get("/api/mediamtx/status").json())["state"] == "DOWN" and j,
                      10, interval=0.1, desc="DOWN detected")
        detect = time.monotonic() - t_kill
        wait_for(lambda: rig.client.get("/api/mediamtx/status").json()["state"] == "UP", 30, desc="UP again")
        up = time.monotonic() - t_kill
        for i in ids:
            rig.wait_status(i, "RUNNING", 30)
        all_back = time.monotonic() - t_kill
        new_pid = rig.client.get("/api/mediamtx/status").json()["managed_pid"]
        print(f"\nEC-09 detected DOWN after {detect:.1f}s (reported {st['last_detection_s']:.1f}s), "
              f"MediaMTX UP after {up:.1f}s, all streams RUNNING after {all_back:.1f}s")
        assert detect < 10 and new_pid != mtx_pid
        for n in ("m1", "m2"):
            assert len(rtsp_packets(rig.url(n), 2)[0]) > 20  # RTSP paths really restored
        assert len(ffmpeg_publishers(rig.run_dir_marker())) == 2
        kinds = [e["kind"] for e in rig.client.get("/api/events?limit=100").json()]
        assert "mediamtx.down" in kinds and "mediamtx.up" in kinds


def test_ec09_fast_mediamtx_restart_before_down_is_detected(rig_factory):
    """Regression (found under systemd): MediaMTX restarted within ~1 s, so the
    monitor never declared DOWN; publishers must still be republished."""
    with rig_factory(["h264_25fps_audio.mp4"], health_interval_s=5.0, mediamtx_auto_recover=False) as rig:
        s = rig.create("fast", "h264_25fps_audio.mp4")
        rig.wait_status(s["id"], "RUNNING")
        old_pid = rig.stream(s["id"])["pid"]
        ctl = rig.ctx.mediamtx.controller
        os.kill(ctl.pid, signal.SIGKILL)
        time.sleep(0.3)
        rig.client.portal.call(ctl.start)  # like systemd Restart=always, before any DOWN is declared
        d = rig.wait_status(s["id"], "RUNNING", 20)
        assert d["pid"] != old_pid
        assert rig.client.get("/api/mediamtx/status").json()["recoveries"] == 0  # no DOWN was ever declared
        assert len(rtsp_packets(rig.url("fast"), 2)[0]) > 20


# ---------------------------------------------------------------- EC-10 (backend side)
def test_ec10_restart_restores_running_streams_only(rig_factory, tmp_path):
    base = tmp_path / "persist"
    vids = ["h264_25fps_audio.mp4", "h264_2997.mp4"]
    with rig_factory(vids, base=base) as rig:
        run_ids = [rig.create(f"run{i}", vids[i % 2], start=f"00:00:0{i + 2}")["id"] for i in range(4)]
        stopped = rig.create("stopped1", vids[0])["id"]
        once = rig.create("once1", vids[0], start="00:00:17", mode="once")["id"]
        for i in run_ids + [stopped]:
            rig.wait_status(i, "RUNNING")
        rig.client.post(f"/api/streams/{stopped}/stop")
        rig.wait_status(once, "COMPLETED", 20)
        before = {i: rig.stream(i)["effective_start_s"] for i in run_ids}
    # backend gone ("reboot"): no publishers may survive it
    assert ffmpeg_publishers(str(base / "data" / "run" / "streams")) == []
    t0 = time.monotonic()
    with rig_factory(vids, base=base) as rig2:
        for i in run_ids:
            d = rig2.wait_status(i, "RUNNING", 30)
            assert d["effective_start_s"] == before[i]  # same start position
        elapsed = time.monotonic() - t0
        assert rig2.stream(stopped)["status"] == "STOPPED"
        assert rig2.stream(once)["status"] == "COMPLETED"
        rep = wait_for(lambda: (j := rig2.client.get("/api/system/recovery").json())["state"] == "done" and j, 30,
                       desc="recovery report")
        print(f"\nEC-10 backend restart: 4/4 streams RUNNING {elapsed:.1f}s after app start; "
              f"report duration {rep['recovery_duration_s']}s")
        assert rep["streams_running"] == 4 and rep["streams_to_recover"] == 4
        assert len(ffmpeg_publishers(rig2.run_dir_marker())) == 4


def test_orphaned_publishers_are_reaped(rig_factory, tmp_path):
    base = tmp_path / "orphan"
    with rig_factory(["h264_25fps_audio.mp4"], base=base) as rig:
        s = rig.create("orph", "h264_25fps_audio.mp4")
        rig.wait_status(s["id"], "RUNNING")
        cmd = rig.stream(s["id"])["ffmpeg_command"]
    # simulate a backend crash that left a publisher behind (pointing at nothing)
    orphan = subprocess.Popen(cmd[:cmd.index("-f", cmd.index("-c:v"))] + ["-f", "null", "-"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        with rig_factory(["h264_25fps_audio.mp4"], base=base) as rig2:
            rig2.wait_status(s["id"], "RUNNING", 30)
            assert orphan.poll() is not None, "orphan publisher was not reaped"
            assert len(ffmpeg_publishers(rig2.run_dir_marker())) == 1
    finally:
        if orphan.poll() is None:
            orphan.kill()


# ---------------------------------------------------------------- EC-11
def test_ec11_concurrent_operations_never_duplicate_publishers(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        s = rig.create("busy", "h264_25fps_audio.mp4")
        rig.wait_status(s["id"], "RUNNING")
        ops = ["restart", "stop", "start", "restart", "restart", "stop", "start", "restart"] * 2
        codes = []
        max_procs = 0
        stop_watch = threading.Event()

        def watch():
            nonlocal max_procs
            while not stop_watch.is_set():
                max_procs = max(max_procs, len(ffmpeg_publishers(rig.run_dir_marker())))
                time.sleep(0.02)

        def user(op):
            codes.append((op, rig.client.post(f"/api/streams/{s['id']}/{op}").status_code))

        w = threading.Thread(target=watch)
        w.start()
        threads = [threading.Thread(target=user, args=(op,)) for op in ops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rig.client.post(f"/api/streams/{s['id']}/start")
        d = rig.wait_status(s["id"], "RUNNING", 20)
        time.sleep(1)
        stop_watch.set()
        w.join()
        print(f"\nEC-11 {len(ops)} concurrent ops: codes={sorted(set(codes))} max publishers seen={max_procs}")
        assert max_procs <= 1, "two publishers existed for one stream"
        assert all(code in (200, 409) for _, code in codes)
        assert ffmpeg_publishers(rig.run_dir_marker()) == [d["pid"]]
        # a stale If-Match from a second user is refused
        r = rig.client.post(f"/api/streams/{s['id']}/stop", headers={"If-Match": "1"})
        assert r.status_code == 409


# ---------------------------------------------------------------- EC-12
def test_ec12_reader_connected_before_publisher_gets_the_stream(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        s = rig.create("early", "h264_25fps_audio.mp4", start_now=False)
        result = {}

        def reader():
            t = time.monotonic()
            try:
                pkts, _, info = rtsp_packets(rig.url("early"), 3, timeout=90)
                result.update(ok=True, n=len(pkts), codec=info["streams"][0]["codec_name"],
                              waited=time.monotonic() - t)
            except Exception as exc:  # noqa: BLE001
                result.update(ok=False, err=str(exc))

        th = threading.Thread(target=reader)
        th.start()
        time.sleep(5)  # the "edge" is waiting on the URL; nothing is published yet
        assert th.is_alive(), f"reader gave up before the stream started: {result}"
        rig.client.post(f"/api/streams/{s['id']}/start")
        th.join(60)
        print(f"\nEC-12 early reader: {result}")
        assert result.get("ok") and result["n"] > 20 and result["codec"] == "h264"


# ---------------------------------------------------------------- EC-13
def test_ec13_two_hour_file_seek_is_fast(rig_factory):
    with rig_factory(["long_2h.mp4"]) as rig:
        t0 = time.monotonic()
        s = rig.create("late", "long_2h.mp4", start="01:45:00", end="01:50:00")
        created = time.monotonic() - t0
        d = rig.wait_status(s["id"], "RUNNING", 15)
        total = time.monotonic() - t0
        assert d["effective_start_s"] == pytest.approx(6300, abs=2.1)
        assert d["effective_end_s"] == pytest.approx(6600, abs=2.1)
        time.sleep(2)
        d = rig.stream(s["id"])
        pos = d["runtime"]["playback_position_s"]
        rss = psutil.Process(d["pid"]).memory_info().rss
        print(f"\nEC-13 2h file: validate+create {created:.2f}s, RUNNING after {total:.2f}s, "
              f"position {pos:.1f}s, ffmpeg RSS {rss / 1e6:.1f} MB")
        assert total < 10 and 6300 <= pos < 6320
        assert rss < 150e6
