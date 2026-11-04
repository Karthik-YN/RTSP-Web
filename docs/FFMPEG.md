# FFmpeg and MediaMTX implementation notes

Every decision below was made from a measurement on FFmpeg 9.0.2 + MediaMTX v1.21.2 (macOS arm64, development machine). The scripts and results are reproduced in the test suite (`tests/integration`). **Re-run the integration tests on the Proxmox VM**, because Debian/Ubuntu ship older FFmpeg (5.1 / 6.1).

## 1. The publisher command

For a stream copy loop of `store.mp4` from 00:01:58 to 00:05:00:

```
ffmpeg -hide_banner -nostdin -loglevel level+info -nostats -progress pipe:1 -stats_period 1 \
       -re -f concat -safe 0 -i /var/lib/rtsp-manager/run/streams/7/playlist.ffconcat \
       -map 0:v:0 -an -sn -dn -c:v copy \
       -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/cam7
```

`playlist.ffconcat` (the entry is repeated K times for loop mode, once for play-once):

```
ffconcat version 1.0
file '/data/rtsp-manager/videos/store.mp4'
inpoint 118.000000      # PTS of the keyframe at/before the requested start
outpoint 299.920000     # DTS of the keyframe at/before the requested end (the cut)
duration 182.000000     # PTS(end keyframe) - inpoint = exact loop period
...
```

| Option | Why |
|---|---|
| `-re` | Read input at native speed (1x), paced on input timestamps, which the playlist keeps continuous across loops. Validated: wall/media ratio 1.007–1.014 for 23.976/29.97/59.94/VFR. |
| `-f concat` + playlist | Start/end/loop without seeking inside the demuxer (§3). |
| `-map 0:v:0 -an -sn -dn` | First video track only; audio, subtitles and data are dropped (ticket requirement). Test: published RTSP has exactly one video track. |
| `-c:v copy` | Default for compatible H.264: near-zero CPU, no quality loss. |
| `-rtsp_transport tcp` | Reliable publish to the local MediaMTX. |
| `-progress pipe:1 -stats_period 1` | Machine-readable `frame`, `out_time_us`, `speed`, `drop_frames` every second. Used for the loop counter, playback position and the UI. |
| `-loglevel level+info` | Level-tagged log lines; `[error]` lines become the stream's `last_error`. |
| `-nostdin` | Never blocks on or reacts to a terminal. |

Transcode mode replaces `-c:v copy` with:

```
-c:v libx264 -preset veryfast -tune zerolatency -profile:v high -pix_fmt yuv420p -crf 23
-maxrate 6000k -bufsize 12000k -g <2 s of frames> -keyint_min <same> -sc_threshold 0
-force_key_frames expr:gte(t,n_forced*2) [-vf scale=w=1920:h=1080:force_original_aspect_ratio=decrease:force_divisible_by=2]
-fps_mode passthrough
```

* `-pix_fmt yuv420p` converts 10-bit, 4:2:2 and 4:4:4 input to 8-bit 4:2:0.
* `-tune zerolatency` removes B-frames and lookahead (low latency, camera-like).
* A fixed 2-second GOP with time-based forced keyframes keeps keyframes regular even with VFR input, so a reader that joins mid-stream waits at most 2 s.
* `passthrough` keeps the source timestamps (no speed change, no duplicated or dropped frames). `RTSPM_OUTPUT_FPS_MODE=cfr` adds `-fps_mode cfr -r <fps>` if the edge requires constant frame rate.

All arguments are built by `ffmpeg_manager.build_command()` (a pure function, unit-tested) as an argv list. No shell is involved, and route handlers never build commands.

## 2. What was wrong with the ticket's suggested command

Measured with a 10 s 25 fps clip (keyframe every 1 s, B-frames). Every output frame was mapped back to its source frame by MD5 (`ffmpeg -f framemd5`):

