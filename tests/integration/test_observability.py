"""Bitrate, uptime history and alerts against real FFmpeg + MediaMTX."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from helpers import free_port, wait_for

pytestmark = pytest.mark.integration


class _Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        _Hook.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


def test_bitrate_history_and_alert_on_source_loss(rig_factory):
    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Hook.received.clear()
    try:
        with rig_factory(["h264_25fps_audio.mp4"], alert_webhook_url=f"http://127.0.0.1:{port}/") as rig:
            s = rig.create("observed", "h264_25fps_audio.mp4")
            rig.wait_status(s["id"], "RUNNING")
            reader = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i",
                                       rig.url("observed"), "-c", "copy", "-f", "null", "-"])
            try:
                rt = wait_for(lambda: (d := rig.stream(s["id"])["runtime"])["egress_bps"] and d["ingest_bps"]
                              and d, 20, desc="bitrate")
                assert 20_000 < rt["ingest_bps"] < 5_000_000, rt  # the 640x360 clip is ~150 kbit/s
                assert rt["egress_bps"] > 0  # first window may cover only part of the reader's session
                sysinfo = rig.client.get("/api/system").json()
                assert sysinfo["rtsp_egress_bps"] > 0 and sysinfo["rtsp_ingest_bps"] > 0
            finally:
                reader.terminate()
                reader.wait()

            h = rig.client.get(f"/api/streams/{s['id']}/history?hours=1").json()
            statuses = [x["status"] for x in h["segments"]]
            assert statuses[-1] == "RUNNING" and "STARTING" in statuses
            assert 0 < h["running_percent"] <= 100

            os.unlink(rig.video_dir / "h264_25fps_audio.mp4")
            rig.wait_status(s["id"], "ERROR", 10)
            msg = wait_for(lambda: next((m for m in _Hook.received
                                         if m["event"]["kind"] == "stream.source_invalid"), None), 10, desc="alert")
            assert "observed" in msg["text"] and "deleted" in msg["text"]
            h = rig.client.get(f"/api/streams/{s['id']}/history?hours=1").json()
            assert h["segments"][-1]["status"] == "ERROR"
    finally:
        srv.shutdown()
