# Test plan and QA matrix

## 1. Strategy

| Layer | Where | Runs | What it proves |
|---|---|---|---|
| Unit | `tests/unit/test_validation.py`, `test_ffmpeg_command.py`, `test_compatibility.py`, `test_files_auth_db.py` | `pytest tests/unit` (< 1 s) | names, timestamps, segments, command construction (copy/transcode/loop/once/scale/cfr), playlist quoting, progress parsing and loop counting, codec policy, file identity and change detection, upload name sanitizing, disk reservations, password hashing and tokens, migrations and constraints |
| API (real FFprobe, no MediaMTX) | `tests/unit/test_api.py` | `pytest tests/unit` (~20 s) | auth and roles, library metadata, rename/missing detection, settling of copied files, streamed uploads, EC-02/EC-06/EC-08 rejections with **no process started**, concurrent creation, optimistic concurrency, delete keeps the source, **the UI API keeps working while MediaMTX is down** |
| Integration (real FFmpeg → real MediaMTX → RTSP reader) | `tests/integration/` | `pytest` (default, ~5 min) | full lifecycle, real-time pacing, loop continuity, play-once completion, EC-01, 03, 04 (short), 05, 07, 09, 10 (backend side), 11, 12, 13, orphan reaping, MediaMTX network exposure |
| Performance | `tests/performance/test_capacity.py` | `pytest -m performance -s` | CPU/RAM/ingest for 1/4/8/16 copy streams with 0–2 readers each, 1/2/4 transcodes; startup time; dropped frames; reader disconnects; publisher restarts |
| End-to-end (browser + simulated edges) | `scripts/e2e/` | `scripts/e2e/run.sh <url> readonly\|full` (~8 min) | every page on desktop and phone, every RTSP URL, 2 simulated edges per camera (fps, gaps, keyframes, B-frames), picture = library file, HLS preview, snapshots, burned-in clock delay; `full` also creates/stops/deletes `e2e-*` streams (test instance only) |
| Long-running | `tests/performance/test_soak.py` | `SOAK_SECONDS=1800 pytest -m long -s` | 30-minute loop over RTSP: 1x, monotonic, no duplicates or gaps, no reader disconnect, exact loop count, no publisher restart |
| Access & operations (API) | `tests/unit/test_api_access.py`, `test_api_ops.py`, `test_api_groups.py` | `pytest tests/unit` | roles, protected streams, login lockout (not affecting other users behind one address), admin user CRUD + last-admin guard, password change, resumable upload interrupted at 50% and resumed, abort releases space, delete-video refused while in use then moved to trash, Prometheus metrics, webhook alerts + rate limit + unreachable webhook, groups/URL export/bulk actions with protection |
| Publisher survival | `tests/integration/test_publisher_survival.py` | `pytest` | RTSP reader stays connected while the backend stops and a new one adopts the publisher (same PID, max gap 80 ms); outdated command replaced, never two publishers |
| Launcher | `tests/unit/test_launcher.py`, `scripts/test_linux.sh launcher` | `python3 app.py` on a clean Debian 12 machine with only Python + FFmpeg: environment setup, verified MediaMTX download, first admin, background run after the terminal closes, second run shows the address, upload, stream, RTSP, preview, restart recovery, stop |
| Live preview | `tests/integration/test_live_preview.py` | HLS playlist and segments through the backend, viewer count unaffected, no path tricks, 409 when not live, login required |
| Observability | `tests/integration/test_observability.py` | `pytest` | ingest/egress bitrate, availability history, webhook alert when a source file is deleted mid-stream |
| Linux matrix | `scripts/test_linux.sh` (Docker) | `scripts/test_linux.sh all` | full suite on Debian 12 (FFmpeg 5.1) and Ubuntu 24.04 (FFmpeg 6.1); `install.sh` under systemd: health, stream, restart without interruption, MediaMTX kill + republish, polkit, backup; HTTPS install (redirect, Secure cookie, backend loopback-only) |
| Frontend | `frontend/src/**/*.test.ts(x)` | `npm test` | time parsing/formatting, name rules, error extraction, create-dialog validation and server preview, created-URL display |
| Manual (edge) | `docs/EDGE_VALIDATION.md` | by hand on the 5.x edge | edge decoding, loop stability on the edge (30 min), first-frame time, reboot recovery seen from the edge, retry behaviour |

Rules: integration tests use the real deployment `mediamtx.yml` (ports substituted). Mocks are not used to prove streaming. Every test creates an isolated instance (own DB, library, MediaMTX on free ports). Destructive tests work on copies of the fixtures.

