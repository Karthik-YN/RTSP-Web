#!/usr/bin/env python3
"""Start a MediaMTX binary with a config on spare ports and verify that only
RTSP listens on non-loopback addresses (new MediaMTX versions add servers that
are ON by default, e.g. MoQ in v1.21). Exit 0 = safe, 1 = unexpected listener.

  check_mediamtx_exposure.py /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
"""

from __future__ import annotations

import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    binary, config = sys.argv[1], Path(sys.argv[2])
    rtsp, api, metrics = free_port(), free_port(), free_port()
    text = config.read_text()
    text = re.sub(r"(?m)^rtspAddress:.*$", f"rtspAddress: :{rtsp}", text)
    text = re.sub(r"(?m)^apiAddress:.*$", f"apiAddress: 127.0.0.1:{api}", text)
    text = re.sub(r"(?m)^metricsAddress:.*$", f"metricsAddress: 127.0.0.1:{metrics}", text)
    with tempfile.TemporaryDirectory() as d:
        cfg = Path(d) / "m.yml"
        cfg.write_text(text)
        proc = subprocess.Popen([binary, str(cfg)], cwd=d, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            ports: set[int] = set()
            for _ in range(40):
                time.sleep(0.25)
                if proc.poll() is not None:
                    print("MediaMTX exited:", proc.stdout.read().decode(errors="replace")[-500:])
                    return 1
                conns = psutil.Process(proc.pid).net_connections(kind="inet")
                ports = {c.laddr.port for c in conns if c.status == psutil.CONN_LISTEN or c.type == socket.SOCK_DGRAM
                         if c.laddr and c.laddr.ip not in ("127.0.0.1", "::1")}
                if rtsp in ports:
                    time.sleep(0.5)
                    conns = psutil.Process(proc.pid).net_connections(kind="inet")
                    ports = {c.laddr.port for c in conns if c.laddr and c.laddr.ip not in ("127.0.0.1", "::1")}
                    break
            extra = sorted(ports - {rtsp})
            if extra:
                print(f"UNSAFE: MediaMTX listens on extra network ports {extra} with this config; "
                      "disable the new servers in mediamtx.yml first.")
                return 1
            print(f"OK: only RTSP is reachable from the network ({binary})")
            return 0
        finally:
            proc.terminate()
            proc.wait(5)


if __name__ == "__main__":
    raise SystemExit(main())
