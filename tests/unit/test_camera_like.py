"""Streams should look like real cameras: no B-frames, H.265 sent as-is only when switched on,
new streams at the file's own size, and a separate H.264 path for the browser preview."""

from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.services import encoders as E
from app.services.compatibility import COPY, TRANSCODE, b_frames, evaluate
from app.services.converter import Converter
from app.services.live_preview import LivePreviews, preview_path

S = Settings(_env_file=None)
DEC = frozenset({"h264", "hevc"})


def meta(**kw):
    m = dict(video_codec="h264", video_profile="High", pix_fmt="yuv420p", bit_depth=8, width=1920,
             height=1080, fps=25.0, is_vfr=False, audio_codec=None, field_order="progressive",
             keyframe_interval_s=2.0)
    m.update(kw)
    return m


def test_new_streams_keep_the_file_size():
    assert S.output_default == "original"


def test_converter_never_makes_b_frames():
    conv = Converter(SimpleNamespace(s=S))
    v = SimpleNamespace(fps=25.0)
    for profile in ("h264", "720p", "16x9"):
        cmd = conv.command(v, Path("in.mp4"), Path("out.mp4"), profile, "pad")
        assert cmd[cmd.index("-bf") + 1] == "0", profile


def test_live_cpu_encoder_never_makes_b_frames_whatever_the_tune():
    args = E.encoder_args(E.CPU, profile="high", gop=50, gop_seconds=2, maxrate_kbps=6000, crf=23,
                          preset="veryfast", tune="film", threads=0)
    assert args[args.index("-bf") + 1] == "0"


def test_b_frames_from_the_stored_probe():
    assert b_frames({"streams": [{"codec_type": "video", "has_b_frames": 2}]}) is True
    assert b_frames({"streams": [{"codec_type": "video", "has_b_frames": 0}]}) is False
    assert b_frames(None) is None and b_frames({"streams": [{"codec_type": "audio"}]}) is None
    r = evaluate(meta(has_b_frames=True), S, DEC, True)
    assert r.mode == COPY and any("B-frames" in w for w in r.warnings)


def test_hevc_is_transcoded_unless_pass_through_is_on():
    assert evaluate(meta(video_codec="hevc", video_profile="Main"), S, DEC, True).mode == TRANSCODE
    r = evaluate(meta(video_codec="hevc", video_profile="Main"), S, DEC, True, extra_copy_codecs=("hevc",))
    assert r.mode == COPY and r.reasons == []


def test_browser_preview_reads_its_own_small_h264_path():
    lp = LivePreviews(S)
    cmd = lp.command("cam-1")
    assert cmd[cmd.index("-i") + 1].endswith("/cam-1?rtspstudio=internal")  # the app's own read: not a viewer
    assert cmd[-1].endswith("/" + preview_path("cam-1")) and preview_path("cam-1") == "preview/cam-1"
    assert cmd[cmd.index("-c:v") + 1] == "libx264" and cmd[cmd.index("-bf") + 1] == "0"


def test_slate_uses_the_codec_the_video_is_sent_in():
    from app.services.slate_service import SlateSpec, slate_command
    h264 = SlateSpec("cam", 1280, 720, "25/1", "high")
    hevc = SlateSpec("cam", 1280, 720, "25/1", "main", codec="hevc")
    a = slate_command(h264, Path("s.mp4"), 5, False, S)
    b = slate_command(hevc, Path("s.mp4"), 5, False, S)
    assert a[a.index("-c:v") + 1] == "libx264" and b[b.index("-c:v") + 1] == "libx265"
    assert h264.key(5) != hevc.key(5)
    assert h264.key(5) == SlateSpec("cam", 1280, 720, "25/1", "high").key(5)  # cached H.264 slates stay valid


def test_app_reads_are_tagged_and_not_counted_as_viewers():
    from app.config import INTERNAL_READER
    assert S.internal_read_url("cam") == S.publish_url("cam") + "?" + INTERNAL_READER


def test_full_health_test_runs_once_a_day():
    import time as _t
    from datetime import datetime, timezone
    from app.services.health_test import HealthTester
    h = HealthTester.__new__(HealthTester)
    h.s = S
    h.last = None
    assert h.full_due()  # never ran
    h.last = {"started_at": datetime.now(timezone.utc).isoformat()}
    assert not h.full_due()
    assert h.full_due(now=_t.time() + 25 * 3600)


