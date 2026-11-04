"""FFmpeg command construction, playlists, progress parsing and loop counting."""

from pathlib import Path

import pytest

from app.config import Settings
from app.services.ffmpeg_manager import (LogSink, PlanError, Progress, PublishPlan, build_command,
                                         ffconcat_quote, loop_entries, render_playlist)

S = Settings(_env_file=None)


def plan(**kw):
    base = dict(stream_id=7, name="cam1", source_path=Path("/data/videos/store a/clip.mp4"),
                inpoint_abs=120.0, outpoint_abs=299.92, duration_s=180.0, mode="loop", output_mode="copy",
                publish_url="rtsp://127.0.0.1:8554/cam1", playlist_path=Path("/var/lib/r/run/streams/7/p.ffconcat"),
                fps=25.0, width=1920, height=1080)
    base.update(kw)
    p = PublishPlan(**base)
    p.entries = loop_entries(p.period_s, S) if p.mode == "loop" else 1
    return p


def opt(args, flag):
    return args[args.index(flag) + 1]


def test_copy_command_shape():
    args = build_command(plan(), S)
    assert args[0] == "ffmpeg"
    assert "-re" in args and args.index("-re") < args.index("-i")  # real-time pacing on input
    assert opt(args, "-f") == "concat"
    assert opt(args, "-i") == "/var/lib/r/run/streams/7/p.ffconcat"
    assert opt(args, "-map") == "0:v:0"
    for flag in ("-an", "-sn", "-dn"):  # audio/subtitles/data dropped
        assert flag in args
    assert opt(args, "-c:v") == "copy"
    assert opt(args, "-rtsp_transport") == "tcp"
    assert args[-1] == "rtsp://127.0.0.1:8554/cam1"
    assert args[-3:-1] == ["-rtsp_transport", "tcp"]
    assert "-progress" in args and opt(args, "-progress") == "pipe:1"
    assert "-stream_loop" not in args  # looping is done by the playlist
    assert "-nostdin" in args


def test_no_shell_metacharacters_are_interpreted():
    # names are validated elsewhere; here we check argv stays a list of plain strings
    args = build_command(plan(publish_url="rtsp://127.0.0.1:8554/a;b"), S)
    assert args[-1] == "rtsp://127.0.0.1:8554/a;b"
    assert all(isinstance(a, str) for a in args)


def test_transcode_command():
    args = build_command(plan(output_mode="transcode", width=3840, height=2160, fps=30000 / 1001), S)
    assert opt(args, "-c:v") == "libx264"
    assert opt(args, "-pix_fmt") == "yuv420p"
    assert opt(args, "-profile:v") == S.x264_profile
    assert opt(args, "-preset") == S.x264_preset
    assert opt(args, "-g") == str(round(30000 / 1001 * S.x264_gop_seconds))
    assert opt(args, "-sc_threshold") == "0"
    assert opt(args, "-force_key_frames").startswith("expr:gte(t,n_forced*")
    assert "force_original_aspect_ratio=decrease" in opt(args, "-vf")
    assert opt(args, "-fps_mode") == "passthrough"
    assert "-an" in args


def test_transcode_no_scale_when_small():
    args = build_command(plan(output_mode="transcode", width=1280, height=720), S)
    assert "-vf" not in args


def test_transcode_cfr_mode():
    s = Settings(_env_file=None, output_fps_mode="cfr")
    args = build_command(plan(output_mode="transcode", fps=29.97), s)
    assert opt(args, "-fps_mode") == "cfr"
    assert opt(args, "-r") == "29.97"


def test_playlist_once_has_single_entry():
    text = render_playlist(plan(mode="once"))
    assert text.startswith("ffconcat version 1.0\n")
    assert text.count("file ") == 1
    # outpoint = keyframe DTS (cut), duration = keyframe PTS - inpoint (timestamp offset);
    # 0.5 ms margins absorb ffprobe's 1 us rounding (NTSC timebases)
    assert "inpoint 120.000500\noutpoint 299.919500\nduration 180.000000\n" in text


def test_playlist_margins_exclude_ntsc_keyframe():
    # real case: keyframe DTS 418418/30000 = 13.94726666.. printed by ffprobe as 13.947267
    true_dts = 418418 / 30000
    text = render_playlist(plan(inpoint_abs=2.002, outpoint_abs=13.947267, duration_s=12.012))
    outpoint = float(text.split("outpoint ")[1].split()[0])
    assert outpoint < true_dts  # the end keyframe is cut off (dts >= outpoint)
    assert true_dts - outpoint < 1 / 120  # but no earlier frame is lost
    assert "inpoint 0.000000" in render_playlist(plan(inpoint_abs=0.0))


def test_playlist_to_end_of_file_has_no_outpoint():
    text = render_playlist(plan(outpoint_abs=None, duration_s=480.0))
    assert "outpoint" not in text and "duration 480.000000" in text


def test_playlist_loop_repeats_entry():
    p = plan()
    text = render_playlist(p)
    assert text.count("file ") == p.entries
    # 7 days of a 180 s segment
    assert p.entries == 3360


def test_loop_entries_bounds():
    assert loop_entries(1.0, S) == S.loop_playlist_max_entries  # capped
    assert loop_entries(10 * 86400, S) == 2  # at least two
    with pytest.raises(PlanError):
        loop_entries(0, S)


def test_ffconcat_quoting():
    assert ffconcat_quote("/v/it's here.mp4") == "'/v/it'\\''s here.mp4'"
    with pytest.raises(PlanError):
        ffconcat_quote("/v/evil\nfile '/etc/passwd'")


def test_progress_parsing_and_loop_count():
    p = Progress()
    block = ["frame=250", "fps=25.0", "out_time_us=10000000", "drop_frames=0", "dup_frames=0",
             "speed=1.00x", "total_size=12345", "progress=continue"]
    done = [p.feed(line) for line in block]
    assert done[-1] is True and not any(done[:-1])
    assert p.frame == 250 and p.out_time_s == 10.0 and p.speed == 1.0
    assert p.loops_completed(3.0) == 3
    assert p.loops_completed(10.0) == 1  # exactly at the boundary
    assert p.loops_completed(0) == 0
    p.feed("out_time_us=N/A")
    p.feed("progress=continue")
    assert p.out_time_s == 10.0  # N/A ignored


def test_progress_exact_period_from_measurement():
    # Measured with FFmpeg 9: 40 loops of inpoint 3.336667/outpoint 9.1 -> out_time 230.542433 s
    p = Progress()
    for line in ("out_time_us=230542433", "progress=continue"):
        p.feed(line)
    assert p.loops_completed(9.1 - 3.336667) == 40


def test_log_sink_rotation_and_noise_suppression(tmp_path):
    sink = LogSink(tmp_path / "s.log", max_bytes=2000, backups=2, tail=50)
    for i in range(200):
        sink.write(f"[warning] line {i} " + "x" * 40)
    assert (tmp_path / "s.log").exists() and (tmp_path / "s.log.1").exists()
    assert not (tmp_path / "s.log.3").exists()
    assert len(sink.tail(20)) == 20 and "line 199" in sink.tail(1)[0]
    for _ in range(10):
        sink.write("[warning] Non-monotonic DTS; previous: 10, current: 9; changing to 11.")
    assert sink.suppressed == 10
    assert sum("Non-monotonic" in ln for ln in sink.tail(50)) == 3  # first three only
    sink.write("[error] Connection refused")
    assert sink.last_error() == "Connection refused"
    # tail survives a restart of the backend
    assert "Connection refused" in LogSink(tmp_path / "s.log", 2000, 2).tail(1)[0]
