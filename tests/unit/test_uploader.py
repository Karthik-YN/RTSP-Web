"""Who uploaded each library video: user, time, address and device."""

import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from app.services.agent import describe_agent
from app.services.auth import UserStore, uploader_label

CHROME_WIN = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/130.0.0.0 Safari/537.36")


@pytest.mark.parametrize("ua, name", [
    (CHROME_WIN, "Windows · Chrome"),
    (CHROME_WIN + " Edg/130.0.0.0", "Windows · Edge"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 "
     "Safari/605.1.15", "macOS · Safari"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/18.0 Mobile/15E148 Safari/604.1", "iPhone · Safari"),
    ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile "
     "Safari/537.36", "Android · Chrome"),
    ("Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", "Linux · Firefox"),
    ("curl/8.5.0", "curl"),
    ("python-httpx/0.27.0", "Python script"),
    ("", None),
    (None, None),
])
def test_describe_agent(ua, name):
    assert describe_agent(ua) == name


def test_upload_records_who_when_where(rig_factory, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes()
    with rig_factory([], mediamtx=False) as rig:
        c = rig.client
        before = datetime.now(timezone.utc) - timedelta(seconds=5)
        # X-Forwarded-For from a non-loopback peer is ignored (can't fake the address)
        r = c.post("/api/videos/upload?filename=one.mp4", content=data,
                   headers={"user-agent": CHROME_WIN, "x-forwarded-for": "6.6.6.6"})
        assert r.status_code == 201, r.text
        v = r.json()
        assert v["uploaded_by"] == "anonymous"  # login off
        assert v["uploaded_from"] == "testclient" and v["uploaded_device"] == "Windows · Chrome"
        assert before <= datetime.fromisoformat(v["uploaded_at"]) <= datetime.now(timezone.utc)

        # resumable upload keeps the origin of the session start
        s = c.post("/api/videos/uploads", json={"filename": "two.mp4", "size": len(data)},
                   headers={"user-agent": "curl/8.5.0"}).json()
        r = c.put(f"/api/videos/uploads/{s['id']}?offset=0", content=data,
                  headers={"content-type": "application/octet-stream"})
        assert r.status_code == 201, r.text
        assert r.json()["uploaded_device"] == "curl" and r.json()["uploaded_by"] == "anonymous"

        # a file copied straight into the folder has no uploader; the list shows the same fields
        (rig.video_dir / "three.mp4").write_bytes(data)
        old = time.time() - 3600
        os.utime(rig.video_dir / "three.mp4", (old, old))
        c.post("/api/videos/refresh")
        lib = {x["filename"]: x for x in c.get("/api/videos").json()}
        assert lib["one.mp4"]["uploaded_device"] == "Windows · Chrome"
        assert lib["three.mp4"]["uploaded_by"] is None and lib["three.mp4"]["uploaded_at"] is None
        assert lib["three.mp4"]["uploaded_device"] is None


def test_with_login_the_signed_in_user_is_the_uploader(rig_factory, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes()
    rig = rig_factory([], mediamtx=False, auth=True)
    us = UserStore(rig.settings.users_path)
    us.set_user("alice", "operator", "alice-password")
    us.set_user("victor", "viewer", "victor-password")
    with rig:
        c = rig.client
        r = c.post("/api/videos/upload?filename=a.mp4", content=data, auth=("alice", "alice-password"))
        assert r.status_code == 201, r.text
        assert r.json()["uploaded_by"] == "alice"
        # viewers can't upload, and nobody can without signing in
        assert c.post("/api/videos/upload?filename=b.mp4", content=data,
                      auth=("victor", "victor-password")).status_code == 403
        assert c.post("/api/videos/upload?filename=b.mp4", content=data).status_code == 401
        assert c.post("/api/videos/uploads", json={"filename": "b.mp4", "size": len(data)},
                      auth=("victor", "victor-password")).status_code == 403
        # but viewers do see who uploaded what
        lib = c.get("/api/videos", auth=("victor", "victor-password")).json()
        assert [x["uploaded_by"] for x in lib] == ["alice"]


@pytest.mark.parametrize("uploaded_by, admins, shown", [
    (None, set(), "admin"),            # copied onto the server
    ("anonymous", set(), "admin"),     # uploaded while login was off
    ("rtsp-admin", {"rtsp-admin"}, "admin"),
    ("rtsp-user", {"rtsp-admin"}, "rtsp-user"),
    ("gone-user", set(), "gone-user"),  # deleted account: its name stays
])
def test_uploader_label(uploaded_by, admins, shown):
    assert uploader_label(uploaded_by, admins) == shown


def test_admin_uploads_show_as_admin_others_by_name(rig_factory, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes()
    rig = rig_factory([], mediamtx=False, auth=True)
    us = UserStore(rig.settings.users_path)
    us.set_user("boss", "admin", "boss-password")
    us.set_user("alice", "operator", "alice-password")
    us.set_user("victor", "viewer", "victor-password")
    with rig:
        c = rig.client
        r = c.post("/api/videos/upload?filename=by-admin.mp4", content=data, auth=("boss", "boss-password"))
        assert r.status_code == 201, r.text
        assert r.json()["uploader"] == "admin" and r.json()["uploaded_by"] == "boss"  # the record keeps the account
        r = c.post("/api/videos/upload?filename=by-alice.mp4", content=data, auth=("alice", "alice-password"))
        assert r.json()["uploader"] == "alice"
        (rig.video_dir / "copied.mp4").write_bytes(data)
        old = time.time() - 3600
        os.utime(rig.video_dir / "copied.mp4", (old, old))
        c.post("/api/videos/refresh", auth=("boss", "boss-password"))
        lib = {x["filename"]: x for x in c.get("/api/videos", auth=("victor", "victor-password")).json()}
        assert {n: v["uploader"] for n, v in lib.items()} == {
            "by-admin.mp4": "admin", "by-alice.mp4": "alice", "copied.mp4": "admin"}
        vid = lib["by-alice.mp4"]["id"]
        assert c.get(f"/api/videos/{vid}", auth=("victor", "victor-password")).json()["uploader"] == "alice"
        # promoting alice makes her uploads show as admin too
        us.set_user("alice", "admin", None)
        assert c.get(f"/api/videos/{vid}", auth=("victor", "victor-password")).json()["uploader"] == "admin"


def test_login_off_uploads_show_as_admin(rig_factory, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes()
    with rig_factory([], mediamtx=False) as rig:
        r = rig.client.post("/api/videos/upload?filename=x.mp4", content=data)
        assert r.json()["uploaded_by"] == "anonymous" and r.json()["uploader"] == "admin"
