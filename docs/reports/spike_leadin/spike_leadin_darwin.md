# Lead-in spike results (Darwin 25.6.0 arm64)

ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers · MediaMTX v1.21.2 · GStreamer RTSP: False · drawtext: False · 2026-11-09 13:10

| Test | Case | Verdict | Details |
|---|---|---|---|
| T1 reader stays connected | h264_1080p25_gop2 | **PASS** | `{"reader_sessions_created": 1, "readers": 1}` |
| T2 clean switch | h264_1080p25_gop2 | **PASS** | `{"pause_at_switch_ms": [160], "slate_style": "band+counter", "segments": ["Sx102", "Vx422(from src frame 100)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [4.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop2 | **PASS** | `{"start_keyframe_s": 4.0, "first_video_src_frame": 100}` |
| T1/T2 GStreamer reader | h264_1080p25_gop2 | **NOT RUN** | `{"reason": "GStreamer RTSP plugins not installed here"}` |
| T1 reader stays connected | h264_720p2997_bframes | **PASS** | `{"reader_sessions_created": 1, "readers": 1}` |
| T2 clean switch | h264_720p2997_bframes | **PASS** | `{"pause_at_switch_ms": [133], "slate_style": "band+counter", "segments": ["Sx122", "Vx507(from src frame 120)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [4.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_720p2997_bframes | **PASS** | `{"start_keyframe_s": 4.004, "first_video_src_frame": 120}` |
| T1/T2 GStreamer reader | h264_720p2997_bframes | **NOT RUN** | `{"reason": "GStreamer RTSP plugins not installed here"}` |
| T1 reader stays connected | h264_1080p25_gop10 | **PASS** | `{"reader_sessions_created": 1, "readers": 1}` |
| T2 clean switch | h264_1080p25_gop10 | **PASS** | `{"pause_at_switch_ms": [160], "slate_style": "band+counter", "segments": ["Sx102", "Vx422(from src frame 250)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [4.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop10 | **PASS** | `{"start_keyframe_s": 10.0, "first_video_src_frame": 250}` |
| T1/T2 GStreamer reader | h264_1080p25_gop10 | **NOT RUN** | `{"reason": "GStreamer RTSP plugins not installed here"}` |
| T1 reader stays connected | hevc_1080p25 | **PASS** | `{"reader_sessions_created": 1, "readers": 1}` |
| T2 clean switch | hevc_1080p25 | **PASS** | `{"pause_at_switch_ms": [160], "slate_style": "band+counter", "segments": ["Sx118", "Vx406(from src frame None)"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [4.0], "unknown": 0, "decode_errors": []}` |
| T3 starts at the configured frame | hevc_1080p25 (transcoded) | **NOT RUN** | `{"reason": "re-encoded frames can't be matched to the source by checksum"}` |
| T1/T2 GStreamer reader | hevc_1080p25 | **NOT RUN** | `{"reason": "GStreamer RTSP plugins not installed here"}` |
| T1 reader stays connected | h264_1080p25_gop2 + slate 1280x720 | **PASS** | `{"reader_sessions_created": 1, "readers": 1}` |
| T2 clean switch | h264_1080p25_gop2 + slate 1280x720 | **INFO** | `{"pause_at_switch_ms": [160], "slate_style": "band+counter", "segments": ["Sx102", "?x422"], "skipped": 0, "repeated": 0, "ts_not_increasing": 0, "gaps": [4.0], "unknown": 422, "decode_errors": []}` |
| T3 starts at the configured frame | h264_1080p25_gop2 + slate 1280x720 | **INFO** | `{"start_keyframe_s": 4.0, "first_video_src_frame": null}` |
| T1/T2 GStreamer reader | h264_1080p25_gop2 + slate 1280x720 | **NOT RUN** | `{"reason": "GStreamer RTSP plugins not installed here"}` |
| T4 hold until viewer | h264_1080p25_gop2 | **PASS** | `{"reader_detected_after_s": 0.26, "video_online_after_detect_s": 5.09, "lead_in_s": 5.0, "publisher_before_viewer": false, "slate_frames_decoded_by_reader": 109, "segments": ["Sx109", "Vx267(from src frame 100)"]}` |
| T5 lead-in frames unique per camera | slate style band+counter | **PASS** | `{"frames_a": 140, "frames_b": 127, "identical_frames_across_cameras": 0, "consecutive_identical_frames_in_one_slate": 0}` |
| T6 publisher crash keeps the reader | h264_1080p25_gop2 | **PASS** | `{"segments": ["Sx27", "Vx213(from src frame 100)", "Sx127", "Vx251(from src frame 100)"], "pauses_ms": [160, 80, 120, 160], "reader_sessions_created": 1, "ts_not_increasing": 0, "decode_errors": []}` |
| T7 MediaMTX restart | re-add path config on MediaMTX up | **PASS** | `{"config_lost_on_restart": true, "frames_after_readd": 77}` |
| T8 cost of waiting streams | 20 streams, 1080p25 slate | **PASS** | `{"mediamtx_cpu_pct_and_rss_mb": {"no_paths": [0.8, 25.1], "20_paths_no_readers": [6.9, 45.3], "20_paths_20_readers": [9.9, 47.4]}, "cpu_count": 8}` |