Fixtures (`scripts/generate_test_videos.sh`): H.264 25 fps + AAC audio (1 s GOP, B-frames), 23.976 / 29.97 / 59.94 fps, VFR (20/60 ms alternating, 1 ms timebase), 8 s long-GOP, HEVC, HEVC 10-bit, VP9, AV1, H.264 10-bit, H.264 4:2:2, corrupt file, 2-hour file, optional 4 GB 1080p file. All use the `testsrc` pattern, which burns in a running timestamp.

## 2. QA matrix: the 13 mandatory edge cases

Status legend: **PASS (server)** = verified by an executed automated test against real FFmpeg + MediaMTX on the development machine (macOS arm64, FFmpeg 9.0.2, MediaMTX 1.21.2). **NOT RUN** = needs the 5.x edge or the Proxmox VM; must be executed there.

| Case | Implementation | Automated test (evidence) | Expected | Actual (dev machine) | Edge/VM status |
|---|---|---|---|---|---|
| **EC-01** Unsupported codecs | `compatibility.py` policy; libx264 8-bit 4:2:0 transcode; `-an` | `test_compatibility.py` (12), `test_ec01_incompatible_codecs_are_transcoded_to_h264[hevc, hevc_10bit, vp9, av1, h264_10bit]`, `test_ec01_transcode_limit_enforced`, `test_unsupported_codec_rejected_when_policy_never` | H.264 output via transcode or a clear refusal; audio dropped | **PASS**: all 5 published as H.264 High yuv420p, pace 1.03–1.04 (includes connect); policy `never` → 422 "Codec HEVC is not in the edge copy list…"; 2nd transcode beyond the limit → 409; RTSP has only a video track | E-01 **NOT RUN** |
| **EC-02** Invalid start/end | `validation.validate_segment` + keyframe alignment in `prepare()`; UI pre-check | `test_validation.py::TestSegments`, `test_invalid_stream_requests_rejected_before_ffmpeg` (10 cases), `test_valid_boundaries_accepted` | refused with a message before FFmpeg; no dead stream; no orphan process | **PASS**: 422 with "beyond the end", "must be after", "minimum"; stream list empty; zero FFmpeg processes | E-02 NOT RUN |
| **EC-03** VFR / odd fps | `-re`, timestamps preserved (copy), `-fps_mode passthrough` (transcode) | `test_ec03_odd_and_variable_frame_rates_play_in_real_time[23.976, 29.97, 59.94, vfr]` | real time, no speed change, stable timestamps | **PASS**: wall/media 1.012 / 1.009 / 1.007 / 1.011; delivered fps = source; 0 duplicate PTS | E-03 stopwatch vs burnt-in clock **NOT RUN** |
| **EC-04** Loop continuity | concat playlist; keyframe-snapped inpoint; outpoint = end-keyframe DTS, duration = PTS − inpoint | `test_realtime_loop_continuity_and_loop_count`, `test_whole_file_loop_with_bframes_is_seamless`, `test_soak.py` (30 min) | no freeze, no timestamp jump, no disconnect | **PASS (server)**: 0 duplicate PTS, max step = one frame, 1x, exact loop count, no restarts; 30-min soak: see ACCEPTANCE_REPORT §3 | E-04 30 min on the edge **NOT RUN** |
| **EC-05** Same source twice | one publisher per stream | `test_same_source_two_independent_streams` | independent publishers and positions; stopping one does not affect the other | **PASS**: two PIDs, positions ≈ 2 s vs ≈ 12 s, B unchanged (same PID, 0 restarts) after A stopped | E-05 NOT RUN |
| **EC-06** Duplicate / invalid names | regex + reserved names; `UNIQUE COLLATE NOCASE`; create lock | `test_validation.py` (19 names), `test_duplicate_names_including_case`, `test_concurrent_creation_same_name` | rejected; no duplicate paths or publishers | **PASS**: spaces/slashes/specials/reserved → 422; duplicate (any case) → 409; 10 simultaneous creates → 1×201 + 9×409 | E-06 NOT RUN |
| **EC-07** File deleted / replaced | fingerprint watchdog (3 s) | `test_ec07_source_deleted_while_streaming`, `…_replaced_…`, `…_truncated_…`, `…_renamed_then_restart_follows_the_file`, `test_fingerprint_and_change_detection` | clear status; no crash; unrelated streams unaffected | **PASS**: ERROR "deleted or moved" in 0.6 s; page loads; other stream same PID; replace → "replaced", Restart re-validates; rename → Restart follows the file | E-07 NOT RUN |
| **EC-08** Disk nearly full | reservation (free − reserve − in-flight) before writing; pre-flight endpoint; temp file + atomic link | `test_upload_refused_when_disk_nearly_full`, `test_upload_reservation_refuses_when_insufficient`, `test_concurrent_uploads_are_accounted` | refused before copying; free space shown; no partial file | **PASS**: 507 with required/free/available/reason; no file in the library or `.uploads/` | E-08 on the VM NOT RUN |
| **EC-09** MediaMTX killed | API poll 2 s × 2 failures; controller start/restart; republish on UP | `test_ec09_mediamtx_killed_detected_restarted_and_republished` | detected ≤ 10 s; restarted; streams republished | **PASS**: SIGKILL detected in **1.6 s**; MediaMTX UP at 2.7 s; both streams RUNNING at 2.9 s; RTSP verified | E-09 with systemd **NOT RUN** |
| **EC-10** Reboot with 16 streams | desired-state persistence; boot recovery (8 parallel); timing report | `test_ec10_restart_restores_running_streams_only`, `test_orphaned_publishers_are_reaped`; capacity test (16 streams start time) | all 16 back, edge green within 2 min of boot | **PASS (backend restart)**: 4/4 RUNNING 1.4 s after app start, same offsets; stopped stays stopped; completed stays completed | E-10 real reboot + edge **NOT RUN** |
| **EC-11** Concurrent users | per-stream locks; `If-Match` versions; create lock | `test_ec11_concurrent_operations_never_duplicate_publishers`, `test_optimistic_concurrency`, `test_concurrent_creation_same_name` | no lost streams, no duplicate names or publishers, consistent state | **PASS**: 16 concurrent start/stop/restart → never > 1 publisher (sampled every 20 ms); stale `If-Match` → 409 "changed by" | E-11 NOT RUN |
| **EC-12** Early edge connection | MediaMTX `runOnDemand` placeholder holds DESCRIBE up to 60 s | `test_ec12_reader_connected_before_publisher_gets_the_stream` | the reader gets the stream when publishing starts | **PASS (ffprobe reader)**: connected 5 s before the start, received H.264 | E-12 edge retry behaviour **NOT RUN** |
| **EC-13** Long / large files | concat inpoint (index seek); ffprobe `-read_intervals` | `test_ec13_two_hour_file_seek_is_fast`; container measurements in FFMPEG.md §5 | seeking takes seconds; correct position; low memory | **PASS**: seek to 01:45:00 → RUNNING 0.38–0.62 s; position 6302.5 s after 2 s; RSS 15–28 MB; MP4/MKV/TS lookups 0.03–0.07 s | E-13 with a 4 GB file on the VM NOT RUN |

