"""Multi-user safety: roles, protected streams, login throttling, user admin."""

import pytest

from app.services.auth import UserStore


@pytest.fixture
def rig(rig_factory):
    r = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, auth=True)
    us = UserStore(r.settings.users_path)
    us.set_user("root", "admin", "root-password")
    us.set_user("alice", "operator", "alice-password")
    us.set_user("bob", "operator", "bob-password")
    us.set_user("victor", "viewer", "victor-password")
    with r:
        yield r


def as_(rig, user):
    return (user, f"{user}-password")


def test_protected_stream_only_owner_or_admin(rig):
    c = rig.client
    vid = c.get("/api/videos", auth=as_(rig, "alice")).json()[0]["id"]
    s = c.post("/api/streams", auth=as_(rig, "alice"),
               json={"name": "demo", "video_id": vid, "start_now": False, "protected": True,
                     "note": "Client demo until 17:00"}).json()
    assert s["owner"] == "alice" and s["protected"] is True and s["note"] == "Client demo until 17:00"

    for path, method in ((f"/api/streams/{s['id']}/start", "post"), (f"/api/streams/{s['id']}/stop", "post"),
                         (f"/api/streams/{s['id']}/restart", "post"), (f"/api/streams/{s['id']}", "delete")):
        r = getattr(c, method)(path, auth=as_(rig, "bob"))
        assert r.status_code == 403, (path, r.text)
        assert "protected by alice" in r.json()["detail"] and "Client demo until 17:00" in r.json()["detail"]
    r = c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "bob"), json={"note": "mine now"})
    assert r.status_code == 403

    # owner may edit and unprotect; admin may transfer ownership
    r = c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "alice"), json={"note": "free after lunch"})
    assert r.status_code == 200 and r.json()["note"] == "free after lunch"
    assert c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "alice"), json={"owner": "bob"}).status_code == 403
    r = c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "root"), json={"owner": "bob"})
    assert r.status_code == 200 and r.json()["owner"] == "bob"
    assert c.post(f"/api/streams/{s['id']}/stop", auth=as_(rig, "bob")).status_code == 200
    # unprotected streams stay shared
    r = c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "bob"), json={"protected": False})
    assert r.status_code == 200
    assert c.delete(f"/api/streams/{s['id']}", auth=as_(rig, "alice")).status_code == 204


def test_metadata_edit_does_not_restart_or_reset_loops(rig):
    c = rig.client
    vid = c.get("/api/videos", auth=as_(rig, "alice")).json()[0]["id"]
    s = c.post("/api/streams", auth=as_(rig, "alice"), json={"name": "m", "video_id": vid, "start_now": False}).json()
    r = c.patch(f"/api/streams/{s['id']}", auth=as_(rig, "alice"), json={"note": "hello"},
                headers={"If-Match": str(s["version"])})
    assert r.status_code == 200 and r.json()["version"] == s["version"] + 1
    assert r.json()["start_offset_s"] == s["start_offset_s"]


def test_login_lockout_after_repeated_failures(rig):
    c = rig.client
    for _ in range(5):
        assert c.post("/api/auth/login", json={"username": "alice", "password": "nope"}).status_code == 401
    r = c.post("/api/auth/login", json={"username": "alice", "password": "alice-password"})
    assert r.status_code == 429 and "Retry-After" in r.headers  # even the right password waits
    # Basic auth cannot be used to keep guessing either
    assert c.get("/api/streams", auth=("alice", "nope")).status_code == 429
    # other users on the same address (e.g. behind nginx/NAT) are not locked out
    assert c.get("/api/streams", auth=as_(rig, "bob")).status_code == 200
    kinds = [e["kind"] for e in c.get("/api/events?limit=50", auth=as_(rig, "root")).json()]
    assert kinds.count("auth.failed") >= 5


def test_user_administration(rig):
    c = rig.client
    assert c.get("/api/users", auth=as_(rig, "alice")).status_code == 403
    assert [u["username"] for u in c.get("/api/users", auth=as_(rig, "root")).json()] == \
        ["alice", "bob", "root", "victor"]
    r = c.post("/api/users", auth=as_(rig, "root"), json={"username": "dana", "role": "viewer", "password": "dana-password"})
    assert r.status_code == 201 and r.json()["role"] == "viewer"
    assert c.post("/api/users", auth=as_(rig, "root"),
                  json={"username": "dana", "role": "viewer", "password": "dana-password"}).status_code == 409
    assert c.post("/api/users", auth=as_(rig, "root"),
                  json={"username": "eve", "password": "short"}).status_code == 422
    r = c.patch("/api/users/dana", auth=as_(rig, "root"), json={"role": "operator"})
    assert r.json()["role"] == "operator" and r.json()["can_write"]
    assert c.get("/api/streams", auth=("dana", "dana-password")).status_code == 200  # password kept
    # the last admin cannot be demoted or deleted, nor can you delete yourself
    assert c.patch("/api/users/root", auth=as_(rig, "root"), json={"role": "viewer"}).status_code == 409
    assert c.delete("/api/users/root", auth=as_(rig, "root")).status_code == 409
    assert c.delete("/api/users/dana", auth=as_(rig, "root")).status_code == 204
    assert c.get("/api/streams", auth=("dana", "dana-password")).status_code == 401
    assert "dana-password" not in (rig.settings.users_path).read_text()


def test_change_own_password(rig):
    c = rig.client
    r = c.post("/api/auth/password", auth=as_(rig, "victor"),
               json={"current_password": "wrong", "new_password": "victor-new-pass"})
    assert r.status_code == 403
    r = c.post("/api/auth/password", auth=as_(rig, "victor"),
               json={"current_password": "victor-password", "new_password": "victor-new-pass"})
    assert r.status_code == 204
    assert c.get("/api/streams", auth=("victor", "victor-new-pass")).status_code == 200
    me = c.get("/api/auth/me", auth=("victor", "victor-new-pass")).json()
    assert me["role"] == "viewer" and me["is_admin"] is False
