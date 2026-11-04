"""API behaviour that needs FFprobe but no MediaMTX.

MediaMTX is deliberately unreachable here, which also proves the dashboard
API keeps working while MediaMTX is down.
"""

import os
import shutil
import threading
import time

import pytest

from helpers import ffmpeg_publishers, link_or_copy, wait_for

VIDEOS = ["h264_25fps_audio.mp4", "h264_2997.mp4", "hevc.mp4", "h264_10bit.mp4", "vp9.webm", "corrupt.mp4",
          "h264_vfr.mkv"]


@pytest.fixture
def rig(rig_factory):
    with rig_factory(VIDEOS, mediamtx=False) as r:
        yield r


@pytest.fixture
def auth_rig(rig_factory):
    r = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, auth=True)
    from app.services.auth import UserStore

    us = UserStore(r.settings.users_path)
    us.set_user("alice", "operator", "alice-pw")
    us.set_user("victor", "viewer", "victor-pw")
    with r:
        yield r


# ------------------------------------------------------------------ auth
def test_auth_required_and_roles(auth_rig):
    c = auth_rig.client
    assert c.get("/api/health").status_code == 200  # probe stays open
    c.cookies.clear()
    assert c.get("/api/streams").status_code == 401
    assert c.get("/api/videos").status_code == 401
    assert c.post("/api/mediamtx/restart").status_code == 401
    assert c.post("/api/auth/login", json={"username": "alice", "password": "bad"}).status_code == 401

    # Basic auth for scripts
    assert c.get("/api/streams", auth=("alice", "alice-pw")).status_code == 200
    assert c.get("/api/streams", auth=("alice", "wrong")).status_code == 401

    # viewer: read-only
    r = c.post("/api/auth/login", json={"username": "victor", "password": "victor-pw"})
    assert r.status_code == 200 and r.json()["can_write"] is False
    assert c.get("/api/videos").status_code == 200
    vid = c.get("/api/videos").json()[0]["id"]
    r = c.post("/api/streams", json={"name": "x", "video_id": vid, "start_now": False})
    assert r.status_code == 403 and "read-only" in r.json()["detail"]
    assert c.post("/api/mediamtx/restart").status_code == 403

    # operator via session cookie
    c.post("/api/auth/logout")
    c.cookies.clear()
    r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-pw"})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert c.post("/api/streams", json={"name": "x", "video_id": vid, "start_now": False}).status_code == 201
    assert c.get("/api/auth/me").json()["username"] == "alice"

    # credentials never leak into responses or the event log
    events = c.get("/api/events?limit=200").text
    assert "alice-pw" not in events and "victor-pw" not in events and "bad" not in events.split("failed login")[0][-5:]


