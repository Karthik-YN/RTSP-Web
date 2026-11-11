"""Lead-in: the stream's slate on its URL before the video (fixed N s, or hold
until a viewer connects + N s), switching in the same reader session.

Readers' decoded frames are matched against the source file: a frame that is
not in the source is a slate frame. This checks what an edge actually
receives, in the order it receives it.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import subprocess
import time

import httpx
import pytest

from helpers import ffmpeg_publishers, wait_for
from test_start_spacing import FPS, VIDEO, source_index

pytestmark = pytest.mark.integration

START = "00:00:04"  # keyframe at 4 s -> source frame 100
START_FRAME = 100


class Reader:
    """An RTSP reader (ffmpeg) that writes every decoded frame's md5."""

    def __init__(self, url: str, seconds: float, out):
        self.out = out
        self.proc = subprocess.Popen(
            ["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i", url, "-t", str(seconds),
             "-map", "0:v", "-f", "framemd5", "-y", str(out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def finish(self, timeout: float = 90) -> tuple[list[tuple[float, str]], str, int]:
        _, err = self.proc.communicate(timeout=timeout)
        return _parse(self.out), err or "", self.proc.returncode


def _parse(path) -> list[tuple[float, str]]:
    tb = 1 / 90000
    frames = []
    for ln in open(path).read().splitlines():
        if ln.startswith("#tb 0:"):
            num, den = ln.split(":", 1)[1].strip().split("/")
            tb = int(num) / int(den)
        if ln.startswith("#") or not ln.strip():
            continue
        p = [x.strip() for x in ln.split(",")]
        frames.append((int(p[2]) * tb, p[5]))
    return frames


def segments(frames, idx) -> list[dict]:
    """Runs of slate ('S') and video ('V') frames, with video frame numbers."""
    segs: list[dict] = []
    for pts, md5 in frames:
        kind = "V" if md5 in idx else "S"
        if not segs or segs[-1]["kind"] != kind:
            segs.append({"kind": kind, "n": 0, "pts0": pts, "first": idx.get(md5), "frames": []})
        segs[-1]["n"] += 1
        segs[-1]["pts1"] = pts
        if kind == "V":
            segs[-1]["frames"].append(idx[md5])
    return segs


def check_video_run(seg: dict, first: int, loop_len: int | None = None, cut_tail: int = 0) -> None:
    """A video run starts at ``first`` and has no lost, repeated or reordered frame.

    ``cut_tail``: the run ended because the publisher was killed; frames in
    flight at that moment are lost, and with B-frames the decoder then shows
    the last few out of order. Those last frames only have to increase."""
    fr = seg["frames"]
    assert fr[0] == first, f"video starts at frame {fr[0]}, expected {first}"
    tail = fr[len(fr) - cut_tail - 1:] if cut_tail else []
    if cut_tail:
        fr = fr[:len(fr) - cut_tail]
        assert all(b > a for a, b in zip(tail, tail[1:])), f"frames before the kill went backwards: {tail}"
    for a, b in zip(fr, fr[1:]):
        ok = b == a + 1 or (loop_len is not None and b == first and a >= first)
        assert ok, f"video frame {a} followed by {b}"


def describe(segs) -> str:
    return " | ".join(f"{s['kind']}x{s['n']} ({s['pts1'] - s['pts0'] + 1 / FPS:.2f}s)" for s in segs)


def test_fixed_lead_in_shows_slate_then_video_from_the_start_frame(rig_factory, tmp_path):
    idx = source_index(VIDEO)
    with rig_factory([VIDEO], start_spacing_s=2.0) as rig:
        s = rig.create("lead-fixed", VIDEO, start=START, lead_in_mode="fixed", lead_in_s=5)
        assert s["lead_in_mode"] == "fixed" and s["lead_in_s"] == 5
        sid = s["id"]
        st = rig.wait_status(sid, "LEAD_IN", 30)
        t_lead = time.monotonic()
        lead = st["runtime"]["lead_in"]
        assert lead["mode"] == "fixed" and lead["video_in_s"] is not None
        assert st["runtime"]["slate_on_url"] is True
        assert ffmpeg_publishers(rig.run_dir_marker()) == []  # no video yet
        reader = Reader(rig.url("lead-fixed"), 10, tmp_path / "r.framemd5")
        rig.wait_status(sid, "RUNNING", 20)
        took = time.monotonic() - t_lead
        frames, err, rc = reader.finish()
        segs = segments(frames, idx)
        print(f"\nfixed lead-in: RUNNING {took:.2f}s after LEAD_IN; reader saw {describe(segs)}")
        assert rc == 0, err
        assert took >= 4.5
        assert [x["kind"] for x in segs] == ["S", "V"], describe(segs)
        assert segs[0]["n"] >= 2 * FPS  # the reader joined during the lead-in and saw slate frames
        check_video_run(segs[1], START_FRAME)
        assert "decod" not in err.lower()


def test_hold_waits_for_a_viewer_then_lead_in(rig_factory, tmp_path):
    idx = source_index(VIDEO)
    with rig_factory([VIDEO], start_spacing_s=2.0) as rig:
        sid = rig.create("lead-hold", VIDEO, start=START, lead_in_mode="hold", lead_in_s=3)["id"]
        st = rig.wait_status(sid, "WAITING_FOR_VIEWER", 30)
        assert st["runtime"]["lead_in"]["phase"] == "waiting"
        # the dashboard preview (HLS) is not a viewer
        rig.client.get(f"/api/streams/{sid}/live/index.m3u8")
        time.sleep(4)
        st = rig.stream(sid)
        assert st["status"] == "WAITING_FOR_VIEWER", st["status"]
        assert ffmpeg_publishers(rig.run_dir_marker()) == []
        t0 = time.monotonic()
        reader = Reader(rig.url("lead-hold"), 12, tmp_path / "h.framemd5")
        st = rig.wait_status(sid, "LEAD_IN", 10)
        seen = time.monotonic() - t0
        assert st["runtime"]["lead_in"]["viewer_seen_at"]
        rig.wait_status(sid, "RUNNING", 20)
        video_after = time.monotonic() - t0
        frames, err, rc = reader.finish()
        segs = segments(frames, idx)
        print(f"\nhold: viewer seen {seen:.2f}s after connecting, RUNNING {video_after:.2f}s; reader saw {describe(segs)}")
        assert rc == 0, err
        assert video_after >= 3.0
        assert [x["kind"] for x in segs] == ["S", "V"], describe(segs)
        # lead_in_s of slate after joining, minus the wait for the reader's first picture
        # (up to one slate keyframe interval, 1 s): the viewer is seen within ~0.3 s of connecting
        assert segs[0]["pts1"] - segs[0]["pts0"] >= 3 - 1.0 - 0.1
        check_video_run(segs[1], START_FRAME)
        kinds = [e["kind"] for e in rig.client.get(f"/api/events?stream_id={sid}").json()]
        assert "stream.viewer_connected" in kinds


def test_hold_sees_a_brief_reader(rig_factory):
    """A reader that only probes (connects for well under the 2 s poll) still counts:
    an edge may probe the camera first and connect for real a moment later."""
    with rig_factory([VIDEO], start_spacing_s=0) as rig:
        mtx = rig.ctx.mediamtx
        polled = mtx.path

        def no_polled_readers(name):  # the 2 s path poll "missed" the probe: only the fresh check can see it
            p = polled(name)
            return dataclasses.replace(p, readers=[]) if p is not None and name == "probe-hold" else p

        mtx.path = no_polled_readers
        sid = rig.create("probe-hold", VIDEO, lead_in_mode="hold", lead_in_s=2)["id"]
        rig.wait_status(sid, "WAITING_FOR_VIEWER", 30)
        t0 = time.monotonic()
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
                            "-show_entries", "stream=codec_name", "-of", "csv=p=0", rig.url("probe-hold")],
                           capture_output=True, text=True, timeout=30)
        probe_s = time.monotonic() - t0
        assert r.returncode == 0 and r.stdout.strip() == "h264", r.stderr
        st = rig.wait_status(sid, ("LEAD_IN", "RUNNING"), 5)
        print(f"\nbrief reader: ffprobe connected for {probe_s:.2f}s; stream {st['status']}")
        rig.wait_status(sid, "RUNNING", 15)
        kinds = [e["kind"] for e in rig.client.get(f"/api/events?stream_id={sid}").json()]
        assert "stream.viewer_connected" in kinds  # not the hold timeout or "start now"


def test_start_video_now_and_hold_timeout(rig_factory):
    with rig_factory([VIDEO], start_spacing_s=0) as rig:
        a = rig.create("go-now", VIDEO, lead_in_mode="hold")["id"]
        rig.wait_status(a, "WAITING_FOR_VIEWER", 30)
        r = rig.client.post(f"/api/streams/{a}/go")
        assert r.status_code == 200, r.text
        rig.wait_status(a, "RUNNING", 15)
        assert rig.client.post(f"/api/streams/{a}/go").status_code == 409  # not in its lead-in any more
        b = rig.create("hold-timeout", VIDEO, lead_in_mode="hold", hold_max_s=2)["id"]
        rig.wait_status(b, "WAITING_FOR_VIEWER", 30)
        t0 = time.monotonic()
        rig.wait_status(b, "RUNNING", 15)
        assert 1.5 <= time.monotonic() - t0 < 8


def test_restart_and_crash_keep_the_reader_connected(rig_factory, tmp_path):
    idx = source_index(VIDEO)
    with rig_factory([VIDEO], start_spacing_s=0) as rig:
        sid = rig.create("keep", VIDEO, start=START, lead_in_mode="fixed", lead_in_s=2)["id"]
        rig.wait_status(sid, "RUNNING", 30)
        reader = Reader(rig.url("keep"), 22, tmp_path / "k.framemd5")
        time.sleep(3)
        # 1. restart from the UI: slate (lead-in again), then video; no disconnect
        assert rig.client.post(f"/api/streams/{sid}/restart").status_code == 200
        rig.wait_status(sid, "LEAD_IN", 10)
        rig.wait_status(sid, "RUNNING", 15)
        time.sleep(3)
        # 2. publisher crash: slate while the retry starts the video again, no lead-in
        pid = rig.stream(sid)["pid"]
        ff = ffmpeg_publishers(rig.run_dir_marker())
        assert ff
        for p in ff:
            os.kill(p, signal.SIGKILL)
        t0 = time.monotonic()
        statuses = set()

        def back():
            s = rig.stream(sid)
            statuses.add(s["status"])
            return s["status"] == "RUNNING" and s["pid"] != pid
        wait_for(back, 20, interval=0.1, desc="publisher back")
        assert "LEAD_IN" not in statuses and "WAITING_FOR_VIEWER" not in statuses
        print(f"\ncrash: video back after {time.monotonic() - t0:.2f}s (statuses {sorted(statuses)})")
        frames, err, rc = reader.finish()
        segs = segments(frames, idx)
        print(f"reader (one session) saw {describe(segs)}")
        assert rc == 0, err
        kinds = [x["kind"] for x in segs]
        assert kinds[:5] == ["V", "S", "V", "S", "V"], describe(segs)
        check_video_run(segs[2], START_FRAME, cut_tail=4)  # this run ends with the SIGKILL
        check_video_run(segs[4], START_FRAME)


def test_mediamtx_restart_restores_the_slate_and_stop_delete_clear_it(rig_factory):
    with rig_factory([VIDEO], start_spacing_s=0) as rig:
        sid = rig.create("persist", VIDEO, lead_in_mode="hold")["id"]
        rig.wait_status(sid, "WAITING_FOR_VIEWER", 30)
        api = rig.settings.mediamtx_api_url

        def configured():
            try:
                return {it["name"] for it in httpx.get(f"{api}/v3/config/paths/list").json()["items"]}
            except httpx.HTTPError:  # MediaMTX restarting
                return set()
        assert "persist" in configured()
        rig.ctx.mediamtx.controller.proc.kill()  # MediaMTX dies; the backend restarts it
        wait_for(lambda: rig.client.get("/api/mediamtx/status").json()["state"] == "UP"
                 and "persist" in configured(), 40, desc="slate path restored")
        assert rig.stream(sid)["status"] == "WAITING_FOR_VIEWER"
        probe = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-show_entries",
                                "stream=width", "-of", "csv=p=0", rig.url("persist")],
                               capture_output=True, text=True, timeout=30)
        assert probe.returncode == 0 and probe.stdout.strip(), probe.stderr
        # stop: the URL fails again, exactly like a stopped stream without lead-in
        rig.client.post(f"/api/streams/{sid}/stop")
        assert "persist" not in configured()
        assert rig.stream(sid)["runtime"]["slate_on_url"] is False
        slates = list(rig.settings.slate_path.glob(f"{sid}-*.mp4"))
        assert slates and oct(slates[0].stat().st_mode & 0o777) == "0o644"
        assert rig.client.delete(f"/api/streams/{sid}").status_code in (200, 204)
        assert not list(rig.settings.slate_path.glob(f"{sid}-*.mp4"))


