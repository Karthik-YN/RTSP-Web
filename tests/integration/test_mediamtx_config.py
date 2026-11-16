"""The shipped MediaMTX config must expose only RTSP to the network."""

import subprocess
import time

import psutil
import pytest

from helpers import REPO, free_port, have_binaries, mediamtx_config, wait_for

pytestmark = pytest.mark.integration


def test_only_rtsp_listens_on_non_loopback(tmp_path):
    if not have_binaries("mediamtx"):
        pytest.skip("mediamtx not installed")
    rtsp, api, metrics, hls = free_port(), free_port(), free_port(), free_port()
    cfg = tmp_path / "m.yml"
    cfg.write_text(mediamtx_config(rtsp, api, metrics, hls_port=hls))
    proc = subprocess.Popen(["mediamtx", str(cfg)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        def listening():
            try:
                conns = psutil.Process(proc.pid).net_connections(kind="inet")
            except psutil.Error:
                return None
            ls = [c for c in conns if c.status == psutil.CONN_LISTEN or c.type == 2]
            return ls if any(c.laddr.port == api for c in ls) else None

        conns = wait_for(listening, 10, desc="MediaMTX listening")
        time.sleep(0.5)
        conns = listening()
        exposed = sorted({c.laddr.port for c in conns if c.laddr.ip not in ("127.0.0.1", "::1")})
        loopback = sorted({c.laddr.port for c in conns if c.laddr.ip in ("127.0.0.1", "::1")})
        assert exposed == [rtsp], f"unexpected network listeners: {exposed}"
        assert set(loopback) == {api, metrics, hls}
        assert proc.poll() is None
    finally:
        proc.terminate()
        proc.wait(5)


def test_config_disables_every_optional_server():
    text = (REPO / "deployment" / "mediamtx.yml").read_text()
    for key in ("rtmp: no", "webrtc: no", "srt: no", "moq: no", "pprof: no", "playback: no"):
        assert f"\n{key}" in text, key
    assert "apiAddress: 127.0.0.1:" in text and "metricsAddress: 127.0.0.1:" in text
    assert "hlsAddress: 127.0.0.1:" in text  # preview goes through the backend only
