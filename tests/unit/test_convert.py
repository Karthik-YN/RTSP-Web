"""Library "Convert to H.264": playability rules, and a real FFmpeg conversion through the API."""

import os
import subprocess
import time
from types import SimpleNamespace

import pytest

from app.services.converter import browser_playback, convert_reasons, target_rel


def _v(**kw):
    base = dict(status="READY", rel_path="a.mp4", video_codec="h264", pix_fmt="yuv420p", bit_depth=8,
                compat_mode="copy", keyframe_interval_s=2.0)
    return SimpleNamespace(**{**base, **kw})


@pytest.mark.parametrize("kw, playable", [
    ({}, True),
    ({"rel_path": "a.mov"}, True),
    ({"rel_path": "a.webm", "video_codec": "vp9"}, True),
    ({"rel_path": "a.mp4", "video_codec": "av1"}, True),
    ({"video_codec": "mpeg4"}, False),              # MPEG-4 Part 2 (DivX/Xvid): the reported case
    ({"video_codec": "hevc"}, False),
    ({"video_codec": "mpeg2video", "rel_path": "a.mpg"}, False),
    ({"bit_depth": 10, "pix_fmt": "yuv420p10le"}, False),
    ({"pix_fmt": "yuv422p"}, False),
    ({"rel_path": "a.mkv"}, False),                 # H.264 in MKV: not reliable in browsers
    ({"rel_path": "a.avi"}, False),
    ({"rel_path": "a.webm"}, False),                # H.264 is not valid in WebM
    ({"status": "MISSING"}, False),
])
def test_browser_playback(kw, playable):
    ok, why = browser_playback(_v(**kw))
    assert ok is playable
    assert (why is None) == (playable or kw.get("status") == "MISSING")


def test_convert_reasons():
    assert convert_reasons(_v()) == []
    assert any("MPEG4" in r for r in convert_reasons(_v(video_codec="mpeg4", compat_mode="transcode")))
    assert any("live transcoding" in r for r in convert_reasons(_v(video_codec="hevc", compat_mode="transcode")))
    assert any("Keyframes only every 10.4 s" in r for r in convert_reasons(_v(keyframe_interval_s=10.4)))
    assert convert_reasons(_v(compat_mode="reject")) == []
    assert convert_reasons(_v(rel_path="cam (h264).mp4", keyframe_interval_s=10)) == []  # already a copy
    bf = {"streams": [{"codec_type": "video", "has_b_frames": 2}]}
    assert any("B-frames" in r for r in convert_reasons(_v(probe_json=bf)))  # real cameras send none
    assert not any("B-frames" in r for r in convert_reasons(_v(probe_json={"streams": [{"codec_type": "video", "has_b_frames": 0}]})))
    assert target_rel("sub/old cam.avi") == "sub/old cam (h264).mp4"


def _make_mpeg4(ffmpeg: str, path, seconds: int) -> None:
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=640x360:rate=25:duration={seconds}", "-c:v", "mpeg4", "-g", "250",
                    str(path)], check=True)
    old = time.time() - 3600  # not "still being copied"
    os.utime(path, (old, old))


def _wait_job(rig, job_id, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = next(j for j in rig.client.get("/api/videos/conversions").json() if j["id"] == job_id)
        if job["state"] not in ("queued", "running"):
            return job
        time.sleep(0.3)
    raise AssertionError(f"conversion {job_id} still {job['state']} after {timeout}s")


def test_convert_mpeg4_to_h264(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        src = rig.video_dir / "old cam.mp4"
        _make_mpeg4(rig.settings.ffmpeg_bin, src, 6)
        rig.client.post("/api/videos/refresh")
        v = next(x for x in rig.client.get("/api/videos").json() if x["filename"] == "old cam.mp4")
        assert v["video_codec"] == "mpeg4" and v["browser_playable"] is False and "MPEG4" in v["browser_note"]
        assert v["convert_reasons"] and v["converting"] is None

        r = rig.client.post(f"/api/videos/{v['id']}/convert")
        assert r.status_code == 202, r.text
        assert rig.client.post(f"/api/videos/{v['id']}/convert").status_code == 409  # already running
        job = _wait_job(rig, r.json()["id"])
        assert job["state"] == "done", job
        assert job["target"] == "old cam (h264).mp4" and job["progress"] == 1.0

        out = rig.client.get(f"/api/videos/{job['result_video_id']}").json()
        assert out["filename"] == "old cam (h264).mp4" and out["video_codec"] == "h264"
        assert out["pix_fmt"] == "yuv420p" and out["compat_mode"] == "copy"
        assert out["browser_playable"] is True and out["convert_reasons"] == []
        assert out["keyframe_interval_s"] <= 2.1 and out["duration_s"] == pytest.approx(6, abs=0.2)
        assert out["status"] == "READY"
        assert src.exists()  # the original is kept
        assert not list((rig.video_dir / ".uploads").glob("*.part"))  # temp file gone

        # converting again: the copy already exists; converting the copy: refused
        assert rig.client.post(f"/api/videos/{v['id']}/convert").status_code == 409
        assert rig.client.post(f"/api/videos/{out['id']}/convert").status_code == 409
        # a later scan keeps both, nothing reported missing
        s = rig.client.post("/api/videos/refresh").json()
        assert s["missing"] == 0
        events = [e["kind"] for e in rig.client.get("/api/events?limit=50").json()]
        assert "video.converted" in events


def test_cancel_conversion_leaves_nothing_behind(rig_factory):
    with rig_factory([], mediamtx=False, convert_preset="veryslow") as rig:
        src = rig.video_dir / "long.mp4"
        _make_mpeg4(rig.settings.ffmpeg_bin, src, 120)
        rig.client.post("/api/videos/refresh")
        vid = rig.video_id("long.mp4")
        job = rig.client.post(f"/api/videos/{vid}/convert").json()
        time.sleep(1.0)
        r = rig.client.delete(f"/api/videos/conversions/{job['id']}")
        assert r.status_code == 200 and r.json()["state"] == "cancelled"
        assert not (rig.video_dir / "long (h264).mp4").exists()
        assert not list((rig.video_dir / ".uploads").glob("*.part"))
        assert rig.client.delete("/api/videos/conversions/nope").status_code == 404

