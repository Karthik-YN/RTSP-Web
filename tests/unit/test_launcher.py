"""The app.py launcher's pure helpers (loaded by path: 'app' is the backend package)."""

import hashlib
import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from helpers import REPO, free_port

spec = importlib.util.spec_from_file_location("rig02_launcher", REPO / "app.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def test_env_file_parsing(tmp_path: Path):
    f = tmp_path / ".env"
    f.write_text("# comment\n\nRTSPM_PUBLIC_RTSP_HOST=rig02.local\nA='quoted'\nnot a setting\n")
    assert launcher.load_env_file(f) == {"RTSPM_PUBLIC_RTSP_HOST": "rig02.local", "A": "quoted"}
    assert launcher.load_env_file(tmp_path / "missing") == {}


def test_checksum_accepts_binary_mode_lines_and_rejects_tampering():
    data = b"mediamtx"
    good = f"{hashlib.sha256(data).hexdigest()} *m_linux_amd64.tar.gz\nabc *other.tar.gz\n"
    assert launcher.verify_checksum(data, good, "m_linux_amd64.tar.gz")
    assert not launcher.verify_checksum(b"tampered", good, "m_linux_amd64.tar.gz")
    assert not launcher.verify_checksum(data, good, "not_listed.tar.gz")


def test_no_browser_attempt_over_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    assert launcher.can_open_browser() is False


def test_recognises_our_app_but_not_another_program():
    class H(BaseHTTPRequestHandler):
        body = b""

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(H.body)

        def log_message(self, *a):
            pass

    port = free_port()
    srv = HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        H.body = json.dumps({"app": "ok", "status": "ok"}).encode()
        assert launcher.app_health(port)["status"] == "ok"
        H.body = b"<html>some other server</html>"
        assert launcher.app_health(port) is None
        assert launcher.port_open(port) is True
    finally:
        srv.shutdown()
    assert launcher.app_health(free_port()) is None


def test_host_is_remembered_across_restarts(tmp_path: Path, monkeypatch):
    """A machine on two networks must not flip its RTSP host on 'restart' (no --host given)."""
    import argparse
    monkeypatch.setattr(launcher, "lan_ip", lambda: "192.168.68.105")
    env, saved = tmp_path / ".env", tmp_path / "launcher-host"
    assert launcher.public_host(argparse.Namespace(host=None), env, saved) == "192.168.68.105"
    assert launcher.public_host(argparse.Namespace(host="192.168.29.108"), env, saved) == "192.168.29.108"
    assert launcher.public_host(argparse.Namespace(host=None), env, saved) == "192.168.29.108"
    env.write_text("RTSPM_PUBLIC_RTSP_HOST=rig02.lan\n")  # .env wins over the remembered one
    assert launcher.public_host(argparse.Namespace(host=None), env, saved) == "rig02.lan"
