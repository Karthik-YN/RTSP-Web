"""Video naming convention WHAT_WHERE_NN[_tag].ext: rules, upload check, codes and safe renames (no streams)."""

import pytest

from app.services import naming
from app.services.converter import is_copy, target_rel

CODES = {"ALPR", "FALL", "JAM", "CROWD", "MISC"}


@pytest.mark.parametrize("name", [
    "ALPR_toll-s1_01.mp4", "FALL_office_01.avi", "JAM_dhaka_01_web.mp4", "CROWD_railway_01_h264.mp4",
    "ALPR_toll-s3_03_12fps.mp4", "ALPR_mlff-6_01_720p_16x9.mp4", "MISC_tbd_99.MP4",
])
def test_valid_names(name):
    r = naming.check(name, CODES)
    assert r.ok, r.errors


@pytest.mark.parametrize("name, says", [
    ("gate.mp4", "three parts"),
    ("Copy of tirupathi test bench.mp4", "no spaces"),
    ("alpr_toll-s1_01.mp4", "UPPERCASE"),
    ("PPE_site_01.mp4", "not an analytics code"),
    ("ALPR_Toll_01.mp4", "lowercase"),
    ("ALPR_a-very-long-place-name_01.mp4", "longer than 14"),
    ("ALPR_toll_1.mp4", "two-digit"),
    ("ALPR_toll_00.mp4", "two-digit"),
    ("ALPR_toll_01_final.mp4", "Unknown tag"),
    ("ALPR_toll_01", "extension"),
])
def test_invalid_names_say_why(name, says):
    r = naming.check(name, CODES)
    assert not r.ok and says in " ".join(r.errors)


def test_parse_next_number_and_build():
    p = naming.parse("ALPR_toll-s1_02_720p.mp4")
    assert (p.what, p.where, p.nn, p.tags, p.ext) == ("ALPR", "toll-s1", 2, ["720p"], ".mp4")
    existing = ["ALPR_toll-s1_01.mp4", "ALPR_toll-s1_02_720p.mp4", "ALPR_toll-s2_01.mp4", "gate.mp4"]
    assert naming.next_number("ALPR", "toll-s1", existing) == 3
    assert naming.next_number("ALPR", "office", existing) == 1
    assert naming.build("JAM", "dhaka", 1, ["web"], "mp4") == "JAM_dhaka_01_web.mp4"
    assert naming.slug("Toll Plaza S1 (east)") == "toll-plaza-s1"
    assert naming.slug("tirupathi-bench") == "tirupathi-bench"[:14].rsplit("-", 1)[0] == "tirupathi"
    assert naming.slug("supermarket-counter") == "supermarket"
    with pytest.raises(ValueError):
        naming.next_number("AB", "b", [f"AB_b_{n:02d}.mp4" for n in range(1, 100)])


def test_suggest_from_an_old_name():
    assert naming.suggest("ALPR_2 first5min h264.mp4", CODES, []) == "ALPR_first5min_01_h264.mp4"
    s = naming.suggest("fall office room h264.mp4", CODES, ["FALL_office-room_01.mp4"])
    assert s == "FALL_office-room_02_h264.mp4"
    assert naming.suggest("random clip.mov", CODES, []) == "MISC_random-clip_01.mov"
    assert naming.check(naming.suggest("Copy of tirupathi test bench.mp4", CODES, []), CODES).ok


def test_auto_code_is_short_unique_and_never_misc():
    assert naming.auto_code("QueueLength", set()) == "QUEUELEN"
    assert naming.auto_code("Queue Length", set()) == "QL"
    assert naming.auto_code("Queue Length", {"QL"}) == "QL2"
    assert naming.auto_code("misc", set()) != "MISC"
    for name in ("x", "9 lives", "Ünïcode Thing"):
        code = naming.auto_code(name, set())
        assert naming.check(f"{code}_a_01.mp4").ok, code


def test_converted_copies_use_tags_for_convention_names():
    assert target_rel("ALPR_toll-s1_01.mp4", "720p") == "ALPR_toll-s1_01_720p.mp4"
    assert target_rel("ALPR_toll-s1_01_4k_web.mp4", "720p") == "ALPR_toll-s1_01_web_720p.mp4"
    assert target_rel("FALL_office_01.avi", "h264") == "FALL_office_01_h264.mp4"
    assert target_rel("CROWD_x_01.mp4", "16x9") == "CROWD_x_01_720p_16x9.mp4"
    assert target_rel("sub/old name.mp4", "720p") == "sub/old name (720p).mp4"  # other names: as before
    assert is_copy("ALPR_toll-s1_01_720p.mp4") and is_copy("old name (720p).mp4")
    assert not is_copy("ALPR_toll-s1_01.mp4")


