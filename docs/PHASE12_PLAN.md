# Phase 12: analytics-first main page, cleaner UI, video naming, fast bulk controls

Status 2026-11-19. Nothing here has touched the live rig (8081/8555) yet. Item 4 is built and running on the
preview rig (8082/8556); everything else is a plan waiting for the decisions at the end.

## 0. Hard constraint: testers and devs keep streaming

The live rig serves ~44 running streams to testers and edges. Every change ships in a way that does not stop them.

| Release kind | What it can contain | Effect on running streams | How it ships |
|---|---|---|---|
| **A. Frontend only** | Pages, layout, buttons, naming guide, upload name builder | **None.** FFmpeg, MediaMTX and the backend keep running | Build the dashboard, verify on preview (8082), copy the new `dist/` into the running `rig02` container (assets first, `index.html` last). People see it on their next page refresh. Rollback: copy the previous `dist/` back |
| **B. Backend** | New API endpoints (rename), upload-name enforcement, DB columns | **One short interruption**: the container restarts, so every RTSP reader drops and the streams republish automatically. Starts are spaced 2 s apart, so with ~44 streams the last one is back after **~1.5–2 min** | Verified on preview first, rollback point taken (image tag + `state` tar, runbook Phase 6), then **one** restart at an announced time |

Plan: put as much as possible into kind A, and batch every backend change into a **single** kind-B restart.

| Release | Contents | Kind |
|---|---|---|
| R1 | Item 4 (done) · main page by analytics · slimmer navigation · cleaner stream/library cards · Naming guide button · upload name builder (client side) | A, no interruption |
| R2 | Rename API (single + batch, dry run) · server-side name check on upload · analytics short codes · `original_name` and `previous_names` columns · converted copies named `_720p` instead of ` (720p)` | B, one ~2 min window |
| R3 | Apply the agreed renames to the existing 66 videos through the R2 API | none (no restart; see §3.4) |

## Urgent (found while planning): the VM's CPU is overloaded

Load average was **107 on 40 vCPUs** on 2026-11-19 ~12:58. The live rig alone used ~30 cores: **20 streams are transcoding 1080p→720p
live**, 5 of them from the same `tirupathitest.mp4` (vehiclealpr-10…14), plus `anpr_secure*`, `gate`, `MLFF_5`. On
2026-11-17 `max_transcode_streams` was raised from 4 to 30. The hand-over recommends 4–8.
This slows the colleague's services on the same VM too, and can cost the edges frames.

Fix (not done, needs your OK because it restarts the affected streams for a few seconds each):
1. In the library, **Make 720p copy** once for each source of those 20 streams (≈11 distinct files, 2 at a time).
2. Point each stream/analytics slot at its copy: the stream then runs in copy mode at near-zero CPU.
3. Put `max_transcode_streams` back to ~8.

## 1. UI/UX: why it feels "dumped in one place", and the fix

What the screenshots show:
- The sidebar does three jobs: navigation (4 links), a 20-item analytics tree (some with 0 videos) and the RTSP address picker.
- The Streams page is one flat wall of identical cards. Each card has 4 icon buttons, a URL bar and ~7 metadata items, with no grouping.
- Every page invents its own header and toolbar. Empty things (analytics with 0 videos) get as much space as busy ones.

Principles for the rework:
1. **Group by what people think in: analytics.** Testers look for "the ALPR streams", not stream #157.
2. **One primary action per area.** Move secondary actions into a `⋯` menu (clone, delete, details).
3. **Hide the empty, fold the idle.** Analytics with no videos collapse into one "6 empty analytics" line.
4. **Dense by default where people scan.** Add a table view to Streams (50 rows fit one screen).
5. **Same header everywhere.** Title, one-line description, primary button on the right, filters below.

Concrete changes:
- **Navigation:** replace the wide sidebar with a slim rail or top tabs: *Overview · Streams · Library · Health · Events*.
  The analytics tree leaves the sidebar (it becomes the main page, §2). The RTSP address picker moves into the top bar's settings menu.
