#!/usr/bin/env python3
"""RIG-02 launcher: one command to get to the dashboard.

    python3 app.py             start in the background (if needed) and open the UI
    python3 app.py status      is it running, and where?
    python3 app.py stop        stop the app and every stream
    python3 app.py restart     stop, then start (streams that should run come back)
    python3 app.py logs        show the last log lines

Options: --port 8080, --host <address shown in RTSP URLs>, --foreground, --no-browser

The app keeps running after the terminal closes, so streams don't depend on
whoever started it. Anyone else who runs this finds it running and just gets the
URL. The first start prepares what is missing: a Python environment in
backend/.venv and MediaMTX in bin/ (checksum-verified download). There is no
login by default. Settings can go in a .env file here (see .env.example).

Uses only the standard library, so it runs before anything is installed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = BACKEND / ".venv"
VENV_PY = VENV / "bin" / "python"
STORAGE = ROOT / "storage"
DATA = STORAGE / "data"
LOG_FILE = STORAGE / "logs" / "app.log"
PID_FILE = DATA / "app.pid"
FIRST_LOGIN_FILE = DATA / "first-admin-login.txt"
DIST = ROOT / "frontend" / "dist"

MEDIAMTX_VERSION = "v1.21.2"
MEDIAMTX_API = "http://127.0.0.1:9997/v3/paths/list"
START_TIMEOUT_S = 40


def say(msg: str = "") -> None:
    print(msg, flush=True)


def fail(msg: str) -> None:
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(1)


# ---------------------------------------------------------------- small helpers

def load_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=value lines; comments and blank lines are ignored."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def lan_ip() -> str:
    """This machine's address on the network (no traffic is actually sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def http_ok(url: str, timeout: float = 2.0) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, OSError, ValueError):
        return None


def app_health(port: int) -> dict | None:
    """The app's health document, or None if this port isn't our app."""
    body = http_ok(f"http://127.0.0.1:{port}/api/health")
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("app") == "ok" else None


def port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def dashboard_url(port: int, host: str) -> str:
    # A server installed with nginx + TLS serves the dashboard on 443.
    if port_open(443) and app_health(port):
        return f"https://{host}/"
    return f"http://{host}:{port}/"


def can_open_browser() -> bool:
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return False  # the browser is on the person's laptop, not on the server
    return sys.platform == "darwin" or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def show_url(url: str, no_browser: bool) -> None:
    say(f"\n  Dashboard:  {url}")
    if not no_browser and can_open_browser() and webbrowser.open(url):
        say("  (opened in your browser)")
    else:
        say("  Open that address in your browser.")


# ---------------------------------------------------------------- preparation

def check_python() -> None:
    if sys.version_info < (3, 10):
        fail(f"Python 3.10 or newer is needed; this is {platform.python_version()}.")
    if os.name != "posix":
        fail("This app runs on Linux or macOS.")


def install_hint(package: str) -> str:
    if sys.platform == "darwin":
        return f"brew install {package}"
    return f"sudo apt-get install -y {package}"


def ensure_ffmpeg() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            fail(f"{tool} is not installed. Install it with:\n\n    {install_hint('ffmpeg')}\n")


def ensure_venv() -> None:
    req = BACKEND / "requirements.txt"
    digest = hashlib.sha256(req.read_bytes()).hexdigest()
    marker = VENV / ".requirements-sha256"
    if VENV_PY.exists() and marker.exists() and marker.read_text() == digest:
        return
    say("Preparing the Python environment (first run only, about a minute)...")
    try:
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True, capture_output=True)
    except subprocess.CalledProcessError:
        shutil.rmtree(VENV, ignore_errors=True)
        fail(f"Could not create a Python environment. Install venv support with:\n\n    {install_hint('python3-venv')}\n")
    r = subprocess.run([str(VENV_PY), "-m", "pip", "install", "-q", "-r", str(req)], capture_output=True, text=True)
    if r.returncode != 0:
        fail("Could not install the Python packages (is there internet access?):\n" + r.stderr[-800:])
    marker.write_text(digest)


def mediamtx_asset() -> str:
    system = {"linux": "linux", "darwin": "darwin"}.get(sys.platform)
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if not system or not arch:
        fail(f"No MediaMTX download for {sys.platform}/{platform.machine()}; install it and put it on the PATH.")
    return f"mediamtx_{MEDIAMTX_VERSION}_{system}_{arch}.tar.gz"


