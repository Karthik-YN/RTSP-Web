# Lead-in spike results (Linux 7.0.14-linuxkit aarch64)

ffmpeg version 5.1.9-0+deb12u1 Copyright (c) 2000-2026 the FFmpeg developers · MediaMTX v1.21.2 · GStreamer RTSP: True · drawtext: True · 2026-11-09 07:47

| Test | Case | Verdict | Details |
|---|---|---|---|
| T1 reader stays connected | h264_1080p25_gop2 | **PASS** | `{"reader_sessions_created": 2, "readers": 2}` |
| T2 clean switch | h264_1080p25_gop2 | **PASS** | `{"pause_at_switch_ms": [80], "slate_style": "drawtext", "segments": ["Sx104", "Vx421(from src frame 100)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [2.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop2 | **PASS** | `{"start_keyframe_s": 4.0, "first_video_src_frame": 100}` |
| T1/T2 GStreamer reader | h264_1080p25_gop2 | **PASS** | `{"frames": 528, "errors": [], "caps_changes": 1}` |
| T1 reader stays connected | h264_720p2997_bframes | **PASS** | `{"reader_sessions_created": 2, "readers": 2}` |
| T2 clean switch | h264_720p2997_bframes | **PASS** | `{"pause_at_switch_ms": [100], "slate_style": "drawtext", "segments": ["Sx125", "Vx504(from src frame 120)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [3.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_720p2997_bframes | **PASS** | `{"start_keyframe_s": 4.004, "first_video_src_frame": 120}` |
| T1/T2 GStreamer reader | h264_720p2997_bframes | **PASS** | `{"frames": 633, "errors": [], "caps_changes": 1}` |
| T1 reader stays connected | h264_1080p25_gop10 | **PASS** | `{"reader_sessions_created": 2, "readers": 2}` |
| T2 clean switch | h264_1080p25_gop10 | **PASS** | `{"pause_at_switch_ms": [], "slate_style": "drawtext", "segments": ["Sx105", "Vx421(from src frame 250)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop10 | **PASS** | `{"start_keyframe_s": 10.0, "first_video_src_frame": 250}` |
| T1/T2 GStreamer reader | h264_1080p25_gop10 | **PASS** | `{"frames": 528, "errors": [], "caps_changes": 1}` |
| T1 reader stays connected | hevc_1080p25 | **PASS** | `{"reader_sessions_created": 2, "readers": 2}` |
| T2 clean switch | hevc_1080p25 | **PASS** | `{"pause_at_switch_ms": [80], "slate_style": "drawtext", "segments": ["Sx112", "Vx413(from src frame None)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [2.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | hevc_1080p25 (transcoded) | **NOT RUN** | `{"reason": "re-encoded frames can't be matched to the source by checksum"}` |
| T1/T2 GStreamer reader | hevc_1080p25 | **PASS** | `{"frames": 519, "errors": [], "caps_changes": 1}` |
| T1 reader stays connected | h264_1080p25_gop2 + slate 1280x720 | **PASS** | `{"reader_sessions_created": 2, "readers": 2}` |
| T2 clean switch | h264_1080p25_gop2 + slate 1280x720 | **INFO** | `{"pause_at_switch_ms": [80], "slate_style": "drawtext", "segments": ["Sx104", "?x421"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [2.0], "unknown": 421, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop2 + slate 1280x720 | **INFO** | `{"start_keyframe_s": 4.0, "first_video_src_frame": null}` |
| T1/T2 GStreamer reader | h264_1080p25_gop2 + slate 1280x720 | **PASS** | `{"frames": 528, "errors": [], "caps_changes": 2}` |
| T4 hold until viewer | h264_1080p25_gop2 | **PASS** | `{"reader_detected_after_s": 0.26, "video_online_after_detect_s": 5.17, "lead_in_s": 5.0, "publisher_before_viewer": false, "slate_frames_decoded_by_reader": 111, "segments": ["Sx111", "Vx265(from src frame 100)"]}` |
| T5 lead-in frames unique per camera | slate style drawtext | **PASS** | `{"frames_a": 149, "frames_b": 126, "identical_frames_across_cameras": 0, "consecutive_identical_frames_in_one_slate": 0}` |
| T6 publisher crash keeps the reader | h264_1080p25_gop2 | **PASS** | `{"segments": ["Sx29", "Vx201(from src frame 100)", "Sx129", "Vx263(from src frame 100)"], "pauses_ms": [80, 120, 80], "reader_sessions_created": 1, "ts_not_increasing": 0, "decode_errors": []}` |
| T7 MediaMTX restart | re-add path config on MediaMTX up | **PASS** | `{"config_lost_on_restart": true, "frames_after_readd": 76}` |
| T8 cost of waiting streams | 20 streams, 1080p25 slate | **PASS** | `{"mediamtx_cpu_pct_and_rss_mb": {"no_paths": [1.4, 46.8], "20_paths_no_readers": [9.3, 56.8], "20_paths_20_readers": [13.1, 59.3]}, "cpu_count": 8}` |
