# RIG-02 hand-over: adding it to the VD-Analytics repository

> **Update 2026-11-23:** the code now lives on GitHub (`main`, including Phase 12). This note describes the 2026-11-14 NAS hand-over and is kept for reference.

For: Vyasa. From: Karthik. Date: 2026-11-14.

RIG-02 turns recorded videos into live RTSP "cameras" for the Video Analytics 5.x edge. It has:
- a web dashboard (React);
- a backend (FastAPI, SQLite);
- one FFmpeg publisher per stream;
- MediaMTX on port 8554.

README.md describes the features. CHANGELOG.md lists what was built, and when.

## What's on the NAS

| File | What it is |
|---|---|
| `rig02-2026-11-14.bundle` | A complete git repository in one file: all history, with branches `feature/live-preview-and-launcher` (the work) and `master` (the first commit). You can clone from it like from a server. |
| `rig02-2026-11-14.tar.gz` | The same source as plain files, with no git history, in a `rig02/` folder. |
| `SHA256SUMS` | Checksums for both. Check them first: `shasum -a 256 -c SHA256SUMS` (Linux: `sha256sum -c SHA256SUMS`). |

Neither file contains secrets, keys, `.env`, the database, videos, `node_modules` or `.venv`. The built dashboard (`frontend/dist`) is included, so Docker builds need no Node.

## Option A: RIG-02 as its own repository in VD-Analytics (simplest)

```bash
git clone rig02-2026-11-14.bundle rig02 && cd rig02
git checkout feature/live-preview-and-launcher
git log --oneline -1                     # check: this matches the commit named in SHA256SUMS
git remote set-url origin <VD-Analytics git URL for the new repo>
git push origin feature/live-preview-and-launcher master
```
Then open a pull request from `feature/live-preview-and-launcher` into `master` (or rename `master` to `main` first if that's the team's convention).

## Option B: RIG-02 as a folder inside an existing VD-Analytics repository (keeps history)

```bash
cd <VD-Analytics checkout>
git checkout -b add-rig02
git fetch /path/to/rig02-2026-11-14.bundle feature/live-preview-and-launcher:rig02-import
git subtree add --prefix=tools/rig02 rig02-import          # pick the folder you want
git branch -D rig02-import
git push origin add-rig02                                  # then open a pull request
```
For Option B, check the relative paths in the root files:
- `docker-compose.yml` and `Dockerfile` use the RIG-02 folder as their build context, so run `docker compose` from inside `tools/rig02`;
- CI should run `pytest` from that folder too, since `pytest.ini` sits there.

## Running it

| How | Command | Where it's documented |
|---|---|---|
| Docker (recommended) | `cp .env.example .env`, set `RTSPM_PUBLIC_RTSP_HOST`, then `docker compose up -d --build` | docs/DEPLOYMENT.md §4 |
| Permanent install with systemd | `sudo scripts/install.sh` | docs/DEPLOYMENT.md §4b |
| Quick try | `python3 app.py` | README.md |
| Proxmox VM step by step | (see the doc) | docs/PROXMOX_RUNBOOK.md |

**Ports:**
- dashboard on 8080 and RTSP on 8554 by default;
- both can be changed in `.env`;
- only RTSP needs to be reachable by the edge.

**Login:** off by default. Turn it on with `RTSPM_AUTH_ENABLED=true`. Then create the first admin with `docker compose exec rig02 rig02-users add <name> --role admin`; whoever runs the command types the password.

## Testing

```bash
python3 -m venv backend/.venv && backend/.venv/bin/pip install -r backend/requirements.txt
backend/.venv/bin/pytest tests/unit          # ~1.5 min
backend/.venv/bin/pytest tests/integration   # ~9 min, needs ffmpeg and mediamtx on the PATH, else skipped (python3 app.py downloads mediamtx into bin/)
(cd frontend && npm ci && npm test)
scripts/test_linux.sh all                    # on a Linux VM: Debian/Ubuntu suites, installer, launcher, Docker image
```

## Test status on 2026-11-14 (commit in SHA256SUMS)

| Where | Result |
|---|---|
| macOS (developer Mac) | 300 unit passed (1 skipped: that FFmpeg has no drawtext), 51 integration passed, 71 frontend passed, plus a browser walkthrough of all Phase 11 features |
| Linux VM (`scripts/test_linux.sh all`) | NOT RUN for this version |
| Real 5.x edge device | NOT RUN for this version (earlier manual procedure: docs/EDGE_VALIDATION.md) |
| Cloudflare Tunnel / Access | NOT RUN |

## Where to look in the code

| Area | Files |
|---|---|
| API routes | `backend/app/api/` (streams, videos, analytics, health, auth) |
| Stream processes | `backend/app/services/ffmpeg_manager.py`, `mediamtx_service.py` |
| Library, probe, conversions | `backend/app/services/video_library.py`, `ffprobe_service.py`, `compatibility.py`, `converter.py` |
| Database and migrations | `backend/app/models/`, `backend/app/database/migrations.py` (version 7) |
| Dashboard | `frontend/src/pages/`, `frontend/src/components/` |
| Docs | `docs/ARCHITECTURE.md`, `docs/API.md`, `docs/TROUBLESHOOTING.md` |

## Open items
- Run `scripts/test_linux.sh all` on the VM and keep `~/rig02-validate.log`.
- Merging with Jeevan's project is planned separately, once his code is available.
