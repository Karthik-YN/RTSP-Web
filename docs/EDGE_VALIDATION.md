# Manual edge validation runbook (Video Analytics 5.x edge + portal)

These tests need the real 5.x edge and portal. Server-side behaviour is already covered by the automated suite. VLC/ffplay playback is **not** evidence of edge compatibility. Until a test below has been executed and recorded, its status is **NOT RUN**.

Fill in a copy of each record sheet (or a spreadsheet with the same columns) and attach screenshots or logs to the ticket.

## 0. Preparation (once)

1. The server is installed (DEPLOYMENT.md) and reachable by its fixed name; note `RTSP_HOST = rig02.<domain>`.
2. Generate the test clips on any machine with FFmpeg and copy them into the server's video folder:
   ```bash
   scripts/generate_test_videos.sh ./rig02-clips --large
   scp ./rig02-clips/*.{mp4,mkv,webm} admin@rig02:/data/rtsp-manager/videos/
   ```
   Also copy **one real store recording** with a burnt-in clock (NVR overlay) and, if available, real H.265 / 10-bit footage.
3. In the dashboard, press **Refresh** in the Video library and confirm every clip shows metadata.
4. On a laptop on the office Wi-Fi: `python3`, `ffmpeg`/`ffprobe`, this repository, and a phone stopwatch.
5. Note in the record: edge model and firmware, portal version, server VM size, FFmpeg version (`/api/config`).

**Edge properties to confirm first** (they feed the copy policy `RTSPM_COPY_*`):

| Property | How | Result |
|---|---|---|
| H.264 profiles decoded (Baseline/Main/High) | E-01 with `h264_25fps_audio.mp4` (High) | |
| Max resolution / fps | 1080p25 and 4K if available; 59.94 fps clip | |
| H.265 support (yes/no) | E-01 `hevc.mp4` with `RTSPM_TRANSCODE_POLICY=never` would be refused; with `auto` it is transcoded. Ask the VA team whether native H.265 is wanted | |
| RTSP transport the edge uses (TCP/UDP) | `curl 127.0.0.1:9997/v3/rtspsessions/list` on the server while the edge is connected → `transport` | |
| Retry behaviour after 404 | E-12 | |

## Standard procedure: add a camera in the portal

1. Dashboard → **New stream** → choose the video → name (e.g. `e01_h264`) → start/end → Loop → **Create & start** → **Copy URL**.
2. Portal → (site/edge) → Add camera → **Name** = the stream name; **Type/Protocol** = RTSP / generic IP camera; **URL** = the copied `rtsp://RTSP_HOST:8554/<name>`; **Username/Password** = empty (readers need none); **Transport** = TCP if the field exists; save and assign it to the edge.
3. Observe the camera status in the portal (green / grey / red) and the live view.

Record for every camera: *first-frame time* = seconds from pressing **Save** in the portal until the live view shows video.

---

## E-01 Unsupported codecs (EC-01)

| Clip | Expected server behaviour | Expected on edge |
|---|---|---|
| `h264_25fps_audio.mp4` | copy, audio dropped | green, live |
| `hevc.mp4`, `hevc_10bit.mkv`, `vp9.webm`, `av1.mkv`, `h264_10bit.mp4` | "transcode" badge, CPU warning, stream RUNNING | green, live (it receives H.264) |
| same clips with `RTSPM_TRANSCODE_POLICY=never` | refused with a clear message, no stream created | n/a |

Pass: every RUNNING stream shows video on the edge and no audio track is announced. Fail: the edge cannot decode a stream-copied or transcoded output. Note the clip and the edge error, then try "Always transcode" (Advanced) on the H.264 source to see whether the profile is the problem.

## E-02 Invalid start/end (EC-02)

In the create dialog try: start beyond the end; end before start; end beyond the duration; start = end; start = 0 with end = full duration (valid). Pass: invalid inputs show a message and **Create** stays disabled (and the API refuses them anyway); the valid boundary creates a working stream; no stray stream appears in the list.

## E-03 Frame rates and real time (EC-03)

Clips: `h264_23976.mp4`, `h264_2997.mp4`, `h264_5994.mp4`, `h264_vfr.mkv`, and the **store recording with a burnt-in clock**.

1. Publish each as a loop from 00:00:00 and add it on the edge.
2. In the live view, start the stopwatch when the burnt-in clock (testsrc shows a running counter) reads a round value; stop it 5 minutes of clock time later.
3. Pass: stopwatch = clock delta ± 2 s over 5 minutes (≤ 0.7 %). Also run `scripts/validate_rtsp.sh -t 60 <url>` on a laptop: ratio about 1.0, PASS.

| Clip | Clock start | Clock end | Stopwatch | Drift | Pass/Fail |
|---|---|---|---|---|---|

## E-04 Loop continuity, 30 minutes on the edge (EC-04)

1. Create `e04_loop` from the store recording (or `h264_2997.mp4`) with **start 00:00:04, end 00:00:15** (≈10 s loop, so ≈180 loop points in 30 minutes), Loop.
2. Add it on the edge; let analytics run on it if possible.
3. For 30 minutes, every 5 minutes record: camera status, loop count (dashboard), visible freeze at the loop point (watch 2–3 loop points live), edge disconnect events in the edge logs, and the stream's `restart_count`.
4. Afterwards collect: the dashboard stream detail (screenshot), `GET /api/streams/<id>` JSON, `journalctl -u rtsp-manager --since -35min`, and the edge's camera/decoder logs.

