# Acceptance report

**Build:** RIG-02 v1.0.0 · **Executed on:** development machine (macOS 26 arm64, Apple M-series 8 cores, 8 GB RAM, FFmpeg 9.0.2, MediaMTX v1.21.2, Python 3.12, Node 24) · **Date:** 2026-11-03

> **Scope of this report.** Everything marked PASS was executed here, against real FFmpeg and MediaMTX processes, and read back over RTSP. **Nothing has been run on the Proxmox VM or the 5.x edge yet.** Those items are NOT RUN until the procedures in DEPLOYMENT.md §6–7 and EDGE_VALIDATION.md are executed and their results are entered below.

## 1. Test execution summary

> **Latest runs (2026-11-12, development Mac), before the final fixes of that day:** backend suite 272 passed + 2 failed (two migration tests still expected schema v5; fixed, that file re-run: 26 passed); Debian 12 container: same 272 + 2; Ubuntu 24.04: not reached (the script stops at the first failure); `install` (systemd, Debian 12): **PASS**; `docker` (production image, linux/arm64): **PASS**; frontend 39 passed; capacity ramp to 100 streams: **PASS** (§2a). Fixed afterwards and **not re-run**: health test port inside Docker, STARTING streams in the health test, the Docker interface-address switch, gzip, Cloudflare session-expiry reload, test tooling without host FFmpeg. From now on all execution happens on the Proxmox VM: **re-run `scripts/test_linux.sh all` there** (docs/PROXMOX_RUNBOOK.md §3) and record the result here.

| Suite | Command | Result |
|---|---|---|
| Backend unit + API + integration | `backend/.venv/bin/pytest -v` | **184 passed** in one run on macOS (FFmpeg 9); Linux matrix below |
| Linux: Debian 12 (FFmpeg 5.1), Ubuntu 24.04 (FFmpeg 6.1) | `scripts/test_linux.sh tests` | Debian 12: **184 passed**. Ubuntu 24.04: 183 passed + 1 over-strict bitrate assertion (first measurement window), fixed and re-run 3× green |
| Installer under systemd (Debian 12 container) | `scripts/test_linux.sh install` / `tls` | **PASS**: install, exposure guard, stream RUNNING over RTSP, backend restart keeps the same publisher PID, MediaMTX SIGKILL → republished, polkit restart, nightly backup; HTTPS: redirect, Secure cookie, backend loopback-only |
| Frontend unit | `cd frontend && npm test` | 24 passed |
| 30-minute loop soak | `SOAK_SECONDS=1800 pytest -m long -s` | **PASS**: 1800 s, 53,947/53,947 packets, 0 duplicate PTS, ratio 1.0001, 149/149 loops, 0 restarts, 0 disconnects |
| Capacity 1/4/8/16 + transcode | `pytest -m performance -s` | **PASS**: all levels RUNNING, 0 dropped frames, 0 reader disconnects, 0 publisher restarts (§4) |
| Browser walkthrough (dev) | manual, in-app browser | create from library at 01:30:00 → RUNNING → copy URL; detail panel, snapshot, logs; backend restart → stream auto-recovered |
| Network exposure (dev) | LAN-address checks | LAN read OK; LAN publish → 401; Control API on LAN → refused; second publisher on a live path → rejected |

