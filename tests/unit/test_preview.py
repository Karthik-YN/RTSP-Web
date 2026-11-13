"""Short browser previews of long library videos."""

import json
import os
import subprocess
import time

import pytest


def _clip(ffmpeg, path, seconds, codec="libx264"):
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"testsrc=size=640x360:rate=10:duration={seconds}", "-c:v", codec, "-g", "20", str(path)],
                   check=True)
    old = time.time() - 3600
    os.utime(path, (old, old))


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    d = json.loads(out)
    return d["streams"][0], float(d["format"]["duration"])


def test_long_video_preview_is_made_once_and_cached(rig_factory, tmp_path):
    with rig_factory([], mediamtx=False, preview_threshold_s=30, preview_seconds=10) as rig:
        _clip(rig.settings.ffmpeg_bin, rig.video_dir / "long.avi", 70, codec="mpeg4")  # browsers can't play it
        rig.client.post("/api/videos/refresh")
        v = next(x for x in rig.client.get("/api/videos").json() if x["filename"] == "long.avi")
        assert v["browser_playable"] is False
        assert rig.client.get("/api/config").json()["preview"] == {"threshold_s": 30, "seconds": 10}

        r = rig.client.get(f"/api/videos/{v['id']}/preview")
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
        assert r.headers["x-preview-start"] == "0" and "inline" in r.headers["content-disposition"]
        (tmp_path / "p.mp4").write_bytes(r.content)
        stream, dur = _probe(tmp_path / "p.mp4")
        assert stream["codec_name"] == "h264" and dur == pytest.approx(10, abs=0.6)

        cache = rig.settings.preview_path
        first = list(cache.glob(f"{v['id']}-*.mp4"))
        assert len(first) == 1
        t0 = time.monotonic()
        assert rig.client.get(f"/api/videos/{v['id']}/preview").status_code == 200
        assert time.monotonic() - t0 < 1.0  # served from the cache
        assert list(cache.glob(f"{v['id']}-*.mp4")) == first

        r = rig.client.get(f"/api/videos/{v['id']}/preview?start=40")
        assert r.headers["x-preview-start"] == "40"
        r = rig.client.get(f"/api/videos/{v['id']}/preview?start=69")
        assert r.headers["x-preview-start"] == "60"  # clamped: a full clip before the end
        assert len(list(cache.glob(f"{v['id']}-*.mp4"))) == 3

        assert rig.client.get("/api/videos/9999/preview").status_code == 404
        assert rig.client.get(f"/api/videos/{v['id']}/preview?start=-1").status_code == 422

        # deleting the video drops its previews
        assert rig.client.delete(f"/api/videos/{v['id']}").status_code == 200
        assert not list(cache.glob(f"{v['id']}-*.mp4"))


def test_preview_cache_keeps_under_its_size_limit(rig_factory):
    with rig_factory([], mediamtx=False, preview_threshold_s=5, preview_seconds=10, preview_cache_bytes=1) as rig:
        _clip(rig.settings.ffmpeg_bin, rig.video_dir / "a.mp4", 40)
        rig.client.post("/api/videos/refresh")
        vid = rig.video_id("a.mp4")
        for start in (0, 20):
            assert rig.client.get(f"/api/videos/{vid}/preview?start={start}").status_code == 200
        # over the (tiny) limit: older clips are removed, never the one being sent
        left = [p.name for p in rig.settings.preview_path.glob("*.mp4")]
        assert len(left) == 1 and left[0].startswith(f"{vid}-") and left[0].endswith("-20.mp4")


def test_playable_h264_is_cut_without_re_encoding():
    from types import SimpleNamespace

    from app.services.preview import PreviewCache

    v = SimpleNamespace(status="READY", rel_path="a.mp4", video_codec="h264", pix_fmt="yuv420p", bit_depth=8,
                        bitrate_bps=6_000_000)
    assert PreviewCache.can_copy(v)
    assert not PreviewCache.can_copy(SimpleNamespace(**{**vars(v), "video_codec": "mpeg4"}))
    assert not PreviewCache.can_copy(SimpleNamespace(**{**vars(v), "bitrate_bps": 40_000_000}))  # too heavy
    assert not PreviewCache.can_copy(SimpleNamespace(**{**vars(v), "rel_path": "a.mkv"}))
