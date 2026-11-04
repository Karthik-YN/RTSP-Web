# RIG-02: Video → RTSP Stream Manager

A small web app on the Proxmox server that turns recorded videos into live RTSP "cameras" for the Video Analytics 5.x edge. Pick a file, choose a start (and optional end) time, loop or play once, and get `rtsp://<server>:8554/<name>` to paste into the portal. Streams survive reboots, MediaMTX crashes and multiple simultaneous users.

```
Video file ─▶ Web dashboard ─▶ FastAPI backend ─▶ FFmpeg publisher (one per stream) ─▶ MediaMTX :8554 ─▶ 5.x edge
```

* **Library:** browser upload (streamed, disk-checked) or copy files into `/data/rtsp-manager/videos`. FFprobe metadata, thumbnail, compatibility verdict.
* **Streams:** unique name, start/end (HH:MM:SS or slider with a frame preview), loop/once, 1x. Start, stop, restart, delete. Per stream: RTSP URL with copy button, status, running since, exact loop count, viewers, the FFmpeg command and the last 20 log lines.
* **Monitoring:** CPU, RAM, disk, capacity, viewers; MediaMTX health (down detected in ≤ 5 s) with automatic restart and republishing.
* **Codecs:** H.264 8-bit is stream-copied; H.265/VP9/AV1/10-bit are transcoded to H.264 (with a CPU warning) or refused, per policy. Audio is always dropped.
* **Teams:** roles (admin/operator/viewer), protected streams with owner notes, groups ("demo sets") with start/stop all and URL export, bulk actions, clone, audit log.
* **Operations:** streams keep playing through backend restarts and upgrades; HTTPS option; nightly backups; webhook alerts (Teams/Slack); Prometheus metrics; per-stream availability and bitrate; resumable uploads; light/dark UI.

## Install on a fresh Proxmox VM (Debian 12 / Ubuntu 24.04)

```bash
# 1. Proxmox: create a VM (4 vCPU, 4 GB RAM, data disk for videos); give it a DHCP reservation or static IP and a DNS name
# 2. On the VM:
sudo apt-get update && sudo apt-get install -y git
git clone <this-repository> /tmp/rig02 && cd /tmp/rig02
sudo scripts/install.sh --host rig02.<office-domain> --with-nginx --tls self-signed   # MediaMTX + app + HTTPS; prints the admin password once
sudo ufw allow 8554/tcp && sudo ufw allow 443/tcp && sudo ufw allow 80/tcp   # only if ufw is enabled
scripts/healthcheck.sh                                    # services, ports, API all OK
# 3. Browser: https://rig02.<office-domain>  → log in as admin (Users page: add your colleagues)
# 4. Copy videos into /data/rtsp-manager/videos (or upload them), New stream → copy the RTSP URL → add the camera in the portal
```

Details, network/firewall, LXC notes and recovery verification are in **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## Commands

| Task | Command |
|---|---|
| Configure | edit `/etc/rtsp-manager/rtsp-manager.env` (see [.env.example](.env.example)), then `sudo systemctl restart rtsp-manager` |
| Add a user | `sudo -u rtspmgr /opt/rtsp-manager/backend/.venv/bin/python /opt/rtsp-manager/scripts/manage_users.py add alice --role operator` |
| Start / stop services | `sudo systemctl start mediamtx rtsp-manager` · `sudo systemctl stop rtsp-manager mediamtx` |
| Status | `systemctl status mediamtx rtsp-manager` · `scripts/healthcheck.sh --rtsp` |
| Logs | `journalctl -u rtsp-manager -f` · `journalctl -u mediamtx -f` · `/var/lib/rtsp-manager/logs/streams/<id>.log` |
| Verify an RTSP stream (laptop) | `scripts/validate_rtsp.sh rtsp://rig02.<domain>:8554/<name>` |
| Verify recovery (laptop, then reboot server) | `python3 scripts/measure_recovery.py urls.txt` |
| Stop every stream | `sudo -u rtspmgr /opt/rtsp-manager/scripts/stop_publishers.sh` (restarting the service does **not** stop streams) |
| Backup now / restore | `sudo /opt/rtsp-manager/scripts/backup.sh` · see docs/DEPLOYMENT.md §5 |
| Linux test matrix (Docker) | `scripts/test_linux.sh all` |
| Load test on the VM | `scripts/load_test.sh isolated` (private MediaMTX) or `scripts/load_test.sh live --video-id N --streams 16` |
| Upgrade | `git pull && sudo scripts/install.sh` |

### Develop and run locally (macOS/Linux with ffmpeg + mediamtx installed)

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && cd ..
cd frontend && npm ci && npm run build && cd ..          # Node ≥ 18
scripts/start.sh                                         # http://localhost:8080 (starts its own MediaMTX, auth off)
# UI hot reload: (cd frontend && npm run dev)           # http://localhost:5173, proxies /api to :8080
```

### Tests

```bash
scripts/generate_test_videos.sh tests/.fixtures          # synthetic clips (done automatically by pytest)
backend/.venv/bin/pytest -v                              # unit + API + real FFmpeg/MediaMTX integration (~5 min)
backend/.venv/bin/pytest tests/unit -v                   # fast tests only
backend/.venv/bin/pytest -m performance -s               # 1/4/8/16-stream capacity measurement
SOAK_SECONDS=1800 backend/.venv/bin/pytest -m long -s    # 30-minute loop soak
(cd frontend && npm test)                                # frontend unit tests
```

Edge-dependent tests are manual: **[docs/EDGE_VALIDATION.md](docs/EDGE_VALIDATION.md)**.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Requirements, decisions on ambiguities, design, state machine, concurrency, recovery, security, **DB schema**, risks, limitations |
| [docs/FFMPEG.md](docs/FFMPEG.md) | Publisher command, why not `-stream_loop`, clean loop points, loop counter, seeking accuracy, MediaMTX early readers, codec policy (with measurements) |
| [docs/API.md](docs/API.md) | REST API (also live at `/docs`) |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | VM, fixed address, firewall, install, operate, verify |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Symptoms → causes → fixes |
| [docs/TEST_PLAN.md](docs/TEST_PLAN.md) | Test strategy, automated suite, **13-case QA matrix** |
| [docs/EDGE_VALIDATION.md](docs/EDGE_VALIDATION.md) | Manual runbook for the 5.x edge (30-min loop, 16-stream reboot) |
| [docs/DEMO_WALKTHROUGH.md](docs/DEMO_WALKTHROUGH.md) | How it works in plain terms, and a full script for demoing it to the team |
| [docs/ACCEPTANCE_REPORT.md](docs/ACCEPTANCE_REPORT.md) | Acceptance checklist, performance and recovery results, report template |

## Repository layout

```
backend/app/            FastAPI app: api/ models/ schemas/ database/ services/
frontend/src/           React + TypeScript + Tailwind dashboard
deployment/             mediamtx.yml, mediamtx.service, rtsp-manager.service, polkit rule, nginx.conf
scripts/                install, start, healthcheck, validate_rtsp, measure_recovery, load_test, generate_test_videos, manage_users
tests/                  unit/ integration/ performance/ (pytest) · manual edge tests in docs/EDGE_VALIDATION.md
docs/                   documentation and docs/reports/ (measurement JSON)
storage/                local development data (git-ignored)
```
