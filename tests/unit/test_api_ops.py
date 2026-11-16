"""Operations features: resumable uploads, video deletion, metrics, alerts."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from app.services.alerts import AlertSender
from helpers import free_port, wait_for


@pytest.fixture
def rig(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4", "h264_2997.mp4"], mediamtx=False) as r:
        yield r


def test_resumable_upload_survives_a_dropped_connection(rig, fixtures_dir):
    c = rig.client
    data = (fixtures_dir / "h264_23976.mp4").read_bytes()
    s = c.post("/api/videos/uploads", json={"filename": "resumed.mp4", "size": len(data)}).json()
    assert s["offset"] == 0
    half = len(data) // 2
    r = c.put(f"/api/videos/uploads/{s['id']}?offset=0", content=data[:half])
    assert r.status_code == 200 and r.json()["offset"] == half
    # the client's Wi-Fi dropped: it asks where to continue
    assert c.get(f"/api/videos/uploads/{s['id']}").json()["offset"] == half
    # resending from a wrong offset is refused with the right one
    r = c.put(f"/api/videos/uploads/{s['id']}?offset=0", content=data[:10])
    assert r.status_code == 409 and r.json()["detail"]["offset"] == half
    r = c.put(f"/api/videos/uploads/{s['id']}?offset={half}", content=data[half:])
    assert r.status_code == 201 and r.json()["filename"] == "resumed.mp4" and r.json()["video_codec"] == "h264"
    assert (rig.video_dir / "resumed.mp4").read_bytes() == data
    assert list((rig.video_dir / ".uploads").iterdir()) == []
    assert c.get(f"/api/videos/uploads/{s['id']}").status_code == 404  # session finished


def test_resumable_upload_abort_releases_space(rig):
    c = rig.client
    s = c.post("/api/videos/uploads", json={"filename": "big.mp4", "size": 10_000}).json()
    assert c.get("/api/system").json()["upload_in_flight_bytes"] == 10_000
    assert c.delete(f"/api/videos/uploads/{s['id']}").status_code == 204
    assert c.get("/api/system").json()["upload_in_flight_bytes"] == 0
    assert list((rig.video_dir / ".uploads").iterdir()) == []


def test_delete_video_refused_while_used_then_moved_to_trash(rig):
    c = rig.client
    vid = rig.video_id("h264_2997.mp4")
    s = c.post("/api/streams", json={"name": "uses_it", "video_id": vid, "start_now": False}).json()
    r = c.delete(f"/api/videos/{vid}")
    assert r.status_code == 409 and "uses_it" in r.json()["detail"]["message"]
    assert (rig.video_dir / "h264_2997.mp4").exists()
    c.delete(f"/api/streams/{s['id']}")
    r = c.delete(f"/api/videos/{vid}")
    assert r.status_code == 200 and ".trash/" in r.json()["message"]
    assert not (rig.video_dir / "h264_2997.mp4").exists()
    trashed = list((rig.video_dir / ".trash").iterdir())
    assert len(trashed) == 1 and trashed[0].name.endswith("_h264_2997.mp4")  # recoverable for 7 days
    assert c.get(f"/api/videos/{vid}").status_code == 404
    assert "h264_2997.mp4" not in [v["filename"] for v in c.get("/api/videos").json()]  # trash is not scanned
    c.post("/api/videos/refresh")
    assert "h264_2997.mp4" not in [v["filename"] for v in c.get("/api/videos").json()]


def test_delete_video_requires_admin(rig_factory):
    from app.services.auth import UserStore
    r = rig_factory(["h264_2997.mp4"], mediamtx=False, auth=True)
    us = UserStore(r.settings.users_path)
    us.set_user("op", "operator", "op-password")
    with r:
        vid = r.client.get("/api/videos", auth=("op", "op-password")).json()[0]["id"]
        assert r.client.delete(f"/api/videos/{vid}", auth=("op", "op-password")).status_code == 403


def test_prometheus_metrics(rig):
    c = rig.client
    c.post("/api/streams", json={"name": "m1", "video_id": rig.video_id("h264_2997.mp4"), "start_now": False})
    text = c.get("/api/metrics").text
    assert '# TYPE rtspm_stream_up gauge' in text
    assert 'rtspm_stream_up{stream="m1"} 0' in text
    assert "rtspm_mediamtx_up 0" in text  # MediaMTX unreachable in this rig
    assert 'rtspm_streams{status="STOPPED"} 1' in text


class _Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Hook.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_alert_webhook_and_rate_limit():
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Hook.received.clear()
    try:
        a = AlertSender(f"http://127.0.0.1:{port}/hook", min_interval_s=60, app_url="http://rig02/", server_name="RIG-02")
        assert a.notify("stream.source_invalid", "ERROR", "Source file was deleted", "cam1")
        assert not a.notify("stream.source_invalid", "ERROR", "again", "cam1")  # rate-limited
        assert a.notify("stream.source_invalid", "ERROR", "other stream", "cam2")
        assert not a.notify("stream.created", "INFO", "routine", "cam1")  # not alert-worthy
        wait_for(lambda: len(_Hook.received) == 2, 10, desc="webhooks")
        first = _Hook.received[0]
        assert "cam1" in first["text"] and "deleted" in first["text"] and "http://rig02/" in first["text"]
        assert first["event"]["kind"] == "stream.source_invalid"
        a.close()
    finally:
        srv.shutdown()


def test_unreachable_webhook_never_breaks_the_app():
    a = AlertSender(f"http://127.0.0.1:{free_port()}/nothing", min_interval_s=0)
    t0 = time.monotonic()
    assert a.notify("mediamtx.down", "ERROR", "down")
    assert time.monotonic() - t0 < 0.5  # sent in the background
    a.close()
    assert a.failed == 1
