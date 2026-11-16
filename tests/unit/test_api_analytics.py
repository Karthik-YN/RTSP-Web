"""Analytics taskbar: seeded categories, video placeholders and running cameras from them."""

import pytest

from app.api.analytics import next_names, slugify
from app.database.migrations import ANALYTICS_SEED


@pytest.fixture
def rig(rig_factory):
    r = rig_factory(["h264_25fps_audio.mp4", "h264_2997.mp4"], mediamtx=False)
    with r:
        yield r


def _analytics(rig, name):
    for cat in rig.client.get("/api/analytics").json():
        for a in cat["analytics"]:
            if a["name"] == name:
                return a
    raise KeyError(name)


def _slot(rig, aid, title="Gate camera", video=None, **kw):
    body = {"title": title, "description": "Workers at the gate, day shift", **kw}
    if video:
        body["video_id"] = rig.video_id(video)
    r = rig.client.post(f"/api/analytics/{aid}/slots", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_seeded_tree_matches_the_requested_list(rig):
    tree = rig.client.get("/api/analytics").json()
    assert [(c["name"], [a["name"] for a in c["analytics"]]) for c in tree] == \
        [(cat, names) for cat, names in ANALYTICS_SEED]
    assert sum(len(c["analytics"]) for c in tree) == 17
    ppe = _analytics(rig, "PPE")
    assert ppe == {**ppe, "slots": 0, "slot_ids": [], "streams": 0, "running": 0, "problems": 0, "viewers": 0}


def test_names_and_slugs():
    assert slugify("Charging station") == "charging-station"
    assert slugify("Human Attributes") == "human-attributes"
    assert slugify("videoANPR") == "videoanpr"
    assert next_names("ppe", {"PPE-1", "ppe-3"}, 3) == ["ppe-2", "ppe-4", "ppe-5"]


def test_placeholder_lifecycle_and_run_cameras(rig):
    c = rig.client
    aid = _analytics(rig, "PPE")["id"]
    empty = _slot(rig, aid, "Empty slot")
    assert empty["video"] is None and empty["streams"] == []
    # running an empty placeholder is refused with a clear message
    r = c.post(f"/api/analytics/slots/{empty['id']}/streams", json={"count": 2, "start_now": False})
    assert r.status_code == 409 and "no video" in r.json()["detail"]

    slot = _slot(rig, aid, video="h264_25fps_audio.mp4")
    assert slot["video"]["filename"] == "h264_25fps_audio.mp4"
    r = c.post(f"/api/analytics/slots/{slot['id']}/streams", json={"count": 3, "start_now": False})
    assert r.status_code == 201, r.text
    body = r.json()
    assert [s["name"] for s in body["created"]] == ["ppe-1", "ppe-2", "ppe-3"]
    assert all(s["slot_id"] == slot["id"] for s in body["created"]) and body["errors"] == []
    # numbering continues after existing names
    more = c.post(f"/api/analytics/slots/{slot['id']}/streams", json={"count": 1, "start_now": False}).json()
    assert [s["name"] for s in more["created"]] == ["ppe-4"]

    d = c.get(f"/api/analytics/{aid}").json()
    assert d["category"]["name"] == "People" and d["streams"] == 4
    assert [sl["title"] for sl in d["slots"]] == ["Empty slot", "Gate camera"]
    assert len(d["slots"][1]["streams"]) == 4
    assert _analytics(rig, "PPE")["slots"] == 2 and _analytics(rig, "PPE")["streams"] == 4

    # edit the description, reorder
    r = c.patch(f"/api/analytics/slots/{slot['id']}", json={"description": "Night shift"})
    assert r.json()["description"] == "Night shift"
    c.post(f"/api/analytics/{aid}/slots/reorder", json={"slot_ids": [slot["id"], empty["id"]]})
    assert [sl["id"] for sl in c.get(f"/api/analytics/{aid}").json()["slots"]] == [slot["id"], empty["id"]]
    assert c.post(f"/api/analytics/{aid}/slots/reorder", json={"slot_ids": [slot["id"]]}).status_code == 422

    # URL export for the portal
    txt = c.get(f"/api/analytics/{aid}/urls").text.splitlines()
    assert txt == [f"rtsp://rig.test:{rig.ports['rtsp']}/ppe-{i}" for i in range(1, 5)]
    assert c.get(f"/api/analytics/{aid}/urls?format=csv").text.startswith("name,rtsp_url\nppe-1,rtsp://")

    # deleting is refused while something depends on it
    assert c.delete(f"/api/analytics/slots/{slot['id']}").status_code == 409
    assert c.delete(f"/api/analytics/{aid}").status_code == 409
    for s in c.get("/api/streams").json():
        assert c.delete(f"/api/streams/{s['id']}").status_code == 204
    assert c.delete(f"/api/analytics/slots/{slot['id']}").status_code == 204
    assert c.delete(f"/api/analytics/slots/{empty['id']}").status_code == 204
    assert _analytics(rig, "PPE")["slots"] == 0


def test_stop_all_and_start_all(rig):
    c = rig.client
    aid = _analytics(rig, "FireSmoke")["id"]
    slot = _slot(rig, aid, video="h264_25fps_audio.mp4")
    c.post(f"/api/analytics/slots/{slot['id']}/streams", json={"count": 2, "start_now": False})
    r = c.post(f"/api/analytics/{aid}/stop").json()
    assert r["ok"] == 2 and r["failed"] == 0
    assert c.post(f"/api/analytics/{aid}/bogus").status_code == 422


def test_capacity_keeps_created_cameras_and_reports_the_rest(rig_factory):
    r = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, max_active_streams=2)
    with r:
        c = r.client
        aid = _analytics(r, "Parking")["id"]
        slot = _slot(r, aid, video="h264_25fps_audio.mp4")
        body = c.post(f"/api/analytics/slots/{slot['id']}/streams", json={"count": 4}).json()
        assert [s["name"] for s in body["created"]] == ["parking-1", "parking-2"]
        assert body["errors"][0]["name"] == "parking-3" and "Capacity reached" in body["errors"][0]["error"]
        assert body["skipped"] == ["parking-4"]
        # nothing could be created: plain error response
        r2 = c.post(f"/api/analytics/slots/{slot['id']}/streams", json={"count": 1})
        assert r2.status_code == 409 and "Capacity reached" in r2.json()["detail"]
        c.post(f"/api/analytics/{aid}/stop")


