"""Application settings.

Every value can be overridden with an environment variable prefixed ``RTSPM_``
(for example ``RTSPM_PUBLIC_RTSP_HOST=rig02.vidatt.local``) or through the
``.env`` file referenced by ``RTSPM_ENV_FILE``. Nothing about the office network
(IP addresses, host names, credentials) is hardcoded here.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# List settings accept "a,b,c" in env files; NoDecode stops pydantic-settings
# from requiring JSON before our validator runs.
CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTSPM_",
        env_file=os.environ.get("RTSPM_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ paths
    data_dir: Path = Path("/var/lib/rtsp-manager")
    video_dir: Path = Path("/data/rtsp-manager/videos")
    log_dir: Path | None = None  # default: <data_dir>/logs
    thumbnail_dir: Path | None = None  # default: <data_dir>/thumbnails
    run_dir: Path | None = None  # default: <data_dir>/run (playlists)
    # Lead-in slates. MediaMTX reads these files, so on the systemd install
    # (MediaMTX runs as its own user) this is a directory it can reach.
    slate_dir: Path | None = None  # default: <data_dir>/slates
    db_path: Path | None = None  # default: <data_dir>/rtsp-manager.db
    frontend_dist: Path | None = None  # built React app; served at "/"

    # --------------------------------------------------------------- network
    # Host name / IP that office laptops and the edge use to reach the server.
    # Used only to *display* RTSP URLs. Must be set in production.
    public_rtsp_host: str = "localhost"
    public_rtsp_port: int = 8554
    # Extra names/IPs that users on other subnets (VPN, other LANs) use to reach
    # this server, offered in the dashboard's address picker and URL exports.
    rtsp_alt_hosts: CsvList = []
    address_check_interval_s: float = 60.0  # how often to look for an IP / DNS change
    # Compare the RTSP host with this machine's own IPs. Off in Docker (bridge
    # network): the container only sees its private address, not the host's.
    address_watch_interfaces: bool = True
    # Cloudflare Access (remote access through a Cloudflare Tunnel). Team name or
    # domain (acme / acme.cloudflareaccess.com) and the Access application's AUD tag.
    # Requests that arrive through Cloudflare without a valid token are refused,
    # even when these are unset (fail closed).
    cf_access_team: str = ""
    cf_access_aud: CsvList = []
    # tunnel: only requests that came through Cloudflare need a token (LAN is open)
    # all: every request needs a token (rig reachable only through the tunnel)
    cf_access_mode: Literal["tunnel", "all"] = "tunnel"
    # Where FFmpeg publishes to (always the local MediaMTX instance).
    publish_rtsp_base: str = "rtsp://127.0.0.1:8554"
    mediamtx_api_url: str = "http://127.0.0.1:9997"
    mediamtx_hls_url: str = "http://127.0.0.1:8888"  # loopback; proxied for the dashboard preview

    # ------------------------------------------------------- MediaMTX control
    # systemd: ask systemd (via polkit) to start/restart mediamtx.service
    # process: the backend launches MediaMTX itself (development / tests)
    # external: detect only, never try to start it
    mediamtx_control: Literal["systemd", "process", "external"] = "systemd"
    mediamtx_service_name: str = "mediamtx.service"
    systemctl_bin: str = "/usr/bin/systemctl"
    mediamtx_bin: str = "mediamtx"
    mediamtx_config: Path | None = None
    mediamtx_auto_recover: bool = True
    health_interval_s: float = 2.0
    health_fail_threshold: int = 2
    health_request_timeout_s: float = 1.5

    # ------------------------------------------------------------- binaries
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"

    # ------------------------------------------------------------- capacity
    max_active_streams: int = 20
    max_transcode_streams: int = 4
    cpu_warn_percent: float = 80.0
    mem_warn_percent: float = 85.0

    # -------------------------------------------------- codec / output profile
    # auto: copy compatible inputs, transcode the rest; never: refuse
    # incompatible inputs; always: transcode everything.
    transcode_policy: Literal["auto", "never", "always"] = "auto"
    copy_codecs: CsvList = ["h264"]
    copy_pix_fmts: CsvList = ["yuv420p", "yuvj420p"]
    copy_profiles: CsvList = ["Constrained Baseline", "Baseline", "Main", "High"]
    max_width: int = 3840
    max_height: int = 2160
    max_fps: float = 60.0
    keyframe_warn_interval_s: float = 4.0
    # libx264 output used when transcoding
    x264_preset: str = "veryfast"
    x264_tune: str = "zerolatency"
    x264_profile: Literal["baseline", "main", "high"] = "high"
    x264_crf: int = 23
    x264_maxrate_kbps: int = 6000
    x264_gop_seconds: float = 2.0
    transcode_max_width: int = 1920
    transcode_max_height: int = 1080
    output_fps_mode: Literal["passthrough", "cfr"] = "passthrough"
    x264_threads: int = 0  # 0 = libx264 decides

    # --------------------------------------------------------- stream policy
    stream_name_pattern: str = r"^[A-Za-z0-9_-]{1,64}$"
    reserved_names: CsvList = [
        "all", "all_others", "api", "metrics", "pprof", "playback", "health",
        "static", "assets", "index", "login",
    ]
    min_segment_s: float = 1.0
    loop_playlist_hours: float = 168.0  # one week before a publisher rollover
    loop_playlist_max_entries: int = 50_000
    startup_timeout_s: float = 20.0
    stop_timeout_s: float = 5.0
    # Keep publishers running while the backend restarts/upgrades and re-adopt
    # them on start (ignored when the backend owns MediaMTX: process mode).
    keep_publishers_on_restart: bool = True
    restart_backoff_initial_s: float = 1.0
    restart_backoff_max_s: float = 30.0
    restart_max_failures: int = 10  # consecutive failures before ERROR
    stalled_grace_s: float = 10.0  # path not ready while process alive
    source_check_interval_s: float = 3.0
    monitor_interval_s: float = 2.0

    # ---------------------------------------------------------- start spacing
    # Every publisher start waits until the previous one is on air plus this
    # many seconds, so the edge never gets identical frames from two cameras
    # at once (0 disables the queue). See services/start_gate.py.
    start_spacing_s: float = 2.0
    start_ready_wait_s: float = 5.0  # stop waiting for a start that never comes on air
    start_phase_margin_s: float = 0.25  # extra content gap for streams of the same video

    # --------------------------------------------------------------- lead-in
    # A per-stream "slate" (black, stream name, running clock) on the URL before
    # the video: for a fixed time, or until the first viewer connects + N s.
    default_lead_in_mode: Literal["none", "fixed", "hold"] = "fixed"  # for new streams
    default_lead_in_s: float = 5.0
    slate_seconds: int = 10  # length of the slate clip MediaMTX loops
    slate_timeout_s: float = 60.0  # max time to render one slate
    slate_font: str = ""  # optional font file for the slate text (else fontconfig default)

    # ------------------------------------------------------------- recovery
    recovery_concurrency: int = 8
    recovery_transcode_concurrency: int = 2
    recovery_mediamtx_wait_s: float = 60.0

    # ---------------------------------------------------------------- alerts
    alert_webhook_url: str | None = None  # Teams/Slack incoming webhook or any JSON endpoint
    alert_min_interval_s: float = 300.0  # per alert kind + stream
    dashboard_url: str | None = None  # link included in alerts, e.g. https://rig02.example/
    net_warn_mbps: float = 400.0  # warn when RTSP egress exceeds this

    # --------------------------------------------------------------- logging
    stream_log_max_bytes: int = 1_000_000
    stream_log_backups: int = 2
    # Health page: automatic quick check (no RTSP reads) every N seconds; 0 = off.
    health_auto_interval_s: float = 600.0
    # Daily database backup inside the app (works in Docker too); 0 = off.
    backup_interval_h: float = 24.0
    backup_keep: int = 14
    backup_dir: Path | None = None  # default: <data_dir>/backups
    log_level: str = "INFO"

    # --------------------------------------------------------------- library
    scan_interval_s: float = 30.0
    settle_seconds: float = 10.0  # files modified more recently are "copying"
    probe_concurrency: int = 2
    allowed_extensions: CsvList = [
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".ts", ".mts",
        ".m2ts", ".mpg", ".mpeg", ".flv",
    ]
    max_scan_depth: int = 4

    # --------------------------------------------------------------- uploads
    upload_max_bytes: int = 64 * 1024**3
    disk_reserve_bytes: int = 5 * 1024**3
    disk_reserve_percent: float = 5.0
    upload_chunk_bytes: int = 1024 * 1024
    upload_session_idle_s: float = 3600.0  # resumable upload sessions expire after this idle time
    trash_days: float = 7.0  # deleted videos stay in <video_dir>/.trash this long

    # ------------------------------------------------------------------ auth
    # Off: direct access for everyone on the network, no login. On: users, roles, protected streams.
    auth_enabled: bool = False
    users_file: Path | None = None  # default: <data_dir>/users
    secret_key: str | None = None  # default: generated into <data_dir>/secret_key
    session_hours: float = 12.0
    cookie_secure: bool = False  # set true when served over HTTPS

    @field_validator(
        "copy_codecs", "copy_pix_fmts", "copy_profiles", "reserved_names",
        "allowed_extensions", "rtsp_alt_hosts", "cf_access_aud", mode="before",
    )
    @classmethod
    def _split_csv(cls, v):
        # Allow "a,b,c" in environment variables in addition to JSON lists.
        if isinstance(v, str):
            if v.strip().startswith("["):
                return json.loads(v)
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    # ------------------------------------------------------- derived paths
    @property
    def log_path(self) -> Path:
        return self.log_dir or self.data_dir / "logs"

    @property
    def thumbnail_path(self) -> Path:
        return self.thumbnail_dir or self.data_dir / "thumbnails"

    @property
    def backup_path(self) -> Path:
        return self.backup_dir or self.data_dir / "backups"

    @property
    def slate_path(self) -> Path:
        return self.slate_dir or self.data_dir / "slates"

    @property
    def run_path(self) -> Path:
        return self.run_dir or self.data_dir / "run"

    @property
    def database_path(self) -> Path:
        return self.db_path or self.data_dir / "rtsp-manager.db"

    @property
    def users_path(self) -> Path:
        return self.users_file or self.data_dir / "users"

    @property
    def upload_tmp_path(self) -> Path:
        # Same filesystem as the library so the final move is an atomic rename.
        return self.video_dir / ".uploads"

    def public_rtsp_url(self, name: str, host: str | None = None) -> str:
        h = host or self.public_rtsp_host
        if ":" in h and not h.startswith("["):  # IPv6 literal
            h = f"[{h}]"
        return f"rtsp://{h}:{self.public_rtsp_port}/{name}"

    def publish_url(self, name: str) -> str:
        return f"{self.publish_rtsp_base.rstrip('/')}/{name}"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.video_dir, self.log_path, self.log_path / "streams",
                  self.thumbnail_path, self.run_path, self.upload_tmp_path):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