def test_slates_differ_between_cameras_and_videos_stay_spaced(rig_factory, tmp_path):
    idx = source_index(VIDEO)
    with rig_factory([VIDEO], start_spacing_s=2.0) as rig:
        ids = [rig.create(f"cam-{i}", VIDEO, lead_in_mode="hold")["id"] for i in range(1, 4)]
        for i in ids:
            rig.wait_status(i, "WAITING_FOR_VIEWER", 30)
        readers = [Reader(rig.url(f"cam-{i}"), 3, tmp_path / f"s{i}.framemd5") for i in range(1, 4)]
        slate_md5 = []
        for r in readers:
            frames, err, rc = r.finish()
            assert rc == 0, err
            slate_md5.append({m for _, m in frames if m not in idx})
        for a in range(3):
            assert slate_md5[a]
            for b in range(a + 1, 3):
                assert not slate_md5[a] & slate_md5[b], "two cameras sent an identical slate frame"
        for i in ids:
            rig.wait_status(i, "RUNNING", 30)
        from test_start_spacing import assert_apart, content_positions
        assert_apart(content_positions(rig, [f"cam-{i}" for i in range(1, 4)], VIDEO), 20.0, 1.85)


def test_transcoded_stream_lead_in_switches_without_decode_errors(rig_factory, tmp_path):
    with rig_factory(["hevc.mp4"], start_spacing_s=0) as rig:
        sid = rig.create("hevc-lead", "hevc.mp4", lead_in_mode="fixed", lead_in_s=3)["id"]
        st = rig.wait_status(sid, "LEAD_IN", 60)
        assert st["output_mode"] == "transcode"
        out = tmp_path / "t.framemd5"
        p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i", rig.url("hevc-lead"),
                            "-t", "8", "-map", "0:v", "-f", "framemd5", "-y", str(out)],
                           capture_output=True, text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        assert rig.stream(sid)["status"] == "RUNNING"
        assert "error" not in p.stderr.lower()
        assert len(_parse(out)) >= 7 * FPS


def test_reboot_recovery_runs_the_lead_in_again(rig_factory, tmp_path):
    base = tmp_path / "persistlead"
    with rig_factory([VIDEO], base=base, start_spacing_s=0) as rig:
        hold = rig.create("rb-hold", VIDEO, lead_in_mode="hold")["id"]
        fixed = rig.create("rb-fixed", VIDEO, lead_in_mode="fixed", lead_in_s=2)["id"]
        rig.wait_status(hold, "WAITING_FOR_VIEWER", 30)
        rig.wait_status(fixed, "RUNNING", 30)
    with rig_factory([], base=base, start_spacing_s=0) as rig2:
        rig2.wait_status(hold, "WAITING_FOR_VIEWER", 40)
        rig2.wait_status(fixed, "RUNNING", 40)
        rep = wait_for(lambda: (j := rig2.client.get("/api/system/recovery").json())["state"] == "done" and j, 60,
                       desc="recovery report")
        assert rep["streams_running"] == 2, rep
        assert ffmpeg_publishers(rig2.run_dir_marker()) and len(ffmpeg_publishers(rig2.run_dir_marker())) == 1