def verify_checksum(data: bytes, checksums: str, asset: str) -> bool:
    actual = hashlib.sha256(data).hexdigest()
    for line in checksums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == asset:
            return parts[0] == actual
    return False


def ensure_mediamtx() -> str:
    local = ROOT / "bin" / "mediamtx"
    if local.exists():
        return str(local)
    found = shutil.which("mediamtx")
    if found:
        return found
    asset = mediamtx_asset()
    base = f"https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}"
    say(f"Downloading MediaMTX {MEDIAMTX_VERSION} (first run only)...")
    try:
        with urllib.request.urlopen(f"{base}/{asset}", timeout=120) as r:
            tarball = r.read()
        with urllib.request.urlopen(f"{base}/checksums.sha256", timeout=30) as r:
            checksums = r.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        fail(f"Could not download MediaMTX: {exc}\nPut the mediamtx binary in {local} or on the PATH.")
    if not verify_checksum(tarball, checksums, asset):
        fail("The MediaMTX download failed its checksum check; not installing it.")
    local.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / asset
        tmp.write_bytes(tarball)
        with tarfile.open(tmp) as tar:
            member = tar.extractfile("mediamtx")
            if member is None:
                fail("The MediaMTX archive has no mediamtx binary.")
            local.write_bytes(member.read())
    local.chmod(0o755)
    return str(local)


def ensure_frontend() -> None:
    if (DIST / "index.html").is_file():
        return
    npm = shutil.which("npm")
    if not npm:
        fail("frontend/dist is missing and Node.js is not installed to build it.\n"
             "Copy the whole project folder, including frontend/dist.")
    say("Building the dashboard (first run only)...")
    for cmd in ([npm, "ci", "--no-audit", "--no-fund"], [npm, "run", "build"]):
        r = subprocess.run(cmd, cwd=ROOT / "frontend", capture_output=True, text=True)
        if r.returncode != 0:
            fail("Could not build the dashboard:\n" + (r.stdout + r.stderr)[-800:])


def ensure_admin(env: dict[str, str]) -> None:
    """Create the first admin on a fresh install; the login goes in a private file."""
    users = Path(env["RTSPM_DATA_DIR"]) / "users"
    if env.get("RTSPM_AUTH_ENABLED", "false").lower() != "true" or (users.exists() and users.stat().st_size > 0):
        return
    password = secrets.token_urlsafe(12)
    r = subprocess.run([str(VENV_PY), str(ROOT / "scripts" / "manage_users.py"), "add", "admin", "--role", "admin",
                        "--password-stdin"], input=password + "\n", text=True, env=env, capture_output=True)
    if r.returncode != 0:
        fail("Could not create the first admin account:\n" + r.stderr[-500:])
    fd = os.open(FIRST_LOGIN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"admin\n{password}\n")
    say(f"\n  First login:  admin / {password}")
    say(f"  (also saved in {FIRST_LOGIN_FILE}; add colleagues on the Users page, then delete this file)")


def backend_env(args: argparse.Namespace, mediamtx: str) -> dict[str, str]:
    env = dict(os.environ)
    settings = load_env_file(ROOT / ".env")
    for key, value in settings.items():
        env.setdefault(key, value)
    defaults = {
        "RTSPM_ENV_FILE": os.devnull,  # .env is already applied above
        "RTSPM_DATA_DIR": str(DATA),
        "RTSPM_VIDEO_DIR": str(STORAGE / "videos"),
        "RTSPM_LOG_DIR": str(STORAGE / "logs"),
        "RTSPM_THUMBNAIL_DIR": str(STORAGE / "thumbnails"),
        "RTSPM_FRONTEND_DIST": str(DIST),
        "RTSPM_PUBLIC_RTSP_HOST": lan_ip(),
        "RTSPM_MEDIAMTX_BIN": mediamtx,
        "RTSPM_MEDIAMTX_CONFIG": str(ROOT / "deployment" / "mediamtx.yml"),
        "RTSPM_AUTH_ENABLED": "false",
        "RTSPM_SETTLE_SECONDS": "5",
    }
    for key, value in defaults.items():
        env.setdefault(key, value)
    if args.host:
        env["RTSPM_PUBLIC_RTSP_HOST"] = args.host
    # Use a MediaMTX that is already running here (e.g. the systemd service), else run our own.
    env.setdefault("RTSPM_MEDIAMTX_CONTROL", "external" if http_ok(MEDIAMTX_API) is not None else "process")
    return env


# ---------------------------------------------------------------- commands

