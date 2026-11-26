"""Video clipper: cut marked segments out of a library video, join them, add the result to the library."""

import json
import os
import subprocess
import time

from test_convert import _wait_job


def _make_h264(ffmpeg: str, path, seconds: int) -> None:
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=640x360:rate=25:duration={seconds}", "-c:v", "libx264", "-bf", "3",
                    str(path)], check=True)
    old = time.time() - 3600
    os.utime(path, (old, old))


def _probe(ffprobe: str, path) -> dict:
    out = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_packets",
                          "-show_entries", "stream=codec_name,has_b_frames,nb_read_packets:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_clip_two_segments_joined_into_library(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        _make_h264(rig.settings.ffmpeg_bin, rig.video_dir / "ALPR_toll-s1_01.mp4", 12)
        rig.client.post("/api/videos/refresh")
        v = next(x for x in rig.client.get("/api/videos").json() if x["filename"] == "ALPR_toll-s1_01.mp4")

        name = rig.client.get(f"/api/videos/{v['id']}/clip-name").json()["name"]
        assert name == "ALPR_toll-s1_02.mp4"  # same place, next free number

        r = rig.client.post(f"/api/videos/{v['id']}/clip", json={"segments": [[1, 3], [8, 10.5]], "name": name})
        assert r.status_code == 202, r.text
        job = _wait_job(rig, r.json()["id"])
        assert job["state"] == "done", job
        assert job["profile"] == "clip" and job["segments"] == [[1.0, 3.0], [8.0, 10.5]]

        out = rig.video_dir / name
        info = _probe(rig.settings.ffprobe_bin, out)
        st = info["streams"][0]
        assert st["codec_name"] == "h264" and st["has_b_frames"] == 0  # camera-style
        assert abs(float(info["format"]["duration"]) - 4.5) < 0.2
        assert abs(int(st["nb_read_packets"]) - 4.5 * 25) <= 2  # frame-exact cuts
        new = next(x for x in rig.client.get("/api/videos").json() if x["id"] == job["result_video_id"])
        assert new["filename"] == name and new["status"] == "READY"

        # the name is taken now: refused, and the suggestion moves on
        r = rig.client.post(f"/api/videos/{v['id']}/clip", json={"segments": [[0, 1]], "name": name})
        assert r.status_code == 409
        assert rig.client.get(f"/api/videos/{v['id']}/clip-name").json()["name"] == "ALPR_toll-s1_03.mp4"


def test_clip_validation(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        _make_h264(rig.settings.ffmpeg_bin, rig.video_dir / "MISC_lab_01.mp4", 4)
        rig.client.post("/api/videos/refresh")
        vid = next(x for x in rig.client.get("/api/videos").json() if x["filename"] == "MISC_lab_01.mp4")["id"]
        bad = [
            {"segments": [], "name": "MISC_lab_02.mp4"},
            {"segments": [[1, 1.05]], "name": "MISC_lab_02.mp4"},       # too short
            {"segments": [[0, 1]], "name": "../x.mp4"},                 # no paths
            {"segments": [[0, 1]] * 101, "name": "MISC_lab_02.mp4"},    # too many
        ]
        for body in bad:
            assert rig.client.post(f"/api/videos/{vid}/clip", json=body).status_code == 422, body
        assert rig.client.post("/api/videos/99999/clip", json={"segments": [[0, 1]], "name": "a.mp4"}).status_code == 404
        assert "clipper" in rig.client.get("/api/config").json()["features"]


def test_clip_name_uses_known_codes():
    from types import SimpleNamespace
    from app.services.converter import Converter
    c = Converter.__new__(Converter)
    v = SimpleNamespace(rel_path="CLOCK_lab_01.mp4")
    assert c.clip_name(v, ["CLOCK_lab_01.mp4"]) == "CLOCK_lab_02.mp4"                   # naming not enforced
    assert c.clip_name(v, ["CLOCK_lab_01.mp4"], {"ALPR", "MISC"}) == "MISC_lab_01.mp4"  # unknown code -> MISC
    v = SimpleNamespace(rel_path="ALPR_toll-s1_01_720p_night.mp4")
    assert c.clip_name(v, ["ALPR_toll-s1_01_720p_night.mp4"], {"ALPR"}) == "ALPR_toll-s1_02_night.mp4"
    assert c.clip_name(SimpleNamespace(rel_path="traffic cam ALPR.mp4"), [], {"ALPR"}) == "ALPR_traffic-cam_01.mp4"
