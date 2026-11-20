# Time sync + server clock in the stream (plan, 2026-11-20)

## Status (2026-11-20, built)
- DONE explicit "Server clock in picture" option when creating a stream (both dialogs), IST or UTC; existing streams:
  off (IST if switched on later); switch on/off + IST/UTC on the stream page (restarts it); card chip "clock IST".
- DONE clock = real wall-clock time with ms at the moment each frame is drawn (setpts=RTCTIME + drawtext), verified:
  frame stamped 19:10:07.182 IST, read over RTSP at 19:10:07.9; ~1.1 core for 720p.
- DONE health check measures the clock against NTP (preview: PASS, -6 ms from ntp.ubuntu.com).
- DONE Rig clock: top-bar time + /clock page (IST and UTC, ms, ±4 ms measured; this device's offset shown).
- DONE CLOCK test camera: runbook step after R2 (MISC_clock-test_01.mp4 + stream clock-test).
- LIVE since R2 (2026-11-20 19:26): everything above; clock-test stream rtsp://192.168.29.105:8555/clock-test.
- OPEN: NTP on laptop/edges (`sudo timedatectl set-ntp true`); RTCP NTP use needs the devs' edge software.

Request (senior dev): sync laptop and rig clocks to NTP; have the rig burn its own clock into the streams it serves
(FFmpeg drawtext with the server time), or use a real IP camera whose on-screen clock is NTP-synced. Purpose: compare
the rig's "sent at" time in the picture with the receiver's clock -> latency, and line up analytics events with frames.

## Facts (measured)
- Rig VM: `System clock synchronized: yes`, NTP service active (systemd-timesyncd). The container uses the same clock.
- FFmpeg in the rig image has drawtext + fontconfig/freetype; DejaVu fonts installed (the lead-in slate already draws text).
- Cost of a burned-in clock = re-encoding every frame (preview, one real-time stream):
  as-is 0.01 core · 720p+clock veryfast ~2 cores · 720p+clock ultrafast ~1 core · 1080p+clock ~4 cores.
  All 29 streams with a clock = 30-60 of 40 cores -> not possible; opt-in per stream, counted in the transcode limit (8).
- Health check "Clock synchronised" is SKIP today (the container can't run timedatectl).

## Plan
1. NTP everywhere (ops, no code): rig VM already synced; also check the Proxmox host; laptop + edges:
   `sudo timedatectl set-ntp true` (Windows: w32time). Best: all point at ONE NTP server on the LAN (router/Proxmox host),
   so their offsets to each other stay in single-digit ms.
2. Rig proves its clock (backend): health check measures the offset to the NTP server itself (SNTP over UDP 123):
   PASS < 50 ms, WARN < 500 ms, FAIL above; shown on System > Health, warning in the top bar when off.
3. Rig clock on screen (frontend, hot): live server time with milliseconds in the top bar / a full-screen "Rig clock"
   page, for the photo method (camera picture + laptop screen in one photo).
4. Burn the clock into chosen streams (backend + UI, R2/R3):
   - per stream "Server clock in picture" (off by default): create/edit dialog, ⋯ menu, bulk for selected streams;
   - FFmpeg drawtext in the publisher: `YYYY-MM-DD HH:MM:SS.mmm` (+ optional frame number), corner chosen so it does not
     cover the footage's own recorded OSD, semi-transparent box; same size as the file, no B-frames, keyframe 2 s;
   - container TZ set (Asia/Kolkata or UTC, decided below); the lead-in slate shows the same clock;
   - counts as a transcoded stream (limit 8); UI shows the CPU cost before enabling.
5. A dedicated "CLOCK" test camera (cheap, recommended): a generated 720p stream (test pattern + big ms clock + frame
   counter), about 1 core. Devs point an edge at it to measure the whole pipeline's latency without touching real streams.
6. Zero-CPU option for edges that read RTP time (DeepStream / GStreamer): every RTSP stream already carries RTCP sender
   reports with NTP wall-clock time; verify MediaMTX passes the rig's clock (path option `useAbsoluteTimestamp`) so an
   edge can stamp each frame with the rig's time without any overlay. Needs a test with their edge.
7. Real IP camera option: outside the app — set the camera's NTP server to the same LAN server and enable its OSD clock.

## Decisions needed
1. Which streams need the clock: a few test streams + the CLOCK camera (recommended), or more (CPU)?
2. Timezone shown (IST or UTC) and whether milliseconds are needed.
3. Which NTP server everyone uses (public pool or one on the LAN).
4. Their edge software: can it use RTCP NTP timestamps (item 6)?
5. Restart window: items 2, 4, 5 are backend (go with R2/R3); items 1, 3 need no restart.
