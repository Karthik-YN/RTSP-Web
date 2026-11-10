# RIG-02 Architecture

## 1. What the ticket actually requires

Turn recorded videos into stable live RTSP "cameras" on the office Proxmox server so that anyone can:

1. pick a file (upload, or copied into a server folder) and see duration, resolution, fps and codec;
2. publish it as `rtsp://<server>:8554/<unique-name>` from a start time, with an optional end time, loop or play once, at 1x;
3. stop, restart and delete streams, and see for each one: the file, start offset, running since, loop count, viewers, the FFmpeg command and the last 20 log lines;
4. see when MediaMTX is down and bring it back up; see CPU and memory;
5. run at least 16 streams for a working day, and get every stream back by itself after a reboot (within 2 minutes, verified from the edge);
6. survive the 13 listed edge cases, each tested with the result written down.

**Done** = a tester takes a clip, picks a start time, publishes it, adds it in the portal and sees it live on the 5.x edge within one minute. 16 streams run all day without restarts, a reboot brings everything back untouched, and every edge case has a recorded outcome.

## 2. Contradictions and ambiguities, and the decisions taken

| # | Issue | Decision |
|---|---|---|
| A1 | Time format: the master prompt says "HH:MM", the ticket says "hh:mm:ss or a slider". | Accept `HH:MM:SS(.mmm)`, `MM:SS` and plain seconds. A two-part value follows FFmpeg's convention (`01:30` = 90 s). The UI shows how every typed value is interpreted ("= 00:01:30") and offers a slider with a frame preview, so nothing is ambiguous. |
| A2 | The suggested command `-re -ss <start> -stream_loop -1` | Measured (docs/FFMPEG.md): input `-t` cancels looping, and the loop seek fails on B-frame sources with a non-zero start. FFmpeg 5.x/6.x (Debian/Ubuntu) also loop back to 0 instead of `-ss`. **Replaced** with a generated concat playlist. |
| A3 | "Start position" precision with stream copy | Copy can only start on a keyframe. Start and end snap to the keyframe at or before the requested time. The UI shows the effective times before you publish. |
| A4 | "Loop count" without inventing it from uptime | Exact count derived from FFmpeg's published media timestamps: `floor(out_time / period)`. The period is exact by construction (playlist `duration`). |
| A5 | "Stop the stream" vs. "desired state" when the source is deleted/replaced | The publisher stops, status=ERROR with a clear message, and desired_state=STOPPED, so it does not come back after a reboot. An explicit user **Restart** re-validates against the current file. |
| A6 | Play-once after reboot | COMPLETED sets desired_state=STOPPED, so the stream is never restarted after a reboot. |
| A7 | Early reader: "must get the stream once it starts" | MediaMTX answers 404 immediately when nothing is publishing. A placeholder `runOnDemand` holds the reader's DESCRIBE for up to 60 s until the real publisher appears (measured, see FFMPEG.md §6). After 60 s the edge's own retry applies; this must be confirmed on the 5.x edge. |
| A8 | "16 streams" vs. capacity limits | `RTSPM_MAX_ACTIVE_STREAMS` (default 20) and `RTSPM_MAX_TRANSCODE_STREAMS` (default 4) are enforced. Creation is refused, never silently over-committed. Real capacity must be measured on the VM (`scripts/load_test.sh isolated`). |
| A9 | Edge codec support is unknown | The copy whitelist is configuration (H.264, yuv420p/yuvj420p, 8-bit, Baseline/Main/High, ≤3840×2160, ≤60 fps). Everything else is transcoded to H.264 High 8-bit 4:2:0 (CPU warning), or refused when `RTSPM_TRANSCODE_POLICY=never`. The properties to confirm on the edge are listed in EDGE_VALIDATION.md. |
| A10 | "Edit" streams with several users | PATCH with optimistic concurrency (`If-Match: <version>`). A stale edit gets 409 with who changed it. Renaming requires the stream to be stopped (the URL changes). |
| A11 | JSON vs SQLite store (ticket allows either) | SQLite in WAL mode, with explicit versioned migrations. |

## 3. Architecture