def uvicorn_cmd(port: int) -> list[str]:
    return [str(VENV_PY), "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port), "--workers", "1"]


def read_pid() -> int | None:
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def tail_log(n: int = 25) -> str:
    try:
        return "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-n:])
    except OSError:
        return "(no log yet)"


def cmd_start(args: argparse.Namespace) -> int:
    check_python()
    host = args.host or load_env_file(ROOT / ".env").get("RTSPM_PUBLIC_RTSP_HOST") or lan_ip()
    if app_health(args.port):
        say("RIG-02 is already running.")
        show_url(dashboard_url(args.port, host), args.no_browser)
        return 0
    if port_open(args.port):
        fail(f"Port {args.port} is used by another program. Try: python3 app.py --port 8090")

    ensure_ffmpeg()
    ensure_venv()
    mediamtx = ensure_mediamtx()
    ensure_frontend()
    for d in (DATA, STORAGE / "logs", STORAGE / "videos", STORAGE / "thumbnails"):
        d.mkdir(parents=True, exist_ok=True)
    env = backend_env(args, mediamtx)
    ensure_admin(env)

    if args.foreground:
        say(f"Running in this terminal on port {args.port} (Ctrl+C stops the app and its streams).")
        os.chdir(BACKEND)
        os.execve(str(VENV_PY), uvicorn_cmd(args.port), env)

    if LOG_FILE.exists() and LOG_FILE.stat().st_size > 5_000_000:
        os.replace(LOG_FILE, LOG_FILE.with_suffix(".log.1"))
    with open(LOG_FILE, "ab") as log:
        proc = subprocess.Popen(uvicorn_cmd(args.port), cwd=BACKEND, env=env, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.write_text(str(proc.pid))
    say("Starting...")
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            fail(f"The app stopped during startup. Last log lines:\n\n{tail_log()}")
        health = app_health(args.port)
        if health and health.get("status") == "ok":  # web server, MediaMTX and stream recovery all ready
            say("RIG-02 is running in the background. It keeps running after you close this terminal.")
            show_url(dashboard_url(args.port, env["RTSPM_PUBLIC_RTSP_HOST"]), args.no_browser)
            say("\n  Stop it with:  python3 app.py stop")
            return 0
        time.sleep(0.5)
    if app_health(args.port):
        say("WARNING: the dashboard is up, but MediaMTX is not ready. Check:  python3 app.py logs")
        show_url(dashboard_url(args.port, env["RTSPM_PUBLIC_RTSP_HOST"]), args.no_browser)
        return 1
    fail(f"The app did not answer within {START_TIMEOUT_S} s. Last log lines:\n\n{tail_log()}")
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    health = app_health(args.port)
    if not health:
        say("RIG-02 is not running. Start it with:  python3 app.py")
        return 1
    host = args.host or load_env_file(ROOT / ".env").get("RTSPM_PUBLIC_RTSP_HOST") or lan_ip()
    say(f"RIG-02 is running: {dashboard_url(args.port, host)}")
    say(f"  status {health.get('status')}, MediaMTX {health.get('mediamtx')}, version {health.get('version')}")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    pid = read_pid()
    if pid is None:
        if app_health(args.port):
            fail("It is running but wasn't started from this folder. If it's the server install, use:\n\n"
                 "    sudo systemctl stop rtsp-manager\n")
        say("RIG-02 is not running.")
        return 0
    say("Stopping (this also stops every stream)...")
    try:
        os.kill(pid, signal.SIGTERM)
    except PermissionError:
        fail("It was started by another user; ask them to stop it, or use sudo.")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and read_pid() is not None:
        time.sleep(0.3)
    if read_pid() is not None:
        os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)
    say("Stopped.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    cmd_stop(args)
    return cmd_start(args)


def cmd_logs(_: argparse.Namespace) -> int:
    say(tail_log(60))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="RIG-02 stream manager launcher")
    ap.add_argument("command", nargs="?", default="start", choices=["start", "status", "stop", "restart", "logs"])
    ap.add_argument("--port", type=int, default=8080, help="dashboard port (default 8080)")
    ap.add_argument("--host", help="address shown in RTSP URLs (default: this machine's network address)")
    ap.add_argument("--foreground", action="store_true", help="run in this terminal instead of the background")
    ap.add_argument("--no-browser", action="store_true", help="don't try to open a browser")
    args = ap.parse_args()
    return {"start": cmd_start, "status": cmd_status, "stop": cmd_stop, "restart": cmd_restart,
            "logs": cmd_logs}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