## 2. Final acceptance checklist (Part 19)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 1 | Video upload works | PASS (dev) | test_upload_streaming_success; browser upload UI with progress |
| 2 | Server-side file discovery | PASS (dev) | test_server_side_discovery_rename_and_missing, test_partially_copied_file_is_not_offered |
| 3 | Metadata extraction | PASS (dev) | test_library_metadata (codec, profile, fps 29.97, pix_fmt, bit depth, VFR, audio, GOP) |
| 4 | Stream creation | PASS (dev) | test_full_lifecycle; browser walkthrough |
| 5 | Unique paths enforced | PASS (dev) | EC-06 tests (10 concurrent creates → 1 success) |
| 6 | Start offset | PASS (dev) | EC-13: position 6302.5 s for start 01:45:00; keyframe snapping shown in UI |
| 7 | Optional end offset | PASS (dev) | test_realtime_loop_continuity (3→7 s, period 4.000 s) |
| 8 | Loop mode | PASS (dev) | loop tests + soak |
| 9 | Play-once mode | PASS (dev) | test_play_once_completes_and_stays_completed |
| 10 | Real time at 1x | PASS (dev) | EC-03 ratios 1.007–1.014; soak ratio (below) |
| 11 | Audio dropped | PASS (dev) | RTSP output has exactly one (video) track for an AAC source |
| 12 | Codec compatibility handled | PASS (dev) | EC-01: HEVC/VP9/AV1/10-bit → H.264; policy never → refused |
| 13–15 | Stop / restart / delete | PASS (dev) | test_full_lifecycle (delete keeps the source file) |
| 16 | Viewer count | PASS (dev) | test_full_lifecycle (1 reader → viewer_count 1, reader address) |
| 17 | FFmpeg command displayed | PASS (dev) | detail panel; API `ffmpeg_command_text` |
| 18 | Last 20 log lines | PASS (dev) | detail panel; `/logs?lines=20` |
| 19 | MediaMTX status displayed | PASS (dev) | health bar, red banner, test_dashboard_works_while_mediamtx_down |
| 20 | MediaMTX recovery | PASS (dev, process mode) · systemd mode NOT RUN | EC-09: detected 1.6 s, back 2.7 s, streams 2.9 s |
| 21–23 | CPU / RAM / disk monitoring | PASS (dev) | health bar; /api/system; test_system_endpoint |
| 24 | 16 simultaneous streams validated | PASS (dev machine) · **VM NOT RUN** | §4 |
| 25 | Same source twice | PASS (dev) | EC-05 |
| 26 | Concurrent user operations safe | PASS (dev) | EC-11 |
| 27 | Source-file changes handled | PASS (dev) | EC-07 (delete, replace, truncate, rename) |
| 28 | Large-file seeking | PASS (dev, 2 h file) · 4 GB on VM NOT RUN | EC-13, FFMPEG.md §5 |
| 29 | Reboot recovery | PASS (backend restart, dev + systemd container; publishers survive backend restarts) · **real reboot NOT RUN** | EC-10 backend test; E-10 runbook |
| 30 | Fixed network access documented | DONE | DEPLOYMENT.md §2–3 (actual office IP/DNS to be assigned by the network admin) |
| 31 | 5.x edge consumes the streams | **NOT RUN** | EDGE_VALIDATION E-01 |
| 32 | 13 edge cases have documented outcomes | Server side: documented (TEST_PLAN §2) · Edge side: NOT RUN | record sheets in tests/manual_edge/ |

### Ticket targets

| Target | Status |
|---|---|
| MediaMTX failure detected within 10 s | PASS (dev): 1.6 s |
| Loop stable ≥ 30 min **on the 5.x edge** | Server side: see §3 · Edge: **NOT RUN** |
| 16 streams back within 2 min of boot | Backend restart with 4 streams: 1.4 s (dev) · 16-stream start time: §4 · **real reboot + edge NOT RUN** |
| Publish a camera in under one minute | Workflow is 4 actions (Stream… → start time → Create & start → Copy URL); the stream is RUNNING < 1 s after Create. The portal part must be timed with a tester (E-01) |
| 16 streams stable for a working day | **NOT RUN** (EDGE_VALIDATION "working-day stability") |
| RTSP URLs usable from other subnets / VPN (Phase 8) | PASS (dev): address picker and `?host=` export checked in the browser via the Mac's LAN IP; foreign host refused (400) · **office subnets, VPN and company DNS name NOT RUN** |

## 2a. Seniors' feature list (November 2026)