```
 laptop browser ──HTTP :8080──▶ FastAPI backend (1 process, user rtspmgr) ─────────────┐
                                 │  API · auth · validation                              │
                                 │  StreamSupervisor ── spawns ──▶ ffmpeg (one per stream)
                                 │  VideoLibrary (scan, ffprobe, thumbnails, uploads)    │ -re, concat playlist,
                                 │  MediaMTXService (health, paths, readers, control)    │ -c:v copy | libx264,
                                 │  RecoveryService · SystemMonitor (psutil)             │ -an, RTSP/TCP publish
                                 │  SQLite (WAL) videos/streams/events                   ▼
                                 └──HTTP 127.0.0.1:9997 (Control API)──▶  MediaMTX (user mediamtx)
 5.x edge / VLC / ffprobe ──────────────────────RTSP/TCP :8554─────────────▶  (read only from LAN)
```

* **Processes:** two systemd services. `mediamtx.service` starts first. `rtsp-manager.service` comes after it (`Wants=`, not `Requires=`, so the dashboard survives a MediaMTX failure and can restart it).
* **One FFmpeg publisher per stream, each behind a tiny runner** (`services/publisher_runner.py`, standard library only, its own session). The runner owns FFmpeg's output: it writes the rotating log and keeps the latest `-progress` block in `run/streams/<id>/state.json`. The backend reads the state file instead of owning pipes, so **publishers survive a backend restart, upgrade or crash** and are re-adopted (§7).
* **Why one publisher per stream:** It's simple, failures are isolated, each stream has its own PID, log, status and lifecycle, and nothing more central is needed for 16 to 20 streams.
* **Single backend worker.** The process that owns the publishers holds an exclusive `flock` on `<data_dir>/backend.lock`, so a second instance refuses to start.
* **The frontend** (React + Vite + TypeScript + Tailwind) is built to static files and served by FastAPI. nginx is optional.

### Code map

| Path | Responsibility |
|---|---|
| `backend/app/main.py` | App factory, lifespan (lock, migrations, services, recovery), error mapping, security headers, SPA serving |
| `backend/app/config.py` | All settings (`RTSPM_*` env vars) |
| `backend/app/database/` | Engine (WAL, busy timeout), versioned SQL migrations |
| `backend/app/models/` | ORM models (videos, streams, events) |
| `backend/app/schemas/` | Pydantic request/response models (OpenAPI) |
| `backend/app/api/` | Routes: auth, videos, streams, system, MediaMTX |
| `services/ffmpeg_manager.py` | Playlist rendering, **command construction (pure functions)**, progress parsing, log sink, process wrapper (spawn via runner / adopt) |
| `services/publisher_runner.py` | Per-stream runner: FFmpeg I/O, log rotation, state file, signal forwarding |
| `services/alerts.py` | Webhook alerts (Teams/Slack), rate-limited, background thread |
| `api/groups.py` | Stream groups, bulk actions, URL export |
| `api/analytics.py` | Analytics taskbar tree, video placeholders, run N cameras from a placeholder, start/stop all, URL export |
| `frontend/src/components/UploadQueue.tsx` | App-wide upload queue: drop anywhere, several files, keeps going across pages, resumable |
| `frontend/src/pages/AnalyticsPage.tsx` | One analytics: placeholder cards, upload/attach, Run RTSP dialog, its streams |
| `services/ffprobe_service.py` | Metadata, keyframe lookup (PTS+DTS), video-end detection, frame grabs |
| `services/compatibility.py` | Codec policy: copy / transcode / reject with reasons |
| `services/stream_supervisor.py` | Lifecycle, locks, state machine, failure classification, watchdogs |
| `services/start_gate.py` | Start queue (2 s spacing between camera starts) and the same-video phase rule |
| `services/slate_service.py` | Lead-in slates: per-stream MP4 matching the publisher's format, cached and world-readable |
| `services/mediamtx_service.py` | Health monitor, path/reader cache, systemd/process control |
| `services/recovery_service.py` | Boot recovery and timing report |
| `services/video_library.py`, `file_manager.py` | Scanning, identity, rename detection, uploads, disk reservations |
| `services/system_monitor.py` | psutil sampling (CPU, RAM, disks, network, per-FFmpeg usage) |
| `services/auth.py`, `events.py`, `validation.py` | Users and sessions, audit log, pure validators |

## 4. Stream state machine

Desired state (`RUNNING` | `STOPPED`) is what users asked for and is persisted. Status is what is actually happening:

```
             create/start                 MediaMTX path ready
  STOPPED ───────────────▶ STARTING ─────────────────────────▶ RUNNING
     ▲                       │  ▲  startup timeout / exit          │
     │ stop                  │  └──────────── RESTARTING ◀────────┤ unexpected exit / stall (backoff 1,2,4..30 s)
     │                       ▼                    │                 │ MediaMTX down: wait, republish on UP
  STOPPING ◀─────────────────┴────────────────────┘                 │
                                         10 consecutive failures ──▶ ERROR (desired stays RUNNING)
  source deleted/replaced/modified/unreadable ─────────────────────▶ ERROR (desired → STOPPED)
  play-once segment finished (exit 0) ─────────────────────────────▶ COMPLETED (desired → STOPPED)
```

With a lead-in (§5b), `create/start/restart` first goes through `STARTING` ("Preparing the lead-in") → `WAITING_FOR_VIEWER` (hold mode) → `LEAD_IN` (countdown) → `STARTING` (video queued, §5a) → `RUNNING`. Crash retries, loop rollovers and MediaMTX recovery skip the lead-in.

`RUNNING` is reported only after the MediaMTX Control API says a **publisher is online** (`online`, not `ready`: an always-available lead-in path is "ready" with no publisher). A stream whose FFmpeg is alive but whose path is not ready for 10 s is treated as stalled and restarted. A live PID is never taken as proof of health.

## 5. Concurrency and process safety

* **Names:** the regex `^[A-Za-z0-9_-]{1,64}$` plus a reserved list, checked in the backend. The DB has `UNIQUE COLLATE NOCASE`, a create lock serializes name checks and inserts, and an `IntegrityError` becomes 409. Test: 10 simultaneous creates of one name → exactly one 201 and nine 409.
* **Per-stream `asyncio.Lock`:** start, stop, restart, delete, edit, internal retries and readiness transitions all hold the stream's lock. `_spawn` always stops an existing publisher before starting another. Exit notifications from a replaced process are ignored by identity check. Test: 16 concurrent start/stop/restart calls never produced a second publisher (sampled every 20 ms).
* **Ownership and protection:** every stream records its `owner`. The owner can mark it **protected** and leave a note ("client demo until 17:00"). After that, only the owner or an `admin` can start, stop, restart, edit or delete it. Others get 403 naming the owner and the note, and bulk/group actions report it per stream instead of failing.
* **Optimistic concurrency:** config and desired-state changes bump `version`. The UI sends `If-Match` on every action, so a user acting on a stale view gets 409 "changed by X" instead of silently overriding.
* **Orphans:** on start, FFmpeg processes owned by the service user whose command line references our playlist directory are terminated. On shutdown every publisher gets SIGTERM, then SIGKILL after 5 s, and the desired state is kept.
* **MediaMTX `overridePublisher: no`**: a second publisher on a path is rejected rather than hijacking it. Publishing is only allowed from 127.0.0.1.
* **Uploads:** disk space is reserved (free − safety reserve − other in-flight uploads) before any byte is written. Data goes to a temp file on the same filesystem and is hard-linked into place only when complete, so a file is never visible half-written and an existing file is never overwritten.

## 5a. Start spacing (edge de-duplication)

The edge discards identical frames that arrive together from several cameras. Two rules keep cameras apart (`services/start_gate.py`):

