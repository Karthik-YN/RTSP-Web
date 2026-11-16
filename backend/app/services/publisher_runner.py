"""Per-stream publisher runner (standard library only).

    python -m app.services.publisher_runner --state S --log L [--max-bytes N --backups K] -- ffmpeg ...

Runs one FFmpeg publisher and owns its output, so the publisher does not
depend on the backend process: the backend can restart (upgrade, crash) and
re-adopt running publishers by reading the state file.

* stderr  -> rotating log file (B-frame loop-point DTS warnings collapsed)
* stdout  -> FFmpeg ``-progress`` blocks; the latest block is written to the
             JSON state file (atomic replace, about once per second)
* SIGTERM -> forwarded to FFmpeg (graceful), SIGKILL after a timeout
* on exit -> final state with ``exit_code``, then the runner exits
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

NOISY = re.compile(r"(Non-monotonic DTS|non monotonically increasing dts|changing to \d+)", re.I)


class State:
    def __init__(self, path: str, base: dict):
        self.path = path
        self.data = dict(base)
        self.lock = threading.Lock()
        self.last_write = 0.0

    def update(self, force: bool = False, **kw) -> None:
        with self.lock:
            self.data.update(kw)
            now = time.monotonic()
            if not force and now - self.last_write < 0.9:
                return
            self.last_write = now
            tmp = f"{self.path}.tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)


class Log:
    def __init__(self, path: str, max_bytes: int, backups: int):
        self.path, self.max_bytes, self.backups = path, max_bytes, backups
        self.suppressed = 0

    def _rotate(self) -> None:
        try:
            if os.path.getsize(self.path) < self.max_bytes:
                return
        except OSError:
            return
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else f"{self.path}.{i - 1}"
            if os.path.exists(src):
                os.replace(src, f"{self.path}.{i}")

    def write(self, text: str, source: str = "ffmpeg") -> None:
        text = text.rstrip()
        if not text:
            return
        if source == "ffmpeg" and NOISY.search(text):
            self.suppressed += 1
            if self.suppressed not in (1, 2, 3) and self.suppressed % 500 != 0:
                return
            if self.suppressed > 3:
                text = (f"(timestamp fix-up warnings so far: {self.suppressed}; expected at loop boundaries) "
                        f"{text}")
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        try:
            self._rotate()
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"{ts} [{source}] {text}\n")
        except OSError:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--max-bytes", type=int, default=1_000_000)
    ap.add_argument("--backups", type=int, default=2)
    ap.add_argument("--stop-timeout", type=float, default=5.0)
    ap.add_argument("--meta", default="{}", help="JSON stored in the state file (e.g. base loop count)")
    ap.add_argument("argv", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    argv = a.argv[1:] if a.argv and a.argv[0] == "--" else a.argv
    if not argv:
        print("no command", file=sys.stderr)
        return 2
    log = Log(a.log, a.max_bytes, a.backups)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, bufsize=0)
    except OSError as exc:
        log.write(f"could not start ffmpeg: {exc}", "rtspm")
        State(a.state, {"runner_pid": os.getpid(), "pid": None, "exit_code": 127,
                        "error": str(exc)}).update(force=True)
        return 127
    state = State(a.state, {
        "runner_pid": os.getpid(), "pid": proc.pid, "argv": argv, "exit_code": None,
        "started_at": datetime.now(timezone.utc).isoformat(), "progress": {}, "suppressed": 0,
        "meta": json.loads(a.meta),
    })
    state.update(force=True)

    stopping = threading.Event()

    def on_term(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return

        def killer():
            try:
                proc.wait(a.stop_timeout)
            except subprocess.TimeoutExpired:
                log.write(f"publisher did not exit within {a.stop_timeout:.0f}s; killing", "rtspm")
                proc.kill()
        threading.Thread(target=killer, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)  # survive the backend's session going away

    def pump_stderr():
        for raw in iter(proc.stderr.readline, b""):
            log.write(raw.decode(errors="replace"))

    def pump_progress():
        block: dict = {}
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode(errors="replace").strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            block[key] = value
            if key == "progress":
                block["updated_at"] = datetime.now(timezone.utc).isoformat()
                state.update(progress=block, suppressed=log.suppressed)
                block = {}

    threads = [threading.Thread(target=pump_stderr, daemon=True), threading.Thread(target=pump_progress, daemon=True)]
    for t in threads:
        t.start()
    rc = proc.wait()
    for t in threads:
        t.join(2)
    state.update(force=True, exit_code=rc, suppressed=log.suppressed,
                 ended_at=datetime.now(timezone.utc).isoformat())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