- **Stream card, compact:** name, status, analytics chip, URL with copy. The second line is only the source video and viewers.
  Loops, uptime and bitrate move to hover/details. Actions are Start/Stop plus `⋯`.
- **Streams page:** sections by analytics (collapsible, "Not in an analytics" last), grid/table toggle, the new bulk bar (§4).
- **Library:** filter chips instead of 4 dropdowns, and fewer buttons per card. Non-standard names get a "Rename" chip (§3).
- Keep the 375 px phone layout working (it was tested in Phase 11).

## 2. Main page with all analytics as sections (implementation plan)

**Status 2026-11-19: built**, steps 1–5 (step 5, the Streams page, done the same day: sections per analytics with
Start/Stop per section and "Not in any analytics" last, a "Group by analytics" switch, card/table toggle remembered
per browser, compact cards with Start/Stop + a ⋯ menu for Restart/Clone/Delete, one detail line with the rest on
hover, natural name order). Tests: `components/bulk.test.tsx` "Streams page layout". Checked in Chromium with the
live rig's 50 streams (read-only): 50 table rows, no sideways scroll at 390 px. Files:
`pages/OverviewPage.tsx` (new, route `/`), `components/AppShell.tsx` (analytics tree removed from the sidebar,
"Overview" link, sidebar 14 rem), `pages/AnalyticsPage.tsx` (breadcrumb). Tests: `pages/analytics.test.tsx`
"Overview (main page)", 5 cases. Checked in Chromium with the live rig's data (read-only, GET only) at 1440 px and 390 px.
It works with the **current** live backend too: without the naming API the dashboard hides the Naming guide and
uploads without the naming step, so it can be hot-deployed (no restart) before the R2 window if wanted.

Route `/` becomes **Overview** (today it redirects to `/streams`). Wireframe:

```
┌ ● All systems normal  CPU ▃ 18%  Streams 44/80 ──────── [Naming guide] [Upload] [+ New stream] ┐
│ Overview   Streams   Library   Health   Events                                                  │
├────────────────────────────────────────────────────────────────────────────────────────────────┤
│  44 live · 0 need attention · 7 viewers · 20 transcoding                                         │
│  [ Find analytics, video or stream…  / ]     All · Traffic · People · Earth Science · Parking    │
│                                                                                                  │
│  ▾ Traffic                                     18 live of 19 streams     [▶ Start all] [■ Stop all]│
│   ┌ VehicleALPR ────────────┐ ┌ Trigger line videos ───┐ ┌ TrafficCongestion ──────┐           │
│   │ ● 15/16 live · 👁 7      │ │ ● 3/3 live             │ │ 3 videos · no streams    │           │
│   │ ▣ ▣ ▣ ▣  +9              │ │ ▣ ▣ ▣                  │ │ ▣ ▣ ▣                    │           │
│   │ [Open] [▶ 1] [■ 15] [⧉ URLs]│ │ [Open] [■ 3] [⧉ URLs]  │ │ [Open] [Run streams]     │           │
│   └──────────────────────────┘ └────────────────────────┘ └──────────────────────────┘           │
│   + 2 empty analytics (VehicleDwell, …)                                                          │
│  ▾ People                                        4 live of 4 streams     [▶ Start all] [■ Stop all]│
│   … tiles …                                                                                      │
│  ▸ Not in any analytics: 31 videos, 22 streams                                    (folded)       │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Each **analytics tile** shows the name, description (one line), videos, live/total streams, viewers, a red dot for problems,
up to 4 thumbnails, and actions: Open (the existing `/analytics/:id` page), Start all / Stop all, Copy URLs.
Each **category header** has Start all / Stop all for the whole category, with a confirm that names the reader count.

Data: all from what the dashboard already polls (`/api/analytics` tree with counts and `slot_ids`, `/api/streams`,
`/api/videos` with each video's `analytics`). **No backend change**, so it ships in R1 without an interruption.
Start/stop per analytics uses the existing `POST /api/analytics/{id}/start|stop`; per category uses `POST /api/streams/bulk`.

Steps:
1. `pages/OverviewPage.tsx` + `components/AnalyticsTile.tsx`; route `/` → Overview; folded categories remembered (localStorage, like today).
2. Move the search ("/" shortcut) and "New analytics" from the sidebar to the Overview header.
3. Slim the navigation (`AppShell.tsx`): drop `AnalyticsNav` from the sidebar; the RTSP address picker goes into the settings menu.
4. Analytics page gets a breadcrumb back: *Overview › Traffic › VehicleALPR*.
5. Streams page: sections by analytics + table view; compact cards.
6. Tests (vitest) for the Overview counts/fold/search/bulk; browser e2e screenshots at phone/laptop/desktop (`scripts/e2e_live.sh --browser` against preview).
7. Ship as release kind A.

## 3. Video naming convention (for discussion)

### 3.1 The problem today
66 videos and 8 duplicate sets (17 files that are really 8 videos), for example `anpr_secure.mp4` = `toll_s1_anpr_secure.mp4`
and `gate.mp4` = `Secure_parking5.mp4` = `boom_Secure_parking5.mp4`. Names mix spaces, brackets, `Copy of …`, and
`vidssave.com …`. Some say the analytics, some the site, most neither.

### 3.2 Proposed format: WHAT _ WHERE _ NUMBER

```
WHAT_WHERE_NN.mp4                     read it as: "what it tests, where it's from, which one"