| Command | Result |
|---|---|
| `-stream_loop -1 -ss 4 -t 3 -i src` | Plays 4→7 s **once and stops**: input `-t` cancels looping, so there is no way to loop a segment with an end time. |
| `-stream_loop -1 -ss 4 -i src` | FFmpeg 9 loops back to 4 s. FFmpeg ≤ 6 (Debian/Ubuntu) restarts at 0 s, so behaviour depends on the version. |
| `-stream_loop -1 -f concat` (inpoint/outpoint) on a 29.97 fps B-frame source | Loop seek **fails** (`Operation not permitted`) for any non-zero inpoint; the stream silently stops looping. |
| **Repeated concat entries** (chosen) | Loops exactly, on every FFmpeg version, with no demuxer seek. Memory: 100,000 entries = 16.7 MB playlist, 44 MB RSS, 0.13 s to start. The backend caps it at 50,000 entries and sizes it to cover 7 days. |

## 3. Clean loop points (EC-04)

The concat demuxer offsets each entry's timestamps by its duration, so timestamps stay monotonic. Two details matter.

1. **Start on a keyframe.** With an unsnapped inpoint, every iteration re-sends the preroll packets from the previous keyframe: 459 "non-monotonic DTS" corrections over 10 loops, plus stray frames. With the inpoint snapped to the keyframe at or before the requested start, there were 2 corrections per loop (B-frame reordering, fixed by FFmpeg itself).
2. **End by DTS, offset by PTS.** The demuxer cuts at `outpoint` by **DTS**. With B-frames the end keyframe's DTS is earlier than its PTS, so with `outpoint = keyframe PTS` that keyframe leaked into the loop and its PTS collided with the next iteration's first frame: 6 duplicate timestamps in 14 s over RTSP. Fix: `outpoint = keyframe DTS` (excludes it) and `duration = keyframe PTS − inpoint` (exact offset). Measured result: **0 duplicate PTS, every iteration carries exactly the same source frames (75–174), and the maximum step between frames is one frame interval.**

Without an end time the period is the end of the **video track** (last packet PTS + duration). Using the container duration would insert a frozen gap at every loop when an audio track outlasts the video.

Integration tests check over RTSP that, across loop boundaries, timestamps are strictly increasing with no duplicates, the largest gap is under one frame interval (<0.2 s), the pace is 1x, and the publisher never restarts. The 30-minute soak test (`pytest -m long`) does the same continuously; see `docs/reports/soak-*.json`.

## 4. Loop counter (exact, not an uptime estimate)

FFmpeg logs nothing at a loop boundary. Because each entry advances the output timestamps by exactly `duration` (measured: 40 iterations of a 5.763333 s segment gave `out_time` = 230.542433 s), the backend computes

```
loop_count = floor(out_time / period)     (out_time from -progress, period = playlist duration)
```

The frame count per iteration is also identical (175/175 copy and libx264), which confirms that every iteration is the same media. The count is persisted every few seconds. After an automatic restart (crash, MediaMTX outage, reboot) it continues from the persisted value. A user-initiated start, restart or edit resets it to 0.

## 5. Start-position accuracy and seeking (EC-13)

| Container | Seek mechanism | Measured (2 h file, seek to 01:45:00) |
|---|---|---|
| MP4/MOV (moov index) | index lookup | probe 0.07 s, keyframe lookup 0.04 s, first packet 0.04 s; RUNNING in MediaMTX 0.38–0.62 s after the API call; FFmpeg RSS 15–28 MB |
| MKV (Cues) | index lookup | lookup 0.03 s, first packet 0.03 s |
| MPEG-TS (no index) | timestamp bisection; non-zero `start_time` (1.48 s) handled | lookup 0.03 s, first packet 0.03 s |
| MKV without Cues, raw streams | may read sequentially | Not supported for long files; remux with `ffmpeg -i in -c copy out.mp4` |

The keyframe lookup (`ffprobe -read_intervals <t-W>%<t>`) also seeks; it never reads from the start of the file.