| Feature | Status | Evidence |
|---|---|---|
| Analytics (Overview page and analytics pages; 17 analytics, 4 categories, video placeholders, run N cameras) | PASS (dev) | test_api_analytics.py, analytics.test.tsx, browser walkthrough 2026-11-11 |
| Start-of-stream slate (fixed N s, or hold until the first viewer) | PASS (dev, Debian container, Docker image) · **5.x edge NOT RUN** | test_lead_in.py, SPIKE_LEADIN.md, `test_linux.sh install` / `docker` |
| 2 s spacing between camera starts (every start incl. recovery) | PASS (dev) | test_start_gate.py, test_start_spacing.py; walkthrough: cameras live 2.3 s apart in creation order |
| Upload from any device (resumable) | PASS (dev, browser + API) · **phone on the office Wi-Fi NOT RUN** | test_api.py upload tests; through Cloudflare: 8 MB chunks < 100 MB limit (not run on a real tunnel) |
| Stop, restart next day on the same URL, delete | PASS (dev) | test_full_lifecycle; recovery tests |
| Viewer counts | PASS (dev) | viewer tests; Health/Streams pages |
| Health test page (+ automatic 10-min check, alerts, report download) | PASS (dev, Docker image) | test_health_test.py, health.test.tsx, browser 2026-11-12 (held stream skipped, not woken) |
| Capacity: runtime limit + ramp load test up to 100 streams | Mac: **PASS at 100 streams × 1 reader** · **Proxmox VM NOT RUN** | `docs/reports/loadtest-ramp-mac-20261112.json` (below) |
| Docker image (portable to any server) | PASS (Docker Desktop, linux/arm64) · **amd64 server NOT RUN** | `scripts/test_linux.sh docker` |
| Access from anywhere (Cloudflare Tunnel + Access, fail closed) | Server side PASS (dev) · **real tunnel, Access login and WARP RTSP NOT RUN** (needs the company Cloudflare account) | test_cf_access.py |
| RTSP URLs for other subnets / VPN | PASS (dev) · office subnets/VPN NOT RUN | test_network_info.py, browser check via LAN IP |
| NAS as the video store | **Dropped** (2026-11-12): the Proxmox VM is both server and storage | — |

**Capacity ramp on the development Mac** (Apple M-series, 8 cores, 8 GB; one 3 Mbit/s H.264 clip, copy mode, 1 RTSP reader per stream on the same machine, 1 min per level):

| Streams | Readers | Start-up of the new streams | CPU avg / max | Memory max | Out (Mbit/s) | FFmpeg RSS | Slowest publisher | Drops / restarts / dead readers |
|---|---|---|---|---|---|---|---|---|
| 10 | 10 | 23 s | 47 / 54 % | 81 % | 65 | 0.27 GB | 1.01x | 0 / 0 / 0 |
| 25 | 25 | 36 s | 44 / 53 % | 83 % | 159 | 0.60 GB | 1.00x | 0 / 0 / 0 |
| 50 | 50 | 115 s | 36 / 45 % | 85 % | 309 | 1.17 GB | 1.00x | 0 / 0 / 0 |
| 75 | 75 | 101 s | 44 / 52 % | 85 % | 463 | 1.77 GB | 1.00x | 0 / 0 / 0 |
| 100 | 100 | 106 s | 56 / 62 % | 86 % | 620 | 2.25 GB | 1.00x | 0 / 0 / 0 |

All levels passed. Start-up grows with the number of streams because starts are kept 2 s apart and streams of the *same* video are also kept 2 s apart in content (a 45 s clip has room for ~22 distinct phases; the rest start with a spacing warning). This is the Mac: the VM's number comes from running `scripts/load_test.sh live … --ramp 10,20,40,60,80,100 --readers 2 --apply` on/against the VM, which also saves 80 % of the last good level as the limit.

## 3. Loop soak (server side, 30 minutes)

Source `h264_2997.mp4` (29.97 fps, B-frames), loop 00:00:04 → 00:00:15 (keyframe-aligned to 2.002 → 14.014, period 12.012 s), read by one RTSP/TCP reader for 30 minutes. Raw data: `docs/reports/soak-1800s-*.json` and `*-packets.txt`.

| Metric | Result |
|---|---|
| Media received / wall clock | 1800.03 s / 1800.2 s (ratio **1.0001**) |
| Packets received / expected at 29.97 fps | **53,947 / 53,947** |
| Duplicate timestamps / backward jumps | **0 / 0** |
| Largest gap between frames | 66.7 ms (two frame intervals, B-frame reorder) |
| Loop count reported / expected | **149 / 149** |
| Publisher restarts / reader disconnects | **0 / 0** |

