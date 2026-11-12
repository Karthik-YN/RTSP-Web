"""Phase 5 + 6: health test, runtime capacity limits, in-app database backup."""

import time

from app.services.health_test import FAIL, PASS, SKIP, WARN


def _by_id(report):
    return {c["id"]: c for c in report["checks"]}


def test_full_health_test_reads_running_streams_and_skips_held_ones(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"])
    with rig:
        c = rig.client
        vid = rig.video_id("h264_25fps_audio.mp4")
        assert c.post("/api/streams?wait=true", json={"name": "hc1", "video_id": vid}).json()["status"] == "RUNNING"
        held = c.post("/api/streams?wait=true", json={"name": "hc-hold", "video_id": vid, "lead_in_mode": "hold",
                                                       "lead_in_s": 2}).json()
        assert held["status"] == "WAITING_FOR_VIEWER"
        rep = c.post("/api/health/test").json()
        checks = _by_id(rep)
        assert rep["mode"] == "full"
        assert checks["mediamtx"]["status"] == PASS
        assert checks["exposure"]["status"] in (PASS, WARN, SKIP)  # never FAIL: internals are loopback-only
        assert checks["database"]["status"] == PASS and checks["writable"]["status"] == PASS
        assert checks["backup"]["status"] == PASS  # the app backed itself up at start
        assert checks["stream:hc1"]["status"] == PASS and checks["stream:hc1"]["data"]["packets"] > 10
        assert checks["stream:hc-hold"]["status"] == SKIP  # reading it would start its video
        # the test did not wake the held stream
        assert c.get(f"/api/streams/{held['id']}").json()["status"] == "WAITING_FOR_VIEWER"
        assert rep["overall"] in (PASS, WARN) and sum(rep["summary"].values()) == len(rep["checks"])
        assert c.get("/api/health/test/last").json()["full"]["started_at"] == rep["started_at"]
        ev = [e for e in c.get("/api/events?limit=30").json() if e["kind"] == "health.test"]
        assert ev and ev[0]["message"].startswith("Health test ")


def test_quick_test_opens_no_rtsp_sessions(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"])
    with rig:
        c = rig.client
        vid = rig.video_id("h264_25fps_audio.mp4")
        c.post("/api/streams?wait=true", json={"name": "hc2", "video_id": vid})
        before = rig.client.get("/api/streams").json()[0]["runtime"]
        quick = c.post("/api/health/test?quick=true").json()
        assert quick["mode"] == "quick" and not any(k.startswith("stream") for k in _by_id(quick))
        assert not [e for e in c.get("/api/events?limit=30").json() if e["kind"] == "stream.viewer_connected"]
        assert before is not None


def test_a_stream_that_should_run_but_does_not_fails_the_test(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False)
    with rig:
        c = rig.client
        vid = rig.video_id("h264_25fps_audio.mp4")
        sid = c.post("/api/streams", json={"name": "hc3", "video_id": vid, "start_now": False}).json()["id"]
        from app.models import Stream
        with rig.ctx.db.session() as s:
            row = s.get(Stream, sid)
            row.desired_state, row.status = "RUNNING", "RESTARTING"
        rep = c.post("/api/health/test").json()
        checks = _by_id(rep)
        assert checks["stream:hc3"]["status"] == FAIL and "RESTARTING" in checks["stream:hc3"]["detail"]
        with rig.ctx.db.session() as s:
            s.get(Stream, sid).status = "STARTING"  # queued for its 2 s start slot: on track
        assert _by_id(c.post("/api/health/test").json())["stream:hc3"]["status"] == SKIP
        assert checks["mediamtx"]["status"] == FAIL and rep["overall"] == FAIL
        ev = [e for e in c.get("/api/events?limit=30").json() if e["kind"] == "health.test"]
        assert ev[0]["level"] == "ERROR"


def test_capacity_limit_is_changed_at_runtime_and_survives_a_restart(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False)
    with rig:
        c = rig.client
        cap = c.get("/api/settings/capacity").json()
        assert cap["max_active_streams"]["value"] == rig.settings.max_active_streams
        assert not cap["max_active_streams"]["overridden"]
        assert c.put("/api/settings/capacity", json={"max_active_streams": 0}).status_code == 422
        assert c.put("/api/settings/capacity", json={"max_active_streams": 501}).status_code == 422
        r = c.put("/api/settings/capacity", json={"max_active_streams": 1, "max_transcode_streams": 0}).json()
        assert r["max_active_streams"]["value"] == 1 and r["max_active_streams"]["overridden"]
        assert c.get("/api/config").json()["max_active_streams"] == 1
        assert c.get("/api/system").json()["streams"]["max_active"] == 1
    with rig:  # restart: the saved limit is kept
        c = rig.client
        assert c.get("/api/settings/capacity").json()["max_active_streams"]["value"] == 1
        r = c.put("/api/settings/capacity", json={"reset": ["max_active_streams"]}).json()
        assert r["max_active_streams"]["value"] == rig.settings.max_active_streams
        assert not r["max_active_streams"]["overridden"]
        assert any(e["kind"] == "settings.capacity" for e in c.get("/api/events?limit=20").json())


def test_capacity_limit_is_enforced(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"])
    with rig:
        c = rig.client
        c.put("/api/settings/capacity", json={"max_active_streams": 1})
        vid = rig.video_id("h264_25fps_audio.mp4")
        assert c.post("/api/streams?wait=true", json={"name": "cap1", "video_id": vid}).status_code == 201
        r = c.post("/api/streams", json={"name": "cap2", "video_id": vid})
        assert r.status_code == 409 and "Capacity reached" in r.json()["detail"]


def test_backup_now_and_retention(rig_factory):
    rig = rig_factory([], mediamtx=False, backup_keep=2)
    with rig:
        c = rig.client
        names = []
        for _ in range(3):
            names.append(c.post("/api/backup").json()["name"])
            time.sleep(1.05)  # backup folders are named by the second
        kept = sorted(p.name for p in rig.settings.backup_path.iterdir())
        assert kept == sorted(names[-2:])
        db = rig.settings.backup_path / names[-1] / "rtsp-manager.db"
        import sqlite3
        assert sqlite3.connect(db).execute("PRAGMA user_version").fetchone()[0] >= 6
        assert oct(db.stat().st_mode & 0o777) == "0o600"