**Guaranteed accuracy:**
* The effective start is the keyframe at or before the requested time, so it is at most one GOP earlier (typically 1–2 s for camera footage). The effective end is the keyframe at or before the requested end. Both are shown in the create dialog and the stream details before and after publishing.
* This applies to transcoded streams too (deliberately, so loop points stay clean).
* Long-GOP files (keyframe interval > 4 s) get a warning in the library.
* Offsets are relative to the file start. The container `start_time` (e.g. MPEG-TS 1.48 s) is added internally.

## 6. MediaMTX path behaviour and early readers (EC-12)

| Measurement | Result |
|---|---|
| Reader asks for a path with no publisher (default config) | `404 Not Found` immediately |
| Same, with `runOnDemand: sleep 3600` placeholder | The DESCRIBE is **held**; the reader received the stream about 1 s after the publisher appeared (test: reader waited 8.1 s in total, then received H.264) |
| The reader leaves | MediaMTX stops only the placeholder ("not needed by anyone"); the real publisher keeps running and the path stays ready |

Config used (`deployment/mediamtx.yml`): `runOnDemand: sleep 3600`, `runOnDemandStartTimeout: 60s`, `runOnDemandCloseAfter: 10s`, `overridePublisher: no`. If the publisher does not start within 60 s, the reader receives 404 and must retry on its own. **Confirm the 5.x edge's retry behaviour** (EDGE_VALIDATION E-12).

Other MediaMTX notes:
* "RTP packets are too big (1460 > 1440), remuxing them into smaller ones" is informational: MediaMTX re-packetizes FFmpeg's RTP for its UDP size limit.
* v1.21 enables a new **MoQ** server (`:8892`/`:8893`) by default. It is disabled explicitly; a test fails if any port other than RTSP is reachable from the network.

## 7. Codec compatibility policy (EC-01)

Copy is allowed only when **all** of these hold: codec ∈ `RTSPM_COPY_CODECS` (h264), pixel format ∈ `RTSPM_COPY_PIX_FMTS` (yuv420p, yuvj420p), 8-bit, profile ∈ `RTSPM_COPY_PROFILES` (Constrained Baseline, Baseline, Main, High), ≤ 3840×2160, ≤ 60 fps. Otherwise:

* `RTSPM_TRANSCODE_POLICY=auto` (default): transcode to the H.264 output profile, with a CPU warning in the UI. Capped by `RTSPM_MAX_TRANSCODE_STREAMS`.
* `never`: refuse with the reasons (e.g. "Codec HEVC is not in the edge copy list (h264). Transcoding is disabled").
* Refused anyway if this FFmpeg build cannot decode the codec, or has no libx264.

Measured on the dev machine: HEVC, HEVC 10-bit, VP9, AV1 and H.264 10-bit were all published as H.264 High yuv420p at 1x.

## 8. Frame rates (EC-03)

Copy mode preserves the source timestamps exactly, so odd and variable frame rates play at real time without conversion. Measured over 12 s via RTSP:

| Source | Wall/media | Delivered fps | Duplicate PTS |
|---|---|---|---|
| 23.976 | 1.012 | 23.98 | 0 |
| 29.97 | 1.009 | 29.88 | 0 |
| 59.94 | 1.007 | 59.83 | 0 |
| VFR (20/60 ms alternating) | 1.011 | 25.0 avg | 0 |

(The wall time includes the reader's RTSP connection setup.) The burnt-in-clock check against a stopwatch must still be done by a person (EDGE_VALIDATION E-03).

## 9. Logs

Each stream has `<data_dir>/logs/streams/<id>.log`, rotated at 1 MB with 2 backups, plus an in-memory tail (the last 20 lines in the UI). The 2-per-loop "Non-monotonic DTS" warnings from B-frame copy loops are expected; after the first three they are collapsed into a counter so they don't drown real errors.
