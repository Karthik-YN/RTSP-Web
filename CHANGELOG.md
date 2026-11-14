# Changelog

All work is on the branch `feature/live-preview-and-launcher`. The branch starts from `304c87d` on `master`.

## 2026-11-13 – 14: Phase 11 (standard 720p output, library and analytics, responsive)
- **Standard 16:9 1280×720 output.**
  - New streams are scaled to it by default; "Original" keeps the file's size, and admins can pick other sizes.
  - One click makes a 720p copy, which then streams without live transcoding.
  - Videos that are not 16:9 can't be streamed until a 16:9 copy is made (blurred bars, black bars or crop). They stay in the library.
- **Long videos** (over 1 h) play a cached 2-minute H.264 preview in the browser.
- **Uploader record.** Every upload stores the user, time, address and device, shown on the card and in a details dialog.
- **Login with Docker.** Turn it on with `rig02-users` inside the container (docs/DEPLOYMENT.md).
- **Analytics from the library:**
  - add one or many videos to analytics;
  - review name-based suggestions;
  - filter the library by analytics;
  - see analytics chips on each video;
  - see video counts in the sidebar.
- **Library:**
  - equal-size cards;
  - a toolbar with search, sort and filters (shape, uploader, analytics, needs attention);
  - grid or list view;
  - 48 cards at a time;
  - multi-select.
- **Responsive and faster.**
  - Every page works at 375–1440 px; dialogs open full-screen on phones.
  - Pages load on demand, and polling pauses in hidden tabs.
- **Database:** migrations 6 and 7 (shape and uploader columns) run by themselves on start.

## 2026-11-12 – 13: Operations and the shared library
- Docker image and compose file.
- Optional Cloudflare Tunnel and Access.
- Health page with a downloadable report.
- Capacity limits.
- Subnet-aware RTSP addresses.
- Access from any network over Tailscale.
- Play and download library videos in the browser.
- Warnings about long keyframe intervals.
- Convert any video to a universal H.264 copy.

## 2026-11-09 – 11: Analytics and lead-in
- Analytics taskbar (categories → analytics → video placeholders).
- Upload queue.
- 2 s start spacing between streams.
- Per-stream lead-in slate: fixed, or hold until a viewer connects.

## 2026-11-04 – 05: First version
- Video → RTSP stream manager (FastAPI, React, FFmpeg, MediaMTX, SQLite).
- Live preview in the dashboard.
- One-command launcher (`python3 app.py`).
- Demo walkthrough.