# ------------------------------------------------------------------ library
def test_library_metadata(rig):
    vids = {v["filename"]: v for v in rig.client.get("/api/videos").json()}
    assert set(VIDEOS) <= set(vids) | {"corrupt.mp4"}

    a = vids["h264_25fps_audio.mp4"]
    assert (a["video_codec"], a["width"], a["height"], a["fps"], a["pix_fmt"], a["bit_depth"]) == \
        ("h264", 640, 360, 25.0, "yuv420p", 8)
    assert a["audio_codec"] == "aac" and a["duration_s"] == pytest.approx(20, abs=0.1)
    assert a["compat_mode"] == "copy" and any("dropped" in w for w in a["compat_warnings"])
    assert a["keyframe_interval_s"] == pytest.approx(1.0, abs=0.05)
    assert a["thumbnail_url"] and a["size_bytes"] > 0

    assert vids["h264_2997.mp4"]["fps"] == pytest.approx(29.97, abs=0.01)
    assert vids["hevc.mp4"]["compat_mode"] == "transcode"
    assert vids["vp9.webm"]["compat_mode"] == "transcode"
    assert vids["h264_10bit.mp4"]["bit_depth"] == 10 and vids["h264_10bit.mp4"]["compat_mode"] == "transcode"
    assert vids["h264_vfr.mkv"]["is_vfr"] is True

    bad = rig.client.get("/api/videos?status=INVALID").json()
    assert [v["filename"] for v in bad] == ["corrupt.mp4"] and bad[0]["compat_mode"] == "reject"
    assert str(rig.video_dir) not in bad[0]["error"] and "corrupt.mp4" in bad[0]["error"]  # no server paths
    assert str(rig.base) not in rig.client.get("/api/videos").text

    assert [v["filename"] for v in rig.client.get("/api/videos?search=2997").json()] == ["h264_2997.mp4"]

    r = rig.client.get(a["thumbnail_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    r = rig.client.get(f"/api/videos/{a['id']}/frame?t=12.5")
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert rig.client.get(f"/api/videos/{a['id']}/frame?t=999").status_code == 422
    assert rig.client.get("/api/videos/99999").status_code == 404


def test_server_side_discovery_rename_and_missing(rig, fixtures_dir):
    link_or_copy(fixtures_dir / "h264_23976.mp4", rig.video_dir / "sub" / "copied in.mp4")
    s = rig.client.post("/api/videos/refresh").json()
    assert s["added"] == 1
    vid = rig.video_id("copied in.mp4")
    os.rename(rig.video_dir / "sub" / "copied in.mp4", rig.video_dir / "renamed.mp4")
    s = rig.client.post("/api/videos/refresh").json()
    assert s["renamed"] == 1 and rig.video_id("renamed.mp4") == vid  # identity follows the file
    os.unlink(rig.video_dir / "renamed.mp4")
    assert rig.client.post("/api/videos/refresh").json()["missing"] == 1
    assert vid not in [v["id"] for v in rig.client.get("/api/videos").json()]
    assert rig.client.get(f"/api/videos/{vid}").json()["status"] == "MISSING"


def test_partially_copied_file_is_not_offered(rig_factory, fixtures_dir):
    with rig_factory([], mediamtx=False, settle_seconds=30) as r:
        # a file being copied in has a fresh mtime (copyfile, unlike a hard link)
        shutil.copyfile(fixtures_dir / "h264_2997.mp4", r.video_dir / "copying.mp4")
        s = r.client.post("/api/videos/refresh").json()
        assert s["settling"] == ["copying.mp4"] and r.client.get("/api/videos").json() == []


# ------------------------------------------------------------------ uploads
def test_upload_streaming_success(rig, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes()
    pre = rig.client.post("/api/videos/upload/check", json={"filename": "new clip.mp4", "size": len(data)})
    assert pre.status_code == 200 and pre.json()["ok"] is True
    r = rig.client.post("/api/videos/upload?filename=new clip.mp4", content=data,
                        headers={"content-type": "application/octet-stream"})
    assert r.status_code == 201, r.text
    assert r.json()["filename"] == "new clip.mp4" and r.json()["video_codec"] == "h264"
    assert (rig.video_dir / "new clip.mp4").read_bytes() == data
    assert list((rig.video_dir / ".uploads").iterdir()) == []
    # same name again -> conflict, existing file untouched
    r = rig.client.post("/api/videos/upload?filename=new clip.mp4", content=b"x" * 10)
    assert r.status_code == 409
    assert (rig.video_dir / "new clip.mp4").read_bytes() == data


def test_upload_rejections(rig):
    c = rig.client
    assert c.post("/api/videos/upload?filename=evil.sh", content=b"#!/bin/sh").status_code == 415
    r = c.post("/api/videos/upload?filename=../../escape.mp4", content=b"not a video" * 100)
    assert r.status_code == 422  # name sanitised to escape.mp4, then rejected as not a video
    assert not (rig.video_dir.parent / "escape.mp4").exists() and not (rig.video_dir / "escape.mp4").exists()
    assert list((rig.video_dir / ".uploads").iterdir()) == []


def test_upload_refused_when_disk_nearly_full(rig_factory, fixtures_dir):
    """EC-08: refuse before copying, show free space, leave no partial file."""
    with rig_factory([], mediamtx=False, disk_reserve_bytes=10**15) as r:
        data = (fixtures_dir / "h264_2997.mp4").read_bytes()
        pre = r.client.post("/api/videos/upload/check", json={"filename": "a.mp4", "size": len(data)}).json()
        assert pre["ok"] is False and pre["free"] > 0 and pre["available"] == 0
        resp = r.client.post("/api/videos/upload?filename=a.mp4", content=data)
        assert resp.status_code == 507
        detail = resp.json()["detail"]
        assert detail["required"] == len(data) and detail["free"] > 0 and "refused before copying" in detail["reason"]
        assert not (r.video_dir / "a.mp4").exists()
        assert list((r.video_dir / ".uploads").iterdir()) == []


# ------------------------------------------------------------------ stream validation
def _body(rig, **kw):
    b = {"name": "cam", "video_id": rig.video_id("h264_25fps_audio.mp4"), "start_now": False}
    b.update(kw)
    return b


@pytest.mark.parametrize("kw,status,fragment", [
    ({"name": "my cam"}, 422, "no spaces"),
    ({"name": "cam/1"}, 422, "slashes"),
    ({"name": "cam#1"}, 422, "Invalid"),
    ({"name": ""}, 422, "required"),
    ({"start": "00:00:25"}, 422, "beyond the end"),
    ({"start": "00:00:10", "end": "00:00:05"}, 422, "must be after"),
    ({"start": "00:00:10", "end": "00:00:10"}, 422, "must be after"),
    ({"end": "00:00:21"}, 422, "beyond the end"),
    ({"start": "abc"}, 422, "Invalid time"),
    ({"start": "-5"}, 422, "negative"),
])
def test_invalid_stream_requests_rejected_before_ffmpeg(rig, kw, status, fragment):
    r = rig.client.post("/api/streams", json=_body(rig, start_now=True, **kw))
    assert r.status_code == status, r.text
    assert fragment in r.json()["detail"]
    assert rig.client.get("/api/streams").json() == []  # no dead stream left behind
    assert ffmpeg_publishers(rig.run_dir_marker()) == []  # no process started


def test_valid_boundaries_accepted(rig):
    c = rig.client
    assert c.post("/api/streams", json=_body(rig, name="b1", start="0", end="00:00:20")).status_code == 201
    r = c.post("/api/streams", json=_body(rig, name="b2", start="00:00:19"))
    assert r.status_code == 201 and r.json()["start_offset_s"] == 19


def test_unsupported_codec_rejected_when_policy_never(rig_factory):
    with rig_factory(["hevc.mp4", "corrupt.mp4"], mediamtx=False, transcode_policy="never") as r:
        resp = r.client.post("/api/streams", json={"name": "h", "video_id": r.video_id("hevc.mp4")})
        assert resp.status_code == 422
        assert "HEVC" in resp.json()["detail"] and "disabled" in resp.json()["detail"]
        resp = r.client.post("/api/streams", json={"name": "c", "video_id": r.video_id("corrupt.mp4")})
        assert resp.status_code == 422 and "not usable" in resp.json()["detail"]


def test_duplicate_names_including_case(rig):
    c = rig.client
    assert c.post("/api/streams", json=_body(rig, name="Cam1")).status_code == 201
    r = c.post("/api/streams", json=_body(rig, name="Cam1"))
    assert r.status_code == 409 and "already in use" in r.json()["detail"]
    assert c.post("/api/streams", json=_body(rig, name="cam1")).status_code == 409
    assert len(c.get("/api/streams").json()) == 1


def test_concurrent_creation_same_name(rig):
    """EC-06/EC-11: 10 users create the same name at the same moment."""
    results = []
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        results.append(rig.client.post("/api/streams", json=_body(rig, name="race")).status_code)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == [201] + [409] * 9
    assert [s["name"] for s in rig.client.get("/api/streams").json()] == ["race"]


def test_optimistic_concurrency(rig):
    c = rig.client
    s = c.post("/api/streams", json=_body(rig, name="edit")).json()
    v = s["version"]
    r = c.patch(f"/api/streams/{s['id']}", json={"start": "00:00:05"}, headers={"If-Match": str(v)})
    assert r.status_code == 200 and r.json()["version"] == v + 1
    # second user still holds the old version -> refused, not silently overwritten
    r = c.patch(f"/api/streams/{s['id']}", json={"start": "00:00:08"}, headers={"If-Match": str(v)})
    assert r.status_code == 409 and "changed by" in r.json()["detail"]
    assert c.get(f"/api/streams/{s['id']}").json()["start_offset_s"] == 5
    r = c.delete(f"/api/streams/{s['id']}", headers={"If-Match": str(v)})
    assert r.status_code == 409


def test_delete_never_removes_source(rig):
    s = rig.client.post("/api/streams", json=_body(rig, name="del")).json()
    assert rig.client.delete(f"/api/streams/{s['id']}").status_code == 204
    assert (rig.video_dir / "h264_25fps_audio.mp4").exists()
    assert rig.client.get(f"/api/streams/{s['id']}").status_code == 404


def test_dashboard_works_while_mediamtx_down(rig):
    st = wait_for(lambda: (j := rig.client.get("/api/mediamtx/status").json())["state"] == "DOWN" and j, 10,
                  desc="DOWN")
    assert st["last_detection_s"] is not None and st["last_detection_s"] < 10
    assert rig.client.get("/api/system").status_code == 200
    assert rig.client.get("/api/streams").status_code == 200
    h = rig.client.get("/api/health").json()
    assert h["status"] == "degraded" and h["mediamtx"] == "DOWN"
    assert rig.client.get("/api/health?strict=true").status_code == 503


def test_system_endpoint(rig):
    time.sleep(0.5)
    j = rig.client.get("/api/system").json()
    assert 0 <= j["cpu_percent"] <= 100
    assert j["memory"]["total"] > 0 and j["disks"]["videos"]["free"] > 0
    assert j["streams"]["max_active"] == rig.settings.max_active_streams
    cfg = rig.client.get("/api/config").json()
    assert cfg["rtsp_url_template"] == f"rtsp://rig.test:{rig.ports['rtsp']}/{{name}}"
