from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helpers import FIXTURES, REPO, free_port, have_binaries, link_or_copy, mediamtx_config, wait_for

REQUIRED_FIXTURES = ["h264_25fps_audio.mp4", "h264_2997.mp4", "long_2h.mp4", "hevc.mp4", "vp9.webm",
                     "av1.mkv", "h264_10bit.mp4", "h264_vfr.mkv", "corrupt.mp4"]


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    if not have_binaries("ffmpeg", "ffprobe"):
        pytest.skip("ffmpeg/ffprobe not installed")
    if not all((FIXTURES / f).exists() for f in REQUIRED_FIXTURES):
        subprocess.run([str(REPO / "scripts" / "generate_test_videos.sh"), str(FIXTURES)], check=True,
                       capture_output=True)
    return FIXTURES


class Rig:
    """A complete, isolated backend instance (app + its own MediaMTX)."""

    def __init__(self, base: Path, videos: list[str] = (), mediamtx: bool = True, auth: bool = False,
                 **overrides):
        from app.config import Settings

        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        ports_file = base / "ports.json"
        if ports_file.exists():
            self.ports = json.loads(ports_file.read_text())
        else:
            self.ports = {"rtsp": free_port(), "api": free_port(), "metrics": free_port(), "hls": free_port()}
            ports_file.write_text(json.dumps(self.ports))
        self.video_dir = base / "videos"
        for v in videos:
            link_or_copy(FIXTURES / v, self.video_dir / v)
        cfg_path = base / "mediamtx.yml"
        cfg_path.write_text(mediamtx_config(self.ports["rtsp"], self.ports["api"], self.ports["metrics"],
                                            hold_s=overrides.pop("hold_s", 60), hls_port=self.ports["hls"]))
        params = dict(
            data_dir=base / "data", video_dir=self.video_dir,
            public_rtsp_host="rig.test", public_rtsp_port=self.ports["rtsp"],
            publish_rtsp_base=f"rtsp://127.0.0.1:{self.ports['rtsp']}",
            mediamtx_api_url=f"http://127.0.0.1:{self.ports['api'] if mediamtx else free_port()}",
            mediamtx_hls_url=f"http://127.0.0.1:{self.ports['hls']}",
            mediamtx_control="process" if mediamtx else "external",
            mediamtx_config=cfg_path, mediamtx_bin="mediamtx",
            health_interval_s=1.0, health_fail_threshold=2, startup_timeout_s=15,
            settle_seconds=0, scan_interval_s=3600, auth_enabled=auth, secret_key="test-secret",
            disk_reserve_bytes=0, disk_reserve_percent=0, restart_backoff_initial_s=0.5,
            source_check_interval_s=1.0, monitor_interval_s=1.0, recovery_mediamtx_wait_s=20,
            frontend_dist=base / "no-frontend",
            # Start spacing (2 s between starts) is tested on its own; elsewhere it
            # would only slow the suite down.
            start_spacing_s=0,
            # New streams default to a 5 s lead-in in production; tests that are not
            # about the lead-in keep today's start-at-once behaviour.
            default_lead_in_mode="none",
            # Production scales new streams to the standard 1280x720; the fixtures are
            # small 16:9 clips, so tests not about output size stream them as they are.
            output_default="original",
        )
        params.update(overrides)
        self.settings = Settings(_env_file=None, **params)
        self.mediamtx = mediamtx

    def __enter__(self) -> "Rig":
        from app.main import create_app

        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.ctx = self.app.state.ctx
        if self.mediamtx:
            wait_for(lambda: self.client.get("/api/mediamtx/status").json()["state"] == "UP", 20,
                     desc="MediaMTX up")
        # scan on the app's event loop (works whether or not auth is enabled)
        self.client.portal.call(self.ctx.library.scan)
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)

    # ------------------------------------------------------------ helpers
    @property
    def rtsp_base(self) -> str:
        return f"rtsp://127.0.0.1:{self.ports['rtsp']}"

    def url(self, name: str) -> str:
        return f"{self.rtsp_base}/{name}"

    def video_id(self, filename: str) -> int:
        for v in self.client.get("/api/videos").json():
            if v["filename"] == filename:
                return v["id"]
        raise KeyError(filename)

    def create(self, name: str, filename: str, expect: int = 201, **kw) -> dict:
        body = {"name": name, "video_id": self.video_id(filename), **kw}
        r = self.client.post("/api/streams", json=body)
        assert r.status_code == expect, r.text
        return r.json()

    def stream(self, sid: int) -> dict:
        return self.client.get(f"/api/streams/{sid}").json()

    def wait_status(self, sid: int, statuses, timeout: float = 20) -> dict:
        if isinstance(statuses, str):
            statuses = {statuses}
        seen = {}

        def check():
            s = self.stream(sid)
            seen.update(status=s.get("status"), detail=s.get("status_detail"), error=s.get("last_error"))
            return s if s["status"] in statuses else None

        try:
            return wait_for(check, timeout, desc=f"stream {sid} in {statuses}")
        except AssertionError as exc:
            raise AssertionError(f"{exc}; last seen {seen}") from None

    def run_dir_marker(self) -> str:
        return str(self.settings.run_path / "streams")


@pytest.fixture
def rig_factory(tmp_path, fixtures_dir):
    rigs: list[Rig] = []

    def make(videos=(), base: Path | None = None, **kw) -> Rig:
        if kw.get("mediamtx", True) and not have_binaries("mediamtx"):
            pytest.skip("mediamtx not installed")
        rig = Rig(base or tmp_path / f"rig{len(rigs)}", list(videos), **kw)
        rigs.append(rig)
        return rig

    yield make
    time.sleep(0.1)
