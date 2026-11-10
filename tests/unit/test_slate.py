"""Lead-in slates and the lead-in fields of the API (no MediaMTX needed)."""

import asyncio
import json
import subprocess

import pytest

from app.config import Settings
from app.services.slate_service import (SlateService, SlateSpec, fps_text_of, profile_of, slate_command,
                                        slate_graph)
from helpers import have_binaries


def test_fps_and_profile_mapping():
    assert fps_text_of("30000/1001", 29.97) == "30000/1001"
    assert fps_text_of(None, 25.0) == "25/1"
    assert fps_text_of("0/0", 29.97002997) == "30000/1001"
    assert profile_of("Constrained Baseline") == "baseline"
    assert profile_of("Main") == "main"
    assert profile_of("High 10") == "high" and profile_of(None) == "high"


def test_graph_and_command():
    spec = SlateSpec("cam-1", 1920, 1080, "25/1", "main")
    g = slate_graph(spec, drawtext=True)
    assert "text='cam-1'" in g and "pts\\:hms" in g and "1920x1080" in g
    band = slate_graph(spec, drawtext=False)
    assert "drawtext" not in band and "drawbox" in band and "testsrc" in band
    assert slate_graph(SlateSpec("cam-2", 1920, 1080, "25/1", "main"), False) != band  # colour per name
    cmd = slate_command(spec, __import__("pathlib").Path("/tmp/x.mp4"), 10, True, Settings(_env_file=None))
    assert cmd[cmd.index("-profile:v") + 1] == "main" and cmd[cmd.index("-g") + 1] == "25"
    assert "-vf" not in cmd
    scaled = SlateSpec("cam-1", 3840, 2160, "25/1", "high", "scale=w=1920:h=1080")
    cmd = slate_command(scaled, __import__("pathlib").Path("/tmp/x.mp4"), 10, False, Settings(_env_file=None))
    assert cmd[cmd.index("-vf") + 1] == "scale=w=1920:h=1080"
    assert spec.key(10) != SlateSpec("cam-1b", 1920, 1080, "25/1", "main").key(10)  # rename -> new slate


@pytest.mark.skipif(not have_binaries("ffmpeg", "ffprobe"), reason="ffmpeg not installed")
@pytest.mark.parametrize("drawtext", [True, False])
def test_render_matches_the_stream_and_is_world_readable(tmp_path, drawtext):
    s = Settings(_env_file=None, data_dir=tmp_path, slate_seconds=2)
    svc = SlateService(s)
    if not drawtext:
        svc._drawtext = False
    elif not svc._has_drawtext():
        pytest.skip("this FFmpeg has no drawtext")
    spec = SlateSpec("lead-cam", 640, 360, "30000/1001", "main")
    out = asyncio.run(svc.ensure(7, spec))
    assert out.parent == tmp_path / "slates" and oct(out.stat().st_mode & 0o777) == "0o644"
    info = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,profile,width,height,r_frame_rate,pix_fmt", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)["streams"][0]
    assert (info["codec_name"], info["profile"], info["width"], info["height"], info["r_frame_rate"],
            info["pix_fmt"]) == ("h264", "Main", 640, 360, "30000/1001", "yuv420p")
    # cached: same file, not rendered again
    mtime = out.stat().st_mtime_ns
    assert asyncio.run(svc.ensure(7, spec)) == out and out.stat().st_mtime_ns == mtime
    # a new configuration replaces the old slate of that stream only
    other = asyncio.run(svc.ensure(8, spec))
    new = asyncio.run(svc.ensure(7, SlateSpec("lead-cam2", 640, 360, "30000/1001", "main")))
    assert not out.exists() and new.exists() and other.exists()
    svc.prune(7)
    assert not new.exists() and other.exists()


def test_lead_in_api_fields(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"], mediamtx=False) as rig:
        vid = rig.video_id("h264_25fps_audio.mp4")
        c = rig.client
        assert c.get("/api/config").json()["default_lead_in_mode"] == "none"  # test rigs; production: fixed
        bad = c.post("/api/streams", json={"name": "x", "video_id": vid, "lead_in_s": 121, "start_now": False})
        assert bad.status_code == 422
        bad = c.post("/api/streams", json={"name": "x", "video_id": vid, "lead_in_mode": "later", "start_now": False})
        assert bad.status_code == 422
        s = c.post("/api/streams", json={"name": "lh", "video_id": vid, "lead_in_mode": "hold", "lead_in_s": 3,
                                         "hold_max_s": 600, "start_now": False}).json()
        assert (s["lead_in_mode"], s["lead_in_s"], s["hold_max_s"]) == ("hold", 3, 600)
        d = c.post("/api/streams", json={"name": "ld", "video_id": vid, "start_now": False}).json()
        assert d["lead_in_mode"] == "none"
        r = c.patch(f"/api/streams/{s['id']}", json={"lead_in_mode": "fixed", "lead_in_s": 8, "clear_hold_max": True})
        assert r.status_code == 200, r.text
        assert (r.json()["lead_in_mode"], r.json()["lead_in_s"], r.json()["hold_max_s"]) == ("fixed", 8, None)
        assert c.post(f"/api/streams/{s['id']}/go").status_code == 409  # not in a lead-in


def test_production_default_is_a_five_second_lead_in():
    s = Settings(_env_file=None)
    assert (s.default_lead_in_mode, s.default_lead_in_s, s.start_spacing_s) == ("fixed", 5.0, 2.0)