## 3. Automated coverage of the required test topics (Part 14)

| # | Topic | Tests |
|---|---|---|
| 1 | Stream-name validation | test_validation (valid/invalid), test_api duplicates |
| 2 | Timestamp validation | test_parse_timestamp*, TestSegments |
| 3 | Duration calculation | TestSegments, test_progress_exact_period_from_measurement, test_playlist_* |
| 4 | Metadata extraction | test_library_metadata (real FFprobe) |
| 5 | File identity / change | test_fingerprint_and_change_detection, test_quick_hash…, EC-07 tests |
| 6 | DB uniqueness | test_migrations_match_orm_and_enforce_constraints, test_duplicate_names_including_case |
| 7 | Concurrent creation | test_concurrent_creation_same_name |
| 8–11 | Command construction (copy, transcode, loop) | test_ffmpeg_command.py |
| 12 | Play-once completion | test_play_once_completes_and_stays_completed |
| 13 | Process lifecycle | test_full_lifecycle |
| 14 | Unexpected exit | EC-07 tests (source), EC-09 (MediaMTX), test_orphaned_publishers_are_reaped |
| 15–16 | MediaMTX health / recovery | test_dashboard_works_while_mediamtx_down, test_ec09_* |
| 17–18 | Persistence / reboot restoration | test_ec10_restart_restores_running_streams_only |
| 19 | Upload disk-space validation | test_upload_refused_when_disk_nearly_full, reservation tests |
| 20 | Authentication / authorization | test_auth_required_and_roles, test_tokens, test_user_store, test_api_access.py (roles, protection, lockout, user admin) |

## 4. Commands

```bash
backend/.venv/bin/pytest -v                                  # unit + API + integration
backend/.venv/bin/pytest -v tests/integration -k ec09        # one case
backend/.venv/bin/pytest -m performance -s                   # capacity (writes docs/reports/performance-*.json)
SOAK_SECONDS=1800 backend/.venv/bin/pytest -m long -s        # 30-minute soak (writes docs/reports/soak-*.json)
cd frontend && npm test
```

On the Proxmox VM: install the test requirements (already in `requirements.txt`), copy the repository, then run the same commands. The integration tests start their own MediaMTX on random ports and do not touch the production services.