| Time | Camera status | Loop count | Freeze seen? | Edge disconnects | Restarts |
|---|---|---|---|---|---|
| 0 | | | | | |
| 5 | | | | | |
| … 30 | | | | | |

Pass: green throughout, 0 disconnects, no visible freeze or jump at the loop points, `restart_count` = 0, loop count ≈ 30 min / period. Fail: any drop, reconnect, decoder error or visible stall at the loop point. Note the loop count when it happened.

## E-05 Same file, two streams (EC-05)

Create `e05_a` (start 00:00:02) and `e05_b` (start 00:00:12) from the same clip; add both on the edge. Pass: both are live, showing different moments; stopping `e05_a` leaves `e05_b` green and live.

## E-06 Names (EC-06)

Try `my cam`, `cam/1`, `cam#1`, an existing name, and the same name in different case. Ask a colleague to press Create with the same new name at the same moment. Pass: all refused with a message; exactly one stream exists.

## E-07 File deleted or replaced while streaming (EC-07)

With two cameras live (`e07_victim`, `e07_other`), on the server: `sudo -u rtspmgr mv /data/rtsp-manager/videos/<victim file> /tmp/`. Pass: within ~5 s `e07_victim` shows ERROR "Source file was deleted or moved", the page still works, `e07_other` stays green on the edge. Move the file back and press Restart: live again. Repeat with a replacement (`cp other.mp4 /data/.../tmp && mv` over the original).

## E-08 Disk nearly full (EC-08)

Use a small dedicated data disk, or temporarily set `RTSPM_DISK_RESERVE_BYTES` to (free space − 100 MB) and restart the service. Upload a 200 MB file. Pass: refused before uploading, showing required/free/available; no new file in the folder or in `.uploads/`. Restore the setting afterwards.

## E-09 MediaMTX killed (EC-09)

With 4 cameras live on the edge, start `python3 scripts/measure_recovery.py urls.txt` on a laptop, then on the server: `sudo systemctl kill -s SIGKILL mediamtx`. Record: the dashboard red banner (within 10 s), the time until each camera is green again, and the Events tab (`mediamtx.down`, `mediamtx.up`, republish). Repeat with `sudo systemctl stop mediamtx` (tests the backend's own restart). Pass: detection ≤ 10 s, all cameras green again without touching the portal.

## E-10 Reboot with 16 streams (EC-10)

1. Create 16 looping streams (`e10_01` … `e10_16`, mixed clips, at least one non-zero start) and add all 16 on the edge; wait until all are green. Put the 16 URLs in `urls.txt`.
2. On a laptop: `python3 scripts/measure_recovery.py urls.txt --max 600`; wait for "16/16 streams up".
3. On the server: `sudo reboot`. Start a stopwatch when the SSH session drops.
4. Watch the edge: note when each camera turns grey and green again.
5. Afterwards on the server: `uptime -s` and `curl -s -u admin:... 127.0.0.1:8080/api/system/recovery` (save the JSON).

| Camera | Grey at | Green at | Seconds after boot (green − boot time) |
|---|---|---|---|

Pass: all 16 green within **120 s of boot** (boot time from `uptime -s`), with no manual action; same start offsets as before; any stopped or completed streams stay so. Attach the recovery CSV, the JSON and the edge screenshots.

## E-11 Two users at once (EC-11)

Two people, two laptops, same dashboard: both edit the same stream's start time at the same time; one restarts while the other stops; both create streams at the same time. Pass: the second editor gets "changed by …" (409) instead of silently overwriting; no duplicate names or publishers (`ps -C ffmpeg` shows one per stream); both screens converge within 2 s.

## E-12 Early edge connection (EC-12)

1. Create `e12_early` with **Create** but stop it immediately (or create it via the API with `start_now:false`).
2. Add the camera on the edge now; it will show offline or connecting.
3. Wait 30 s, then **Start** the stream. Record whether and when the edge turns green **without touching the camera**.
4. Repeat with a 3-minute wait (longer than the 60 s hold), to learn the edge's retry behaviour after a 404.

Pass: the edge shows video after Start without recreating or toggling the camera. Note the delay for both waits.

## E-13 Long and large files (EC-13)

Clips: `long_2h.mp4`, `large_1080p.mp4` (≈4 GB, from `--large`), and the longest real recording. Publish from 01:45:00 (or near the end), add on the edge. Pass: RUNNING within seconds; the edge picture shows the expected clock time (± one GOP); FFmpeg RSS stays small (stream detail → CPU/memory).

---

## 16-stream working-day stability (acceptance target)

Keep the 16 E-10 cameras running for a working day (≥ 8 h) with analytics on. At start, midday and end, record: all green? total restarts (`/api/streams` → sum of `restart_count`), CPU and RAM (dashboard), Events tab errors. Pass: no unexpected restarts, all green.

## Evidence to collect for the ticket

* `GET /api/streams` and `GET /api/system/recovery` JSON
* `journalctl -u rtsp-manager -u mediamtx --since "<test start>"`
* `/var/lib/rtsp-manager/logs/streams/<id>.log` of the streams under test
* Edge logs and portal screenshots (camera list with statuses)
* `recovery-*.csv` from `measure_recovery.py`, `validate_rtsp.sh` output
