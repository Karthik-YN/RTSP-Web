"""Library videos in analytics: name-based suggestions, assigning, links in the library."""

import os
import shutil
import time
from types import SimpleNamespace

import pytest

from app.services.analytics_suggest import base_name, match, suggest, words


@pytest.mark.parametrize("name, analytics, word", [
    ("Fire.mp4", "FireSmoke", "fire"),
    ("forest_smoke_02.mp4", "FireSmoke", "smoke"),
    ("FireSmoke-cam1.mp4", "FireSmoke", "fire"),
    ("PPE_gate.mp4", "PPE", "ppe"),
    ("site helmets.mov", "PPE", "helmet"),          # plural
    ("crowd2.mp4", "CrowdDensity", "crowd"),
    ("bag1.mp4", "AbandonedObject", "bag"),
    ("ANPR highway.mp4", "videoANPR", "anpr"),
    ("chargingStation.mp4", "Charging station", "charging"),
    ("my Forklift test.mp4", "Forklift", "Forklift"),   # an analytics added later matches its own name
])
def test_match(name, analytics, word):
    assert match(name, analytics) == word


@pytest.mark.parametrize("name, analytics", [
    ("every frame.mp4", "Charging station"),   # "ev" only as a whole word
    ("firewall.mp4", "FireSmoke"),
    ("happen.mp4", "PPE"),
    ("lobby.mp4", "Parking"),
])
def test_no_match(name, analytics):
    assert match(name, analytics) is None


def test_words_and_base_name():
    assert words("crowd2_PPE-FireSmoke.mp4") == ["crowd", "ppe", "fire", "smoke", "mp"]
    assert base_name("sub/Fire (720p).mp4") == "sub/Fire"
    assert base_name("phone (720p 16x9).mp4") == "phone"
    assert base_name("Fire.mp4") == "Fire"


def test_suggest_picks_one_per_video_family():
    def v(i, rel, wide=True):
        return SimpleNamespace(id=i, rel_path=rel, status="READY", compat_mode="copy", is_16_9=wide)
    fire = SimpleNamespace(id=10, name="FireSmoke")
    crowd = SimpleNamespace(id=11, name="CrowdDensity")
    vids = [v(1, "Fire.mp4"), v(2, "Fire (720p).mp4"), v(3, "crowd2.mp4", wide=False), v(4, "crowd2 (720p 16x9).mp4"),
            v(5, "lobby.mp4")]
    out = suggest(vids, [fire, crowd], linked=set())
    # 16:9 original over its 720p copy; the 16:9 copy over a portrait original
    assert [(s["analytics_name"], s["video_id"]) for s in out] == [("CrowdDensity", 4), ("FireSmoke", 1)]
    # already linked (even through a copy): no suggestion
    assert suggest(vids, [fire], linked={(10, 2)}) == []


def _add(rig, fixtures_dir, name, src="h264_2997.mp4"):
    shutil.copy(fixtures_dir / src, rig.video_dir / name)
    old = time.time() - 3600
    os.utime(rig.video_dir / name, (old, old))


def _aid(c, name):
    return next(a["id"] for cat in c.get("/api/analytics").json() for a in cat["analytics"] if a["name"] == name)


def test_suggest_assign_and_library_links(rig_factory, fixtures_dir):
    with rig_factory([], mediamtx=False) as rig:
        c = rig.client
        for n in ("Fire.mp4", "PPE gate.mp4", "lobby.mp4"):
            _add(rig, fixtures_dir, n)
        c.post("/api/videos/refresh")
        fire_id, ppe_id = rig.video_id("Fire.mp4"), rig.video_id("PPE gate.mp4")
        fs, ppe, crowd = _aid(c, "FireSmoke"), _aid(c, "PPE"), _aid(c, "CrowdDensity")

        sug = c.get("/api/analytics/suggestions").json()
        assert {(s["rel_path"], s["analytics_name"]) for s in sug} == {("Fire.mp4", "FireSmoke"), ("PPE gate.mp4", "PPE")}
        assert all(v["analytics"] == [] for v in c.get("/api/videos").json())

        # nothing is linked until assigned; assign is repeat-safe
        r = c.post("/api/analytics/assign", json={"video_ids": [fire_id], "analytics_ids": [fs, crowd]})
        assert r.status_code == 200, r.text
        assert len(r.json()["created"]) == 2 and r.json()["skipped"] == 0
        r = c.post("/api/analytics/assign", json={"video_ids": [fire_id, fire_id], "analytics_ids": [fs]})
        assert r.json() == {"created": [], "skipped": 1}

        lib = {v["filename"]: v for v in c.get("/api/videos").json()}
        assert [a["name"] for a in lib["Fire.mp4"]["analytics"]] == ["CrowdDensity", "FireSmoke"]
        assert c.get(f"/api/videos/{fire_id}").json()["analytics"] == lib["Fire.mp4"]["analytics"]
        assert [(s["rel_path"], s["analytics_name"]) for s in c.get("/api/analytics/suggestions").json()] \
            == [("PPE gate.mp4", "PPE")]

        # the analytics page and taskbar see the new placeholder, titled after the file
        d = c.get(f"/api/analytics/{fs}").json()
        assert [(s["title"], s["video"]["id"]) for s in d["slots"]] == [("Fire", fire_id)]
        node = next(a for cat in c.get("/api/analytics").json() for a in cat["analytics"] if a["id"] == fs)
        assert node["slots"] == 1 and node["videos"] == 1

        assert c.post("/api/analytics/assign", json={"video_ids": [ppe_id], "analytics_ids": [ppe]}).status_code == 200
        assert c.get("/api/analytics/suggestions").json() == []

        # unknown ids
        r = c.post("/api/analytics/assign", json={"video_ids": [9999], "analytics_ids": [fs]})
        assert r.status_code == 404 and "9999" in r.json()["detail"]
        assert c.post("/api/analytics/assign", json={"video_ids": [], "analytics_ids": [fs]}).status_code == 422
        events = [e["kind"] for e in c.get("/api/events?limit=50").json()]
        assert "analytics.assign" in events


def test_viewers_cannot_assign(rig_factory, fixtures_dir):
    from app.services.auth import UserStore
    rig = rig_factory(["h264_2997.mp4"], mediamtx=False, auth=True)
    UserStore(rig.settings.users_path).set_user("victor", "viewer", "victor-password")
    with rig:
        auth = ("victor", "victor-password")
        vid = rig.client.get("/api/videos", auth=auth).json()[0]["id"]
        aid = _aid(SimpleNamespace(get=lambda p: rig.client.get(p, auth=auth)), "PPE")
        r = rig.client.post("/api/analytics/assign", json={"video_ids": [vid], "analytics_ids": [aid]}, auth=auth)
        assert r.status_code == 403
        assert rig.client.get("/api/analytics/suggestions", auth=auth).status_code == 200