# ------------------------------------------------------------------ API (no MediaMTX needed)
def test_upload_is_refused_with_a_suggestion_when_enforced(rig_factory, fixtures_dir):
    data = (fixtures_dir / "h264_2997.mp4").read_bytes() + b"unique-upload"  # not a copy of the fixture (duplicates are refused)
    with rig_factory([], mediamtx=False, naming_mode="enforce") as rig:
        c = rig.client
        r = c.post("/api/videos/upload?filename=my clip.mp4", content=data)
        assert r.status_code == 422
        d = r.json()["detail"]
        assert "naming convention" in d["message"] and d["suggestion"].endswith("_01.mp4")
        assert c.post("/api/videos/upload/check", json={"filename": "x.mp4", "size": 10}).status_code == 422
        assert c.post("/api/videos/uploads", json={"filename": "x.mp4", "size": 10}).status_code == 422

        r = c.post("/api/videos/upload?filename=ALPR_toll-s1_01.mp4&original_name=my clip.mp4", content=data)
        assert r.status_code == 201, r.text
        v = r.json()
        assert v["name_parts"] == {"what": "ALPR", "where": "toll-s1", "nn": 1, "tags": []}
        assert v["original_name"] == "my clip.mp4"

        # next free number, and the name checker
        s = c.post("/api/naming/suggest", json={"what": "ALPR", "where": "Toll S1", "ext": ".mp4"}).json()
        assert s["name"] == "ALPR_toll-s1_02.mp4" and s["ok"]
        s = c.post("/api/naming/suggest", json={"what": "ALPR", "where": "toll-s1", "reserve": ["ALPR_toll-s1_02.mp4"]})
        assert s.json()["name"] == "ALPR_toll-s1_03.mp4"
        chk = c.post("/api/naming/check", json={"name": "ALPR_toll-s1_01.mp4"}).json()
        assert chk["ok"] and chk["taken"]
        chk = c.post("/api/naming/check", json={"name": "NOPE_x_01.mp4"}).json()
        assert not chk["ok"] and chk["suggestion"]

        # warn mode (live switch, no restart): any name is accepted, the library flags it
        assert c.put("/api/settings/naming", json={"mode": "warn"}).json()["mode"] == "warn"
        r = c.post("/api/videos/upload?filename=other.mp4", content=data + b"-other")  # another video, not a duplicate
        assert r.status_code == 201 and r.json()["name_parts"] is None
        assert c.get("/api/naming").json()["mode"] == "warn"


def test_every_analytics_has_a_code(rig_factory):
    with rig_factory([], mediamtx=False) as rig:
        c = rig.client
        conv = c.get("/api/naming").json()
        codes = {x["code"]: x["analytics"] for x in conv["codes"]}
        assert codes["ALPR"] == "VehicleALPR" and codes["FALL"] == "SlipAndFall" and "MISC" in codes
        tree = c.get("/api/analytics").json()
        assert all(a["code"] for cat in tree for a in cat["analytics"])
        cat = tree[0]["id"]
        a = c.post("/api/analytics", json={"name": "Red light", "category_id": cat, "code": "RLVD"}).json()
        assert a["code"] == "RLVD"
        b = c.post("/api/analytics", json={"name": "Queue Length", "category_id": cat}).json()
        assert b["code"] == "QL"
        assert c.post("/api/analytics", json={"name": "Dup", "category_id": cat, "code": "RLVD"}).status_code == 409
        assert c.patch(f"/api/analytics/{b['id']}", json={"code": "MISC"}).status_code == 422
        assert c.patch(f"/api/analytics/{b['id']}", json={"code": "QUEUE"}).json()["code"] == "QUEUE"
        assert "RLVD" in {x["code"] for x in c.get("/api/naming").json()["codes"]}


def test_rename_without_streams(rig_factory):
    with rig_factory(["h264_2997.mp4", "h264_23976.mp4"], mediamtx=False) as rig:
        c = rig.client
        vid = rig.video_id("h264_2997.mp4")
        url = f"/api/videos/{vid}/rename"
        assert c.post(url, json={"name": "bad name.mp4"}).status_code == 422
        assert c.post(url, json={"name": "ALPR_toll-s1_01.mkv"}).status_code == 400  # extension must stay
        r = c.post(url, json={"name": "ALPR_toll-s1_01", "dry_run": True}).json()  # extension added
        assert r["new"] == "ALPR_toll-s1_01.mp4" and (rig.video_dir / "h264_2997.mp4").exists()
        r = c.post(url, json={"name": "ALPR_toll-s1_01.mp4"})
        assert r.status_code == 200, r.text
        v = r.json()["video"]
        assert v["filename"] == "ALPR_toll-s1_01.mp4" and v["previous_names"] == ["h264_2997.mp4"]
        assert (rig.video_dir / "ALPR_toll-s1_01.mp4").exists() and not (rig.video_dir / "h264_2997.mp4").exists()
        assert c.get("/api/videos?search=2997").json()[0]["id"] == vid  # old name still finds it
        # a rescan sees the same video, not a new one
        rig.client.portal.call(rig.ctx.library.scan)
        assert len(c.get("/api/videos").json()) == 2

        other = rig.video_id("h264_23976.mp4")
        assert c.post(f"/api/videos/{other}/rename", json={"name": "ALPR_toll-s1_01.mp4"}).status_code == 409
        # batch: dry run reports clashes inside the batch, changes nothing
        items = [{"video_id": other, "name": "FALL_office_01.mp4"}, {"video_id": vid, "name": "FALL_office_01.mp4"}]
        b = c.post("/api/videos/rename-batch", json={"items": items}).json()
        assert b["dry_run"] and b["ok"] == 1 and b["failed"] == 1 and "clashes" in b["results"][1]["error"]
        assert (rig.video_dir / "h264_23976.mp4").exists()
        b = c.post("/api/videos/rename-batch", json={"items": items[:1], "dry_run": False}).json()
        assert b["ok"] == 1 and (rig.video_dir / "FALL_office_01.mp4").exists()
        ev = [e for e in c.get("/api/events?limit=50").json() if e["kind"] == "video.renamed"]
        assert len(ev) == 2