ALPR_toll-s1_01.mp4                   ALPR, toll plaza camera s1, clip 1
FALL_office_01.mp4                    slip-and-fall, office, clip 1
FIRE_forest_01.mp4                    fire/smoke, forest, clip 1
JAM_dhaka_01_web.mp4                  traffic jam, Dhaka, clip 1, downloaded from the internet
CROWD_railway_01_h264.mp4             crowd, railway station, clip 1, H.264 copy
```

Three parts, always in this order, joined by `_`:

| Part | What goes in it | Rule |
|---|---|---|
| **WHAT** | The analytics it tests | A code from the table below, UPPERCASE, 2–8 letters |
| **WHERE** | The site or camera it was recorded at (`toll-s1`, `tirupati`, `office`). If nobody knows, a one-word description of the scene (`bag`, `overcrowd`) | lowercase, letters/digits, `-` between words, max 14 characters |
| **NN** | Clip number for the same WHAT + WHERE | `01`–`99`; **the app fills it in** |
| *tag* (optional) | Only from a short fixed list: `720p` `1080p` `16x9` `h264` `4k` `web` `night` `12fps` (any `<n>fps`) | The app adds `720p`/`16x9`/`h264` itself when it makes a copy; `web` = downloaded from the internet |

That's all. **No** dates, durations, "clipped", "test", "copy of", "final", spaces or brackets: the app already shows
the length, resolution, codec, uploader and upload date of every video.

Names come out at 13–25 characters (today: up to 73). Sorting the library by name groups it by analytics, then by site.

**WHAT codes**: short and readable as words, so nobody needs to look them up:

| Code | Analytics in the app | | Code | Analytics in the app |
|---|---|---|---|---|
| `ALPR` | VehicleALPR | | `FALL` | SlipAndFall |
| `ANPR` | videoANPR | | `CROWD` | CrowdDensity |
| `JAM` | TrafficCongestion | | `REGION` | RegionCount |
| `DWELL` | VehicleDwell | | `LINE` | LineCount |
| `TRIG` | Trigger line videos | | `PPE` | PPE |
| `EV` | Charging station | | `ABANDON` | AbandonedObject |
| `PARK` | Parking | | `FENCE` | DigitalFence |
| `RLVD` | Red-light violation (new) | | `TAMPER` | Camera Tampering |
| `FIRE` | FireSmoke | | `ATTR` | Human Attributes |
| `ELEPHANT` | Elephant | | `MISC` | not decided yet |

A clip used by several analytics is named after its **main** one; the app's analytics tags show the others.

Machine check (used by the upload form and the server):
`^[A-Z]{2,8}_[a-z0-9]+(-[a-z0-9]+)*_\d{2}(_(720p|1080p|16x9|h264|4k|web|night|\d+fps))*\.<ext>$`, WHERE ≤ 14 characters.

**Recognisable at a glance in the app too:** the library and stream cards show the name split into chips
(`ALPR` · `toll-s1` · `#01` · `720p`), with the code coloured by its analytics, and the Naming guide lists every code and every known WHERE.