* **One start queue.** Every publisher start goes through one first-in-first-out queue (`StartGate`). That covers start, restart, create, Run N cameras, Start all, crash retries, MediaMTX coming back and boot recovery, because all of them go through `_spawn`. A start is launched only once the previous one is **on air** (MediaMTX reports the path ready) **and** `RTSPM_START_SPACING_S` (2 s) has passed. Measuring from "on air" rather than from the launch makes the gap independent of FFmpeg's start-up time (0.3–1 s). If the previous start isn't on air within `RTSPM_START_READY_WAIT_S` (5 s), the queue moves on. Requests return at once: queued streams are `STARTING` with `runtime.start_queue = {position, eta_s}` ("Queued #3 · starts in ~5 s"). Stop or delete takes a queued stream out at once. Re-adopted publishers (after a backend restart) are never queued, because they never stopped.
* **Same-video phase rule.** Streams of the same file and segment loop with the same period, so a camera restarted later can land on the frame another camera is showing (for example, a restart 60 s later on a 30 s loop). Before launching, the queue works out which frame every running stream of that video shows. For each one it takes the earliest `progress report time − media time sent`, the same measurement for every stream so the systematic error cancels. It also learns how long a new publisher takes to send its first frame (FFmpeg ≥ 7 sends a 0.5 s initial burst). Then it delays the start until the new camera is at least spacing + `RTSPM_START_PHASE_MARGIN_S` (2.25 s) of content away from all of them. If the clip is too short for that many cameras (6 cameras need more than 13.5 s), it waits at most 2 × 2.25 s for the least-bad moment, starts anyway, and sets `runtime.spacing_warning` and a `stream.spacing_short` event.
* **Cost.** Starts are one at a time, about 2.3–2.5 s per copy-mode stream. Measured: 6 cameras of one video were on air within 12.2 s, and a reboot recovery of 16 streams took 36.6 s (gaps 2.33–2.39 s). Around 48 streams fit the ticket's 2-minute reboot target, and 100 streams take about 4 minutes.
* **Not covered.** Streams of the same file with *different* segments have different loop lengths and drift relative to each other, so they can briefly show the same frame from time to time. Only identical file + segment pairs are kept apart.
* **Measured end to end** (`tests/integration/test_start_spacing.py`, which matches every reader's decoded frames to source frame numbers): the closest pair of 6 cameras was 2.28 s apart; a restart placed on a loop multiple was 2.24–2.32 s apart (4 runs); the closest of 8 cameras after reboot recovery was 2.20 s apart.

## 5b. Lead-in (slate before the video)

Proven in the Phase 1 spike (`docs/SPIKE_LEADIN.md`); built in `services/slate_service.py` and the lead-in section of `stream_supervisor.py`.

* **Per stream:** `lead_in_mode` `none` | `fixed` | `hold`, `lead_in_s` (0–120, default 5), `hold_max_s` (hold: start anyway after this, default never). New streams default to **fixed 5 s** (`RTSPM_DEFAULT_LEAD_IN_MODE`, `RTSPM_DEFAULT_LEAD_IN_S`). Streams that existed before migration v5 keep `none`. Changes apply at the next start or restart.
* **Slate:** a 10 s H.264 MP4 per stream, black with the stream name and a running clock (`drawtext`), or a colour band from the name plus a counter where FFmpeg has no drawtext (Homebrew's macOS build). Every camera's slate frames are unique, so the edge doesn't drop them as duplicates. It's made with exactly the publisher's width, height, frame rate and H.264 profile. For transcoded streams it goes through the same scale filter, so readers see no format change at the switch. Cached in `<data_dir>/slates/<stream>-<hash>.mp4` (0644: MediaMTX runs as its own user on the systemd install; `install.sh` makes `/var/lib/rtsp-manager` 0751 and `slates/` 0755). It's rebuilt when the name, video, segment or output changes, and deleted with the stream.
* **On the URL:** on start the backend adds the stream's path through the MediaMTX API with `alwaysAvailable` and the slate file (`runOnDemand` off, which MediaMTX requires). Readers can connect at any time and get the slate. When the publisher starts, MediaMTX switches the **same reader session** to the video, and back to the slate if the publisher stops or crashes. **Stop**, **delete**, a completed play-once stream and an invalid source remove the path, so the URL fails exactly as it did before lead-ins existed. MediaMTX forgets API-added paths when it restarts, so the backend re-adds them on "MediaMTX up" and checks every 5 s.
* **Fixed:** the slate goes on the URL, and after `lead_in_s` the video start enters the start queue (§5a). The slate therefore shows for at least `lead_in_s`, and longer if cameras are queued.
* **Hold:** `WAITING_FOR_VIEWER` until MediaMTX lists an RTSP reader on the path (the dashboard's HLS preview doesn't count). Then `LEAD_IN` for `lead_in_s`, then the video. **Start video now** (`POST /api/streams/{id}/go`) skips the wait. A `hold_max_s` timeout starts the video anyway.
* **Restart from the UI:** the path stays, so the edge stays connected on the slate, the lead-in runs again, then the video starts. **Publisher crash:** the edge sees the slate, and the retry brings the video back with no lead-in (measured: 0.9 s, same reader session, video from the start frame). **Boot recovery** runs every stream's lead-in again; a stream counts as recovered once its slate is on the URL.
* **Measured** (`tests/integration/test_lead_in.py`, readers' frames matched to the source):
  * Fixed 5 s: the reader joined during the lead-in and got 4.2 s of slate, then the video from exactly the configured start frame, with no lost or repeated frame.
  * Hold: no publisher until a reader connected; the viewer was seen within 1.1 s and the video started 3 s later.
  * Restart and crash: one reader session saw video → slate → video → slate → video.
  * Slates of 3 cameras: no identical frame across cameras. Their videos are still ≥ 2.2 s apart (§5a).
  * The switch pause (80–160 ms of extra slate, no lost video frame) was measured in the spike and accepted.
* **Not verified here:** the real 5.x edge (see the edge check in `docs/SPIKE_LEADIN.md`), and VLC.

## 6. Source-file lifecycle

Each stream stores the fingerprint of its file (size, mtime_ns, inode, device, and a SHA-256 of size + first/last MiB). A watchdog compares it every `source_check_interval_s` (3 s):

| Event | Detection | Behaviour |
|---|---|---|
| deleted / moved | stat fails | stop, ERROR "deleted or moved" (measured: 0.6 s) |
| replaced (new inode) | inode differs | stop, ERROR "replaced"; Restart re-validates the new file |
| modified / truncated | size or mtime differ | stop, ERROR |
| permission lost | `os.access` | stop, ERROR |
| renamed inside the library | scanner matches inode+size | the video keeps its id and the new path; a running stream is stopped (the playlist re-opens the file by path each loop) and a Restart uses the new name |
| copied in but still being written | mtime newer than `settle_seconds`, or size still changing | not offered until stable |

Other streams are never affected.

## 7. Recovery

**Backend restart / upgrade (not a reboot):** with MediaMTX running separately (systemd), `KillMode=process` stops only the backend. Publishers keep playing. On start the backend first marks every desired-running stream "recovering", then **adopts** each publisher whose runner and FFmpeg are alive and whose command line equals what the current configuration would start. A publisher with an outdated command is replaced (one brief reconnect), and anything else matching our run directory is terminated. Boot recovery owns startup exclusively (`supervisor.recovering`), so the MediaMTX "up" handler cannot race it into creating a second publisher. Measured: an RTSP reader stayed connected through a full backend stop/start (max gap 80 ms, same PID); under systemd in Debian, `systemctl restart rtsp-manager` kept PID and stream.

**Boot:** systemd starts MediaMTX, then the backend. The backend reaps orphans, normalizes runtime status, waits for the Control API (starting MediaMTX itself if it manages it), then starts every desired-`RUNNING` stream through the start queue (§5a: one at a time, 2 s after the previous is on air). Each stream is verified as `ready` in MediaMTX. `GET /api/system/recovery` reports each stream's ready time in seconds after OS boot.

**MediaMTX crash while the backend runs:** a publisher that dies while MediaMTX is unreachable is marked "waiting for MediaMTX" and republished **as soon as the API answers again**, whether or not an outage was ever declared. (systemd restarts MediaMTX in about 1 s, faster than two failed polls; found in the systemd container test, where streams otherwise waited forever.) The API is polled every 2 s and 2 failures mark it DOWN (≤ ~5 s; measured 1.6 s after `kill -9`). Every desired-`RUNNING` stream is marked "waiting for MediaMTX". The service is started, or restarted if it is alive but hung for more than 15 s, via systemd/polkit (systemd's own `Restart=always` usually wins the race). On UP, every stream without a live publisher is republished. Publisher failures that happen while MediaMTX is unreachable are not counted as stream failures. Measured locally: MediaMTX back 2.7 s after the kill, all streams RUNNING at 2.9 s.

## 8. Security

* **Access:** login is off by default by decision of the project owner: the tool is for the office network, and anyone who reaches it has full access. Everything below about accounts applies only with `RTSPM_AUTH_ENABLED=true`. Network isolation is therefore the main control, and MediaMTX still only accepts publishing from the server itself.
* Roles `admin` / `operator` / `viewer`; an admin Users page (the last admin cannot be removed or demoted). Login throttling is per user (5 free failures, then 30 s doubling up to 15 min) and per address (30), so one person cannot lock out everyone behind nginx or office NAT. `X-Forwarded-For` is trusted only from a loopback proxy.
* Optional HTTPS through nginx (`install.sh --tls`), with Secure session cookies and HSTS.
* Dashboard login: users file with PBKDF2-SHA256 hashes (200k iterations); roles `operator` and `viewer`. Sessions use an HMAC-signed HttpOnly SameSite=Strict cookie. HTTP Basic is accepted for scripts. `/api/health` is the only unauthenticated endpoint and returns no sensitive data.
* No shell anywhere. Every subprocess call is an argv list. Stream names are restricted to `[A-Za-z0-9_-]`, upload names are sanitized, and paths in the ffconcat playlist are quoted, with control characters rejected.
* The API never accepts filesystem paths. Videos are referenced by DB id, every resolved path must stay under the library root, and symlinks are ignored.
* Server paths are stripped from user-facing error messages. Passwords are never logged or returned.
* Both services run as dedicated non-root users under systemd hardening (`ProtectSystem=strict`, `NoNewPrivileges`, no capabilities). The backend may only start, restart or stop `mediamtx.service` (polkit rule).
* MediaMTX exposes only RTSP :8554. The Control API and metrics bind to 127.0.0.1. RTMP, HLS, WebRTC, SRT, MoQ, pprof and playback are disabled, which a test asserts. Publishing is loopback-only.

## 9. Database schema (SQLite, `PRAGMA user_version` = 5)

**videos**: `id`, `rel_path` UNIQUE, `size_bytes`, `mtime_ns`, `inode`, `device`, `quick_hash`, `status` (READY|INVALID|MISSING), `error`, `container`, `duration_s`, `width`, `height`, `fps`, `fps_text`, `is_vfr`, `video_codec`, `video_profile`, `pix_fmt`, `bit_depth`, `field_order`, `bitrate_bps`, `audio_codec`, `keyframe_interval_s`, `compat_mode` (copy|transcode|reject), `compat_reasons` JSON, `compat_warnings` JSON, `probe_json`, `thumbnail`, `created_at`, `updated_at`, `last_seen_at`.

**streams**: `id`, `name` UNIQUE NOCASE, `video_id` → videos (SET NULL), `source_rel_path`, `source_fingerprint` JSON, `start_offset_s` (requested, CHECK ≥ 0), `effective_start_s` (keyframe), `end_offset_s` (CHECK > start), `playback_mode` (loop|once), `playback_speed` (CHECK = 1.0), `force_transcode`, `output_mode`, `desired_state` (RUNNING|STOPPED), `status`, `status_detail`, `pid`, `last_error`, `loop_count`, `restart_count`, `ffmpeg_command` JSON, `config_json` (effective end, outpoint, period, compat warnings, fps, size), `version`, `created_at/by`, `updated_at/by`, `last_start_at`, `last_stop_at`, `running_since`.

**events**: `id`, `ts`, `level`, `kind`, `stream_id`, `stream_name`, `username`, `message`, `data_json` (pruned to 20,000 rows).

**v2:** `streams.owner`, `streams.note`, `streams.protected`, `streams.group_id` → **stream_groups** (`id`, `name` UNIQUE NOCASE, `description`, `created_by`, `created_at`). The owner is backfilled from `created_by`.

**v3:** **status_history** (`id`, `stream_id`, `ts`, `status`, `detail`): one row per status change, pruned after 7 days. It feeds the 24 h availability bar and `GET /api/streams/{id}/history`.

**v4:** analytics for the dashboard's left taskbar. **analytics_categories** (`id`, `name` UNIQUE NOCASE, `sort`), **analytics** (`id`, `category_id` → categories (RESTRICT), `name` UNIQUE NOCASE, `description`, `sort`), **analytics_slots** (video placeholders: `id`, `analytics_id` → analytics (RESTRICT), `title`, `description`, `video_id` → videos (SET NULL), `sort`, `created_by`, `created_at`, `updated_at`) and `streams.slot_id` → analytics_slots (SET NULL). The migration seeds Traffic, People, Earth Science and Parking with their 17 analytics (`ANALYTICS_SEED` in `migrations.py`). Deletes are refused while something depends on the row, so a placeholder can't vanish under running cameras; deleting a video only empties its placeholders.

**v5:** lead-in: `streams.lead_in_mode` (CHECK none|fixed|hold, default `none` for existing rows), `lead_in_s` (CHECK 0–120, default 5), `hold_max_s` (NULL = no timeout). New streams take `RTSPM_DEFAULT_LEAD_IN_MODE` (fixed).

Users stay in the hashed users file (not SQLite): atomic writes, works with the CLI and is backed up nightly.

## 9a. Observability

* **Live preview:** browsers can't play RTSP, so the dashboard plays the same video as HLS (`hls.js`; native in Safari). MediaMTX's HLS server binds to loopback and the backend proxies it at `/api/streams/{id}/live/`, so no port is added and the login applies. There is no re-encoding, and HLS is generated only while someone watches (closed after 60 s idle). Preview sessions are excluded from viewer counts. MediaMTX needs 3 segments before the first playlist and a segment can't be shorter than the keyframe interval, so the first picture takes about 4.5 s with 2 s keyframes and 20 to 30 s with 10 s keyframes. The RTSP URL isn't affected.
* **Viewers:** the count is RTSP readers from MediaMTX (the edge, VLC, test tools). Each reader shows its address and, when reverse DNS knows it, its host name (looked up in the background, cached 10 min). The day's peak is kept per stream in memory, so it restarts at 0 after a backend restart. Dashboard preview watchers are counted separately: the backend notes each distinct browser (address + user agent) fetching the stream's HLS playlist and counts those seen in the last 20 s.
* **Alerts** (`RTSPM_ALERT_WEBHOOK_URL`): stream errors, lost source files, stalls, MediaMTX down/up, boot recovery result, new resource warnings. Rate-limited per (kind, stream).
* **Prometheus** `/api/metrics`; **bitrate** per stream (ingest/egress from MediaMTX byte counters) and total egress, with a warning above `RTSPM_NET_WARN_MBPS`.
* **Availability** per stream (24 h timeline); live preview thumbnails taken from the source file at the current playback position (no RTSP reader, so viewer counts stay accurate).

Migrations live in `backend/app/database/migrations.py`. Append a new numbered SQL entry; never edit a shipped one. A unit test checks that the ORM matches the migrated schema.

## 10. Risk register

| Risk | Mitigation | Residual |
|---|---|---|
| The edge rejects the stream-copied H.264 profile or level | Configurable copy whitelist and force-transcode per stream | Must be confirmed on the edge (EDGE_VALIDATION E-01) |
| Loop point disturbs the edge decoder | Keyframe-aligned DTS/PTS cut gives identical frames every loop, no duplicate PTS, no gap; 30-minute soak test | 30 min on the real edge still required (E-04) |
| Edge does not retry after 404 / hold timeout | runOnDemand hold of 60 s | Edge behaviour after 60 s unknown (E-12) |
| Transcoding load on the VM | Transcode cap (4), CPU warnings, measured copy vs transcode cost | VM must be measured |
| Long GOP sources (e.g. 8 s) | Warning shown; start/end snap to keyframes | A reader may wait one GOP for its first picture |
| Backend restart drops every camera | Publishers run behind independent runners and are re-adopted | A changed publish command after an upgrade causes one reconnect |
| One user stops another's demo | Owner + protected flag + note; audit log | Unprotected streams remain shared by design |
| Disk fills with uploads | Space reserved before writing; admin delete to trash; disk warnings + alerts | Files copied in over SMB/scp bypass the reservation |
| Loop playlist exhaustion | Playlist covers 7 days (≤ 50,000 iterations), then a fresh publisher starts | One brief reconnect per week on long-running loops (documented) |
| Network-mounted video storage | Seeks use container indexes; identity checks tolerate NFS | Not tested on NFS |

## 11. Known limitations and future improvements

* Start/end positions are keyframe-accurate, not frame-accurate (even when transcoding, to keep loop points clean). A future "frame-accurate" option could pre-render a segment file.
* Very long-running loops reconnect once when the loop playlist runs out, after `RTSPM_LOOP_PLAYLIST_HOURS` (7 days; up to 50,000 iterations).
* The loop count of a transcoded stream relies on passthrough timestamps (the default). With `RTSPM_OUTPUT_FPS_MODE=cfr` it is still media-time based, but frame-rate conversion may duplicate or drop frames.
* Video files cannot be deleted from the UI (deliberate: "do not delete user data"). Remove them on the server.
* There is a single backend instance; there is no HA or clustering. That's fine for one Proxmox VM.
* The interactive frame preview runs FFmpeg per request (limited to 2 at a time); it is not designed for heavy concurrent scrubbing.
* Future: SSE/WebSocket push instead of 2 s polling; Prometheus exporter for the backend's own metrics; per-user stream ownership if teams grow.