The first attempt was lost to a test-script parsing bug, and a 60 s validation run then exposed a real duplicate-timestamp bug at loop points on 29.97 fps sources (ffprobe rounds the end keyframe's DTS); it was fixed before this run (FFMPEG.md §3). This proves the **server side** of EC-04; the 30 minutes on the 5.x edge (E-04) are still NOT RUN.

## 4. Performance / capacity (development machine, NOT the Proxmox VM)

Source: 1920×1080 25 fps H.264 4 Mbit/s; each level measured for 30 s. Raw data: `docs/reports/performance-Mac.bbrouter-20261103.json`. CPU % is per core (100 % = one core); host CPU is the whole 8-core machine.

| Streams | Mode | RTSP readers | All RUNNING after | Publishers CPU | Publishers RSS | MediaMTX CPU | Host CPU | Ingest | Dropped | Disconnects | Restarts |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | copy | 1 | 0.34 s | 1.1 % | 25 MB | 2.1 % | 12.9 % | 3.9 Mbit/s | 0 | 0 | 0 |
| 4 | copy | 4 | 0.59 s | 3.7 % | 102 MB | 6.1 % | 13.3 % | 15.8 Mbit/s | 0 | 0 | 0 |
| 8 | copy | 8 | 0.92 s | 6.7 % | 161 MB | 11.6 % | 16.7 % | 31.6 Mbit/s | 0 | 0 | 0 |
| 16 | copy | 0 | 1.63 s | 13.4 % | 288 MB | 12.0 % | 16.3 % | 63.5 Mbit/s | 0 | 0 | 0 |
| 16 | copy | 16 | 1.64 s | 12.3 % | 255 MB | 21.6 % | 18.6 % | 63.0 Mbit/s | 0 | 0 | 0 |
| 16 | copy | 32 | 1.62 s | 11.5 % | 252 MB | 26.0 % | 19.9 % | 63.2 Mbit/s | 0 | 0 | 0 |
| 1 | transcode | 1 | 0.33 s | 81 % | 144 MB | 1.6 % | 17.8 % | 6.1 Mbit/s | 0 | 0 | 0 |
| 2 | transcode | 2 | 0.43 s | 129 % | 290 MB | 2.6 % | 23.1 % | 12.1 Mbit/s | 0 | 0 | 0 |
| 4 | transcode | 4 | 0.61 s | 196 % | 540 MB | 4.3 % | 38.2 % | 24.2 Mbit/s | 0 | 0 | 0 |

**Reading:** stream copy is close to free: 16 publishers together use about 0.13 of a core and about 16 MB RSS each. 16 streams were all RUNNING 1.6 s after creation, well inside the 2-minute reboot target. The network (16 × source bitrate in, plus readers × bitrate out) and disk read are the real limits for copy. 1080p transcoding costs about 0.5–0.8 of a core per stream (on this Apple M-series machine, `veryfast`), roughly 40× a copy stream, which is why transcodes are capped at 4 by default. Publisher speed stayed ≥ 1.01x at every level (real time).

**Not yet measured:** network throughput to real readers across the office LAN, disk I/O on the VM's storage, and all figures on the Proxmox VM itself. Run `scripts/load_test.sh isolated` (and `scripts/load_test.sh live … --streams 16 --minutes 480` for the working-day test) on the VM and add the results here.

## 5. Reboot recovery report

| Scenario | Environment | Result |
|---|---|---|
| Backend restart with 4 running + 1 stopped + 1 completed stream | dev | 4/4 RUNNING 1.4 s after app start; same effective start offsets; stopped stays STOPPED; completed stays COMPLETED; 0 orphan publishers |
| Orphaned publisher left by a crashed backend | dev | terminated on start; exactly one publisher afterwards |
| MediaMTX SIGKILL with 2 streams | dev (process mode) | DOWN detected 1.6 s; UP 2.7 s; both RUNNING 2.9 s; RTSP verified |
| **Server reboot with 16 streams, edge cameras** | Proxmox VM + 5.x edge | **NOT RUN**: fill in from EDGE_VALIDATION E-10 (`measure_recovery.py` CSV + `/api/system/recovery` + edge screenshots) |

## 6. Test execution report template (for VM and edge runs)

| Field | Value |
|---|---|
| Date / tester | |
| Server (VM size, OS, FFmpeg, MediaMTX versions) | |
| Edge model / firmware, portal version | |
| Build / commit | |
| Test ID (E-xx / pytest id) | |
| Steps executed | |
| Expected | |
| Actual (numbers: first-frame s, recovery s, drift s, disconnects) | |
| Result (PASS / FAIL / BLOCKED / NOT RUN) | |
| Evidence location (file names, screenshots, log excerpts) | |
| Defects raised | |

## 7. Known limitations

See ARCHITECTURE.md §11. In short: start/end are keyframe-accurate; long loops reconnect once per 7-day playlist; edge-side behaviour (decoding, retry after the 60 s hold, 30-minute loop, reboot from the edge's view) is unverified until the runbook is executed; capacity figures above are from the development machine, not the VM.