**What does not change:** stream names, and with them every RTSP URL (`rtsp://192.168.29.105:8555/vehiclealpr-5`).
Edge and tester configurations stay valid. Only the video file names change.

### 3.3 Existing videos: proposed new names
[`docs/naming/video-renames-proposed.csv`](naming/video-renames-proposed.csv) has one row per video: current name,
proposed name, duplicate set, analytics, streams using it, resolution and duration.
- 49 rows are filled in from the current names; **17 rows need input** (`needs_input = yes`): WHERE is `tbd`, or the analytics
  is uncertain (`MISC`, REGION vs LINE). Whoever knows where the footage came from should fill those in.
- Duplicates get `_01`, `_02`… for now. Whether to delete the extra copies is a separate decision, because some of them have running streams.

### 3.4 How the rename is applied safely (R2 + R3)
A plain `mv` would **break running streams**: the library follows the file (same inode), but each stream keeps the old path,
and its next loop or restart fails with "Source file was deleted or moved". So the rename is a backend operation:

- `POST /api/videos/{id}/rename {"name": …}` and `POST /api/videos/rename-batch` (CSV/JSON, `dry_run` first).
- Under the supervisor's lock: validate the name, `os.rename` in the same folder, update `videos.rel_path` **and every stream's `source_rel_path`**, keep the old name in `previous_names` (search still finds it), log a `video.renamed` event.
- A rename in place keeps inode, size and mtime, so the running FFmpeg (file already open) and the stream's fingerprint check both stay valid. **No stream restarts, no URL changes.**
- Tested on preview first with a stream looping across the rename. Rollback is the same batch with the columns swapped.

### 3.5 From now on: uploads must use the convention
- **Upload dialog name builder:** pick WHAT (code) → pick or type WHERE (known places offered first) → the app fills `NN`, adds tags it can detect (`4k`, `h264`) and shows the final name live.
  The original file name is kept as metadata (`original_name`). Uploading from an analytics page prefills the code.
- **Server check (R2):** uploads through the API/curl with a non-conforming name get a 400 that explains the format and suggests a name.
- **Files copied straight into the video folder** (NAS/scp) can't be blocked, so the library marks them "Name not standard" with a one-click Rename.
- **"Naming guide" button** in the top bar of the main page opens a page with the format, the code table, examples, and a box that checks a name as you type.

## 4. Fast select / bulk start-stop (DONE, on preview 8082, not on live)

- Streams page: a bulk bar that is always visible. **Select all (N) / Deselect all** is one checkbox (tri-state).
  Next to it, **Start N / Stop N / Restart** act on all streams the current filters show (for example "Live" + "VehicleALPR").
- A checkbox on every card for operators; no more separate "Select" mode. The bottom bar for a selection now has **Deselect all**.
- Library cards: for every video with streams, **"2/3 live · ▶ Start 1 · ■ Stop 2"** starts or stops all streams made from that video.
- **Stop and Restart of more than one stream ask first** and say how many readers (edges/testers) will lose the feed. Start shows when capacity is short.
- Uses the existing `POST /api/streams/bulk`, so no backend change.
- Files: `frontend/src/components/BulkControls.tsx` (new), `StreamsTable.tsx`, `VideoLibrary.tsx`, tests in `bulk.test.tsx`.
- Tests: frontend 84/84 pass (80 existing + 4 new); typecheck clean; checked in Chromium against preview.