# ---------------------------------------------------------------- server clock drawn on streams
def test_clock_filter_draws_wall_clock_ms_and_keeps_timing(tmp_path):
    from app.services.ffmpeg_manager import PublishPlan, clock_text_file, transcode_args, write_clock_text
    p = PublishPlan(stream_id=1, name="cam", source_path=Path("/v/a.mp4"), inpoint_abs=0, outpoint_abs=None,
                    duration_s=60, mode="loop", output_mode="transcode", publish_url="rtsp://x/cam",
                    playlist_path=tmp_path / "streams" / "1" / "p.ffconcat", fps=25, width=1280, height=720, clock_tz="IST")
    vf = transcode_args(p, S)[transcode_args(p, S).index("-vf") + 1]
    assert vf.startswith("setpts=RTCTIME/(TB*1000000),") and vf.endswith(",setpts=PTS-STARTPTS")
    assert f"textfile={clock_text_file(p)}" in vf and "drawtext=" in vf
    write_clock_text(p)
    text = clock_text_file(p).read_text()
    assert text.startswith("%{pts:gmtime:19800:%Y-%m-%d %H\\:%M\\:%S}.%{eif:mod(floor(t*1000),1000):d:3}") and text.endswith(" IST")
    p.clock_tz = "UTC"
    write_clock_text(p)
    assert "%{pts:gmtime:0:" in clock_text_file(p).read_text() and clock_text_file(p).read_text().endswith(" UTC")
    p.clock_tz = None
    assert "-vf" not in transcode_args(p, S) or "drawtext" not in transcode_args(p, S)[transcode_args(p, S).index("-vf") + 1]


def test_sntp_offset_against_a_fake_ntp_server():
    import socket, struct, threading, time as _t
    from app.services.health_test import sntp_offset_ms
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]

    def serve():
        _, addr = srv.recvfrom(48)
        now = _t.time() + 2.5 + 2208988800  # the "server" clock is 2.5 s ahead
        sec, frac = int(now), int((now % 1) * 2 ** 32)
        reply = bytearray(48)
        reply[32:40] = struct.pack("!II", sec, frac)
        reply[40:48] = struct.pack("!II", sec, frac)
        srv.sendto(bytes(reply), addr)
    threading.Thread(target=serve, daemon=True).start()
    import app.services.health_test as ht
    real = socket.socket
    orig_sendto = real.sendto
    off, rtt = None, None
    # point the request at our fake server
    class S2(real):
        def sendto(self, data, addr):
            return orig_sendto(self, data, ("127.0.0.1", port))
    ht_socket = socket.socket
    socket.socket = S2
    try:
        off, rtt = sntp_offset_ms("fake.ntp")
    finally:
        socket.socket = ht_socket
        srv.close()
    assert 2400 < off < 2600 and rtt >= 0


def test_streams_with_and_without_the_clock(rig_factory):
    with rig_factory(["h264_2997.mp4"], mediamtx=False) as rig:
      # explicit option at creation: clock on = re-encoded with the clock; time zone IST or UTC
      s = rig.create("clk-utc", "h264_2997.mp4", start_now=False, clock=True, clock_tz="UTC")
      assert s["clock"] is True and s["clock_tz"] == "UTC" and s["output_mode"] == "transcode"
      assert any("Server clock (UTC)" in w for w in s["warnings"])
      # default: no clock, stream sent as it is; the zone defaults to IST
      d = rig.create("clk-none", "h264_2997.mp4", start_now=False)
      assert d["clock"] is False and d["clock_tz"] == "IST" and d["output_mode"] == "copy"
      # switching it off again goes back to copy
      r = rig.client.patch(f"/api/streams/{s['id']}", json={"clock": False})
      assert r.status_code == 200 and r.json()["clock"] is False and r.json()["output_mode"] == "copy"
      # only IST / UTC
      assert rig.client.post("/api/streams", json={"name": "clk-bad", "video_id": d["video_id"], "clock": True,
                                                   "clock_tz": "PST", "start_now": False}).status_code == 422
      assert rig.client.get("/api/system/time").json()["epoch_ms"] > 1.7e12
      assert "clock_overlay" in rig.client.get("/api/config").json()["features"]
