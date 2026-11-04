"""Stream groups, URL export and bulk actions (incl. protected streams)."""

import pytest

from app.services.auth import UserStore


@pytest.fixture
def rig(rig_factory):
    r = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, auth=True)
    us = UserStore(r.settings.users_path)
    us.set_user("alice", "operator", "alice-password")
    us.set_user("bob", "operator", "bob-password")
    with r:
        yield r


A, B = ("alice", "alice-password"), ("bob", "bob-password")


def _mk(rig, name, auth, **kw):
    vid = rig.client.get("/api/videos", auth=auth).json()[0]["id"]
    r = rig.client.post("/api/streams", auth=auth, json={"name": name, "video_id": vid, "start_now": False, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_group_lifecycle_and_url_export(rig):
    c = rig.client
    ids = [_mk(rig, f"cam{i:02d}", A) for i in range(3)]
    g = c.post("/api/groups", auth=A, json={"name": "Store demo", "stream_ids": ids[:2]}).json()
    assert g["total"] == 2 and sorted(g["stream_ids"]) == sorted(ids[:2])
    assert c.post("/api/groups", auth=A, json={"name": "store DEMO"}).status_code == 409  # case-insensitive
    c.post("/api/groups/members", auth=A, json={"stream_ids": [ids[2]], "group_id": g["id"]})
    assert c.get("/api/groups", auth=B).json()[0]["total"] == 3
    txt = c.get(f"/api/groups/{g['id']}/urls", auth=B).text.splitlines()
    assert txt == [f"rtsp://rig.test:{rig.ports['rtsp']}/cam{i:02d}" for i in range(3)]
    csv = c.get(f"/api/groups/{g['id']}/urls?format=csv", auth=B).text.splitlines()
    assert csv[0] == "name,rtsp_url" and csv[1].startswith("cam00,rtsp://")
    assert c.get("/api/streams", auth=A).json()[0]["group_id"] == g["id"]
    # deleting the group keeps its streams
    assert c.delete(f"/api/groups/{g['id']}", auth=A).status_code == 204
    streams = c.get("/api/streams", auth=A).json()
    assert len(streams) == 3 and all(s["group_id"] is None for s in streams)


def test_bulk_actions_respect_protection_and_report_per_stream(rig):
    c = rig.client
    mine = [_mk(rig, f"a{i}", A) for i in range(2)]
    locked = _mk(rig, "locked", A, protected=True, note="keep")
    r = c.post("/api/streams/bulk", auth=B, json={"stream_ids": mine + [locked], "action": "delete"}).json()
    assert r["ok"] == 2 and r["failed"] == 1
    bad = next(x for x in r["results"] if not x["ok"])
    assert bad["id"] == locked and "protected by alice" in bad["error"]
    remaining = [s["name"] for s in c.get("/api/streams", auth=A).json()]
    assert remaining == ["locked"]
    assert c.post("/api/streams/bulk", auth=B, json={"stream_ids": [], "action": "stop"}).status_code == 422


def test_group_action_stop(rig):
    c = rig.client
    ids = [_mk(rig, f"g{i}", A) for i in range(3)]
    g = c.post("/api/groups", auth=A, json={"name": "demo", "stream_ids": ids}).json()
    r = c.post(f"/api/groups/{g['id']}/stop", auth=A).json()
    assert r["ok"] == 3 and r["failed"] == 0
    assert all(s["desired_state"] == "STOPPED" for s in c.get("/api/streams", auth=A).json())


def test_preview_from_source_position(rig):
    c = rig.client
    sid = _mk(rig, "prev", A, start="00:00:05")
    r = c.get(f"/api/streams/{sid}/preview", auth=A)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