## R2 upgrade: naming convention + camera-like streams — DONE on live 2026-11-20 19:26 (104 s window, 0 errors)

**Status: LIVE since 2026-11-20 19:26.** Rollback point: image `rig02-rtsp-manager:before-r2-20261120-1926`,
`~/rig02-backups/state-before-r2-20261120-1926.tgz`, DB backup 20261120-135615. All 34 streams back in 104 s, 0 errors;
schema v8; codes assigned (RLVD too); clock-test camera created; 40 renames applied with 0 stream restarts
(undo list `docs/naming/renames-applied-20261120-193006.csv`); health PASS (clock -29 ms from NTP).
The frontend of all of this is already live (hot-copied); it switches the new parts on by itself once the backend answers.

Also in R2 (added 2026-11-19 evening, tested on preview):
- Converter and every live encoder: **no B-frames** (`-bf 0`), keyframe every 2 s: copies behave like real cameras.
- New streams default to **the file's own size** (`RTSPM_OUTPUT_DEFAULT=original`); 720p only when picked.
- Library knows **B-frames** per video (from the stored probe; no migration) and flags them.
- **H.265 to edges as-is**: admin switch on the Health page (`PUT /api/settings/codecs`), **off by default**; turn on
  only once the devs confirm their edges decode H.265. The lead-in slate is then made in H.265 too (MediaMTX keeps
  the slate's format for the path), and the browser preview reads its own small H.264 path `preview/<name>`, made
  only while someone watches and stopped ~60 s after (tested: edges get `hevc`, browser gets `avc1` 960×540).
- Duplicate uploads refused **by content** (size + SHA-256 of first/last MiB, the library's quick_hash), whatever the name;
  `/api/videos/upload/check` takes `quick_hash` and answers `duplicate_of`; the video list includes `quick_hash`.
- The app's own RTSP reads (snapshots, health test, preview copy) carry `?rtspstudio=internal` and are not counted as viewers.
- Automatic **full** health test once a day (`RTSPM_HEALTH_FULL_INTERVAL_H`, default 24), quick check every 10 min as before.
- **Server clock in picture** (explicit option per stream when creating it; IST or UTC; existing streams: off, IST if
  switched on later): wall-clock time with ms of the moment each frame leaves the rig (FFmpeg `setpts=RTCTIME` +
  drawtext); re-encodes that stream (~1-2 cores at 720p, 4 at 1080p; counts toward the transcode limit).
  `GET /api/system/time` for the dashboard's Rig clock (±few ms); health check measures the clock against
  `RTSPM_NTP_SERVER` (default ntp.ubuntu.com, the VM's own server).
- MediaMTX `hlsAlwaysRemux: yes` in `deployment/mediamtx.yml` (already applied on live at runtime: preview starts in
  ~0.1 s; this makes it survive the restart).

What R2 brings: upload name check (enforce), the "Name your videos" dialog on every upload, Naming guide button,
name chips + Rename in the library, analytics codes, `POST /api/videos/{id}/rename` and `/api/videos/rename-batch`,
converted copies named `_720p` / `_h264`, and #4 (select all / fast start-stop). Schema v7 → v8 (3 nullable columns).

How a rename keeps streams running: each stream's playlist now reads its video through its own symlink
(`run/streams/<id>/source.mp4`). A rename hard-links the new name, re-points the symlinks and the running
publishers' plans, updates the database, then removes the old name. Tested:
- `tests/integration/test_rename_live.py` (real FFmpeg + MediaMTX): two looping streams, video renamed mid-stream →
  same PIDs, 0 restarts, ≥2 more loops through the new name, RTSP feed continuous.
- Preview rehearsal in Docker: `Fire (720p).mp4` → `FIRE_forest_01_720p.mp4` under the running `Fire_720p` stream →
  same PID, 0 restarts, 2 loops after the rename, 1,434 packets in 60 s (full 23.976 fps), largest gap 0.21 s.
- Migration v7 → v8 rehearsed on the preview's real database; codes assigned on start-up.

Steps (≈2 min interruption at step 3 only; everything else is live-safe):

```bash
cd ~/rig02
# 1. rollback point (no interruption)
docker tag rig02-rtsp-manager:latest rig02-rtsp-manager:before-naming-$(date +%F)
docker run --rm -v rig02_rig02-data:/data -v ~/rig02-backups:/b alpine \
    tar czf /b/state-before-naming-$(date +%F-%H%M).tgz -C /data state
# 2. build the new image while live keeps running (no interruption)
docker compose build rig02
# 3. THE WINDOW: recreate the container; all RTSP readers drop, streams come back 2 s apart (~1.5-2 min for ~44)
docker compose up -d rig02
watch -n2 'curl -s localhost:8081/api/streams | python3 -c "import json,sys; v=json.load(sys.stdin); print(sum(s[\"status\"]==\"RUNNING\" for s in v), \"/\", sum(s[\"desired_state\"]==\"RUNNING\" for s in v), \"running\")"'
# 4. checks: the "RLVD" analytics (created by testers) got code RLVD; H.265 switch is off; preview is instant
curl -s localhost:8081/api/analytics | python3 -c "import json,sys; print([(a['name'],a.get('code')) for c in json.load(sys.stdin) for a in c['analytics']])"
curl -s localhost:8081/api/settings/codecs
# 4b. the CLOCK test camera (latency checks): a 60 s test pattern, streamed with the IST clock (~1 core)
docker exec rig02 sh -c 'ffmpeg -hide_banner -loglevel error -f lavfi -i "testsrc2=size=1280x720:rate=25:duration=60" \
    -c:v libx264 -preset veryfast -pix_fmt yuv420p -bf 0 -g 50 -keyint_min 50 -sc_threshold 0 -movflags +faststart \
    /tmp/c.mp4 && touch -d "1 hour ago" /tmp/c.mp4 && mv /tmp/c.mp4 /data/videos/MISC_clock-test_01.mp4'
curl -s -X POST localhost:8081/api/videos/refresh
VID=$(curl -s 'localhost:8081/api/videos?limit=1000' | python3 -c "import sys,json;print([v['id'] for v in json.load(sys.stdin) if v['rel_path']=='MISC_clock-test_01.mp4'][0])")
curl -s -X POST localhost:8081/api/streams -H 'content-type: application/json' \
    -d "{\"name\": \"clock-test\", \"video_id\": $VID, \"clock\": true, \"clock_tz\": \"IST\", \"lead_in_mode\": \"none\"}"
# 5. renames (no interruption): dry run, read it, then apply. The 17 needs_input rows are skipped.
python3 scripts/apply_renames.py
python3 scripts/apply_renames.py --apply
```

Rollback (another ~2 min window): `docker tag rig02-rtsp-manager:before-naming-<date> rig02-rtsp-manager:latest && docker compose up -d rig02`.
The old version runs on the v8 database as it is (new columns are ignored), and renamed files keep playing.
To also undo the renames, run the report `docs/naming/renames-applied-*.csv` back (old ↔ new) before rolling back.

## Decisions needed

1. ~~Naming format and codes~~ **agreed 2026-11-19** (both `ANPR` and `ALPR` kept, `RLVD` added).
2. **Who fills the 31 `needs_input` rows** in the CSV (sites, analytics)?
3. **Uploads with a bad name via API/curl**: reject (strict) or accept with a warning for a transition week?
4. **R2 restart window**: when can every RTSP reader drop for ~2 minutes?
5. ~~CPU overload~~ **done 2026-11-19**: every stream on a camera-style copy, 0 live transcodes, limit 8.
6. **Duplicates**: keep them for now, or remove the extra copies once their streams point at one file?
7. ~~R1 hot-deploy~~ **done**: all frontend work is live.
8. **H.265 to edges**: which edges decode H.265? (switch stays off until confirmed)