def test_deleting_a_video_empties_its_placeholder(rig):
    c = rig.client
    aid = _analytics(rig, "Elephant")["id"]
    slot = _slot(rig, aid, video="h264_2997.mp4")
    assert c.delete(f"/api/videos/{rig.video_id('h264_2997.mp4')}").status_code == 200
    d = c.get(f"/api/analytics/{aid}").json()
    assert d["slots"][0]["id"] == slot["id"] and d["slots"][0]["video"] is None


def test_manual_categories_and_analytics(rig):
    c = rig.client
    cat = c.post("/api/analytics/categories", json={"name": "Retail"}).json()
    assert c.post("/api/analytics/categories", json={"name": "retail"}).status_code == 409
    a = c.post("/api/analytics", json={"name": "Queue length", "category_id": cat["id"]}).json()
    assert c.post("/api/analytics", json={"name": "PPE", "category_id": cat["id"]}).status_code == 409
    assert c.delete(f"/api/analytics/categories/{cat['id']}").status_code == 409
    c.patch(f"/api/analytics/{a['id']}", json={"name": "Queue Length", "description": "Checkout queues"})
    assert _analytics(rig, "Queue Length")["description"] == "Checkout queues"
    assert c.get("/api/analytics").json()[-1]["name"] == "Retail"
    assert c.delete(f"/api/analytics/{a['id']}").status_code == 204
    assert c.delete(f"/api/analytics/categories/{cat['id']}").status_code == 204


def test_stream_create_accepts_a_placeholder(rig):
    c = rig.client
    aid = _analytics(rig, "LineCount")["id"]
    slot = _slot(rig, aid, video="h264_25fps_audio.mp4")
    body = {"name": "door-1", "video_id": rig.video_id("h264_25fps_audio.mp4"), "start_now": False}
    assert c.post("/api/streams", json={**body, "slot_id": 9999}).status_code == 404
    s = c.post("/api/streams", json={**body, "slot_id": slot["id"]}).json()
    assert s["slot_id"] == slot["id"]
    assert _analytics(rig, "LineCount")["streams"] == 1


def test_problem_rule_matches_the_ui():
    """Queued / starting / lead-in are normal; failed or not running when it should be is a problem."""
    from types import SimpleNamespace as S

    from app.api.analytics import _problem
    want_running = {st: _problem(S(status=st, desired_state="RUNNING"))
                    for st in ("RUNNING", "STARTING", "LEAD_IN", "WAITING_FOR_VIEWER", "RESTARTING", "STOPPED", "ERROR")}
    assert want_running == {"RUNNING": False, "STARTING": False, "LEAD_IN": False, "WAITING_FOR_VIEWER": False,
                            "RESTARTING": True, "STOPPED": True, "ERROR": True}
    assert _problem(S(status="ERROR", desired_state="STOPPED"))
    assert not _problem(S(status="STOPPED", desired_state="STOPPED"))
