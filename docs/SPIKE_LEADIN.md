# Phase 1 spike: lead-in slate and hold-until-viewer

**Question.** Can a stream show a per-camera "waiting" picture (the *slate*) before its video, either for a fixed N seconds or until the edge connects plus N seconds, then switch to the video **without the edge reconnecting** and **without losing, repeating or reordering a video frame**?

**Answer: yes**, using MediaMTX's own *always-available* mode. Every test that could run passed on macOS and on Debian 12. The real 5.x edge is still to be checked (see "Edge check" below).

* Script: `scripts/spike_leadin.py` (also `scripts/test_linux.sh spike` for Debian 12 with GStreamer).
* Raw results: `docs/reports/spike_leadin/spike_leadin_darwin.{md,json}` and `spike_leadin_linux.{md,json}`.
* Date: 2026-11-09. MediaMTX v1.21.2 on both.

## How it works

MediaMTX (v1.21.2, the version we pin) can keep a path *available* while nobody publishes to it: it plays an MP4 file of our choosing (`alwaysAvailableFile`) on repeat. Readers can connect at any time. When a publisher connects, MediaMTX switches the same reader sessions to the live video, and back to the file if the publisher stops or crashes.

The backend would:
1. make a slate MP4 per stream with FFmpeg: black, with the stream name and a running clock, at the **same resolution and frame rate as the source video**;
2. add the stream's path to MediaMTX through its API with `alwaysAvailable: true` and that file (and `runOnDemand: ""`, because MediaMTX refuses to combine the two);
3. start the existing FFmpeg publisher (stream copy, unchanged) when the lead-in is over.

## Results

Sources: 1080p25 H.264 with 2 s keyframes; 720p 29.97 fps with B-frames; 1080p25 with 10 s keyframes (like `000003.mp4`); an H.265 source the product transcodes to H.264 with its own settings. Configured start: the first keyframe at or after 4 s (10 s for the 10 s-keyframe source).

| # | Test | macOS (FFmpeg 9.0.2) | Debian 12 (FFmpeg 5.1, GStreamer) |
|---|---|---|---|
| T1 | Reader stays connected across slate → video (same MediaMTX session) | PASS, 5/5 sources | PASS, 5/5 (FFmpeg and GStreamer readers) |
| T2 | No video frame lost, repeated or out of order; no decode errors | PASS, 4/4 sources | PASS, 4/4 |
| T3 | First video frame is exactly the configured start frame | PASS, 3/3 copy sources | PASS, 3/3 |
| T3 | Same, transcoded source | NOT RUN: re-encoded frames can't be matched by checksum | NOT RUN, same reason |
| — | GStreamer reader (`rtspsrc ! rtph264depay ! avdec_h264`) | NOT RUN: no GStreamer RTSP plugins on this Mac | PASS, 5/5: every frame decoded, no errors |
| T4 | Hold: no publisher until a reader connects; video online 5 s after the reader was seen | PASS: reader seen in 0.26 s, video online after 5.09 s | PASS: 0.26 s, 5.17 s |
| T5 | Slate frames differ between cameras (the edge won't drop them as duplicates) | PASS: 0 identical frames across 2 cameras, 0 repeated frames in a slate | PASS, same |
| T6 | Publisher crash (SIGKILL) → slate → publisher back, same reader session | PASS: slate, video, slate, video in one session; video restarts at the start frame | PASS |
| T7 | MediaMTX restart | PASS: API-added path settings are **lost** on restart and must be re-added; after re-adding, readers get the slate | PASS, same |
| T8 | Cost: 20 streams waiting on a 1080p slate | MediaMTX +6 % of one core idle, +9 % with 20 readers; +20 MB RAM | +8 % idle, +12 % with 20 readers; +10 MB |
| INFO | Slate at a **different** resolution from the video (1280×720 slate, 1080p video) | FFmpeg reader rescales to the first size, so frames can't be checked | GStreamer handled the size change (2 resolutions, no errors) |

### What the numbers mean

* **A short pause at the switch.** The video's first frame arrives 80–160 ms after the last slate frame (the time the publisher needs to deliver its first frame). No video frame is missing: the reader receives every frame from the configured start onward. The plan said "no gap larger than one frame"; the measured pause is 2–4 frames of slate time, not lost video. I judged T2 on lost/repeated/reordered video frames and a pause of at most 500 ms. **Your call** whether that pause is acceptable.
* **Slate resolution must match the video.** GStreamer coped with a size change, but the FFmpeg reader rescaled, and edges differ. The design always makes the slate at the video's size and frame rate, so this case never happens.
* **Slate text.** Debian's FFmpeg can draw text, so the slate shows the stream name and a clock. Homebrew's FFmpeg on the Mac can't (built without FreeType), so the slate falls back to a colour band derived from the name plus a moving counter. It's still unique per camera (T5 passed with both styles).
* **Mid-stream joins.** "co located POCs unavailable" lines come from readers joining between keyframes. They are normal decoder notices and occurred only for the cost-test readers, not in any switch test.
* **Not covered.** VLC isn't installed here (NOT RUN). The Debian run was Docker on Apple silicon (arm64), not the VM's amd64 CPU; FFmpeg and MediaMTX versions are the same as the VM will have.

## Proposed design for Phase 3 (and the Phase 2 hook)

* **Per stream:** `lead_in_mode` = `none` | `fixed` | `hold`, `lead_in_s` (default 5).
* **Slate file:** generated when the stream is created or its name/video changes; same width, height and frame rate as the source; 10 s, keyframe every second; cached under the data folder, **readable by the MediaMTX service user** (systemd install runs MediaMTX as a separate user).
* **MediaMTX path:** added through the API on start, re-added whenever MediaMTX comes back up (the existing "MediaMTX up" hook), removed on stop and delete, so a stopped stream's URL still fails as today.
* **Fixed:** slate from the moment the stream starts; publisher started after `lead_in_s`.
* **Hold:** status `WAITING_FOR_VIEWER`; the monitor (already polling MediaMTX every second) sees the first RTSP reader (dashboard preview excluded), waits `lead_in_s`, then starts the publisher. A **Start video now** button skips the wait.
* **Crash/restart:** the edge stays on the slate while the publisher restarts (T6), instead of being disconnected as today.
* **Phase 2 hook:** the 2 s start spacing gate applies to the moment each publisher starts, including hold-triggered starts.
* **Fallback** (not needed on this evidence): a feeder that switches the publisher's input, or transcoding lead-in streams.

## Edge check (for you, about 10 minutes, before Phase 3 is called done)

1. Run `backend/.venv/bin/python scripts/spike_leadin.py` on the VM, or start a hold-mode stream once Phase 3 exists.
2. Add the stream's URL as a camera in the portal while the slate shows.
3. Confirm: the edge shows the slate, then the video, **without** a reconnect in its logs, and the analytics sees the first seconds of the video.
4. Kill the publisher once (`pkill -f "rtsp://127.0.0.1:8554/<name>"`) and confirm the edge stays connected and the video comes back.
