"""16:9 rule, standard 720p output, and the 720p / 16:9 copies."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from app.config import Settings
from app.services.ffmpeg_manager import PublishPlan, build_command
from app.services.shape import display_size, fit_filter, parse_size, shape_of

from test_convert import _wait_job


@pytest.mark.parametrize("w, h, shape", [
    (1920, 1080, "16:9"), (1280, 720, "16:9"), (1366, 768, "16:9"), (854, 480, "16:9"),
    (1080, 1920, "portrait"), (768, 544, "4:3"), (640, 480, "4:3"), (720, 480, "4:3"),
    (1080, 1080, "square"), (2560, 1080, "other"),
])
def test_shape_of(w, h, shape):
    assert shape_of(w, h) == shape


def test_display_size_applies_rotation_and_pixel_shape():
    rotated = {"streams": [{"codec_type": "video", "side_data_list": [{"rotation": -90}]}]}
    assert display_size(1920, 1080, rotated) == (1080, 1920)  # phone clip stored landscape
    tag = {"streams": [{"codec_type": "video", "tags": {"rotate": "270"}}]}
    assert display_size(1920, 1080, tag) == (1080, 1920)
    anamorphic = {"streams": [{"codec_type": "video", "sample_aspect_ratio": "64:45"}]}
    assert display_size(720, 576, anamorphic) == (1024, 576)  # PAL 16:9
    unknown = {"streams": [{"codec_type": "video", "sample_aspect_ratio": "0:1"}]}
    assert display_size(640, 360, unknown) == (640, 360)
    assert display_size(None, 360) is None
    assert parse_size("1280x720") == (1280, 720) and parse_size("1280×720") == (1280, 720)
    assert parse_size("big") is None


@pytest.mark.parametrize("fit", ["pad", "crop", "blur"])
@pytest.mark.parametrize("src", ["1080x1920", "768x544", "1920x1080"])
def test_fit_filter_always_gives_the_exact_size(tmp_path, fit, src):
    out = tmp_path / "o.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size={src}:rate=25:duration=0.4", "-vf", fit_filter(1280, 720, fit),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)], check=True)
    got = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=width,height,sample_aspect_ratio", "-of", "csv=p=0", str(out)],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert got.split(",")[:2] == ["1280", "720"]


def test_transcode_scales_to_the_output_size():
    s = Settings(_env_file=None)
    plan = PublishPlan(stream_id=1, name="x", source_path=Path("/v.mp4"), inpoint_abs=0, outpoint_abs=None,
                       duration_s=10, mode="loop", output_mode="transcode", publish_url="rtsp://h/x",
                       playlist_path=Path("/p"), fps=25, width=1920, height=1080, out_width=1280, out_height=720)
    cmd = build_command(plan, s)
    vf = cmd[cmd.index("-vf") + 1]
    assert "pad=1280:720" in vf and "scale=1280:720" in vf
    plan.out_width = plan.out_height = None
    assert "-vf" not in build_command(plan, s)  # 1080p fits the transcode maximum: no scaling


def _clip(ffmpeg: str, path, size: str, seconds: int = 4) -> None:
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size={size}:rate=25:duration={seconds}", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-g", "50", str(path)], check=True)
    old = time.time() - 3600
    os.utime(path, (old, old))


def _video(rig, name):
    return next(x for x in rig.client.get("/api/videos").json() if x["filename"] == name)


def test_portrait_is_kept_but_not_streamed_until_a_16_9_copy_exists(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        _clip(rig.settings.ffmpeg_bin, rig.video_dir / "phone.mp4", "540x960")
        rig.client.post("/api/videos/refresh")
        v = _video(rig, "phone.mp4")
        assert v["shape"] == "portrait" and v["is_16_9"] is False and v["status"] == "READY"
        assert (v["display_width"], v["display_height"]) == (540, 960)

        r = rig.client.post("/api/streams", json={"name": "phone", "video_id": v["id"], "start_now": False})
        assert r.status_code == 422 and "not 16:9" in r.json()["detail"]
        assert rig.client.post(f"/api/videos/{v['id']}/convert", json={"profile": "720p"}).status_code == 409

        r = rig.client.post(f"/api/videos/{v['id']}/convert", json={"profile": "16x9", "fit": "blur"})
        assert r.status_code == 202, r.text
        job = _wait_job(rig, r.json()["id"])
        assert job["state"] == "done", job
        assert job["target"] == "phone (720p 16x9).mp4" and job["fit"] == "blur"
        copy = rig.client.get(f"/api/videos/{job['result_video_id']}").json()
        assert (copy["width"], copy["height"]) == (1280, 720) and copy["is_16_9"] is True
        assert copy["compat_mode"] == "copy" and copy["keyframe_interval_s"] <= 2.1
        assert (rig.video_dir / "phone.mp4").exists()  # the original stays
        assert _video(rig, "phone.mp4")["copy_16x9_id"] == copy["id"]

        r = rig.client.post("/api/streams", json={"name": "phone", "video_id": copy["id"], "start_now": False,
                                                  "output_size": "standard"})
        assert r.status_code == 201, r.text
        s = r.json()
        assert s["output_mode"] == "copy" and s["output_resolution"] == "1280x720"


def test_standard_output_uses_the_720p_copy(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        _clip(rig.settings.ffmpeg_bin, rig.video_dir / "cam.mp4", "1920x1080")
        rig.client.post("/api/videos/refresh")
        v = _video(rig, "cam.mp4")
        body = {"name": "cam", "video_id": v["id"], "output_size": "standard"}

        # no copy yet: scaled live (transcode), with a hint to make the copy
        r = rig.client.post("/api/streams/validate", json=body).json()
        assert r["ok"] and r["output_mode"] == "transcode"
        assert any("Make a 720p copy" in w for w in r["warnings"])
        # original size: as it is
        r = rig.client.post("/api/streams/validate", json={**body, "output_size": "original"}).json()
        assert r["output_mode"] == "copy"
        # sizes outside the list are refused
        r = rig.client.post("/api/streams/validate", json={**body, "output_size": "333x111"}).json()
        assert not r["ok"] and "not allowed" in r["errors"][0]["message"]

        r = rig.client.post(f"/api/videos/{v['id']}/convert", json={"profile": "720p"})
        assert r.status_code == 202, r.text
        job = _wait_job(rig, r.json()["id"])
        assert job["state"] == "done", job
        assert job["target"] == "cam (720p).mp4"
        assert _video(rig, "cam.mp4")["copy_720p_id"] == job["result_video_id"]
        assert rig.client.post(f"/api/videos/{job['result_video_id']}/convert",
                               json={"profile": "720p"}).status_code == 409  # already the copy

        r = rig.client.post("/api/streams", json={**body, "start_now": False})
        assert r.status_code == 201, r.text
        s = r.json()
        assert s["video_id"] == job["result_video_id"] and s["output_mode"] == "copy"
        assert s["output_resolution"] == "1280x720" and s["output_size"] == "standard"
        assert any("720p copy" in w for w in s["warnings"])


def test_config_reports_the_output_policy(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        out = rig.client.get("/api/config").json()["output"]
        assert out["width"] == 1280 and out["height"] == 720 and out["require_16_9"] is True
        assert "1920x1080" in out["sizes"]
