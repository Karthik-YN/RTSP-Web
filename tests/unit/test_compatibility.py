"""Codec compatibility policy (EC-01)."""

from app.config import Settings
from app.services.compatibility import COPY, REJECT, TRANSCODE, evaluate

S = Settings(_env_file=None)
DEC = frozenset({"h264", "hevc", "vp9", "av1"})


def meta(**kw):
    m = dict(video_codec="h264", video_profile="High", pix_fmt="yuv420p", bit_depth=8, width=1920,
             height=1080, fps=25.0, is_vfr=False, audio_codec=None, field_order="progressive",
             keyframe_interval_s=2.0)
    m.update(kw)
    return m


def test_h264_8bit_is_copied():
    r = evaluate(meta(), S, DEC, True)
    assert r.mode == COPY and r.reasons == []


def test_audio_is_flagged_as_dropped():
    r = evaluate(meta(audio_codec="aac"), S, DEC, True)
    assert r.mode == COPY and any("dropped" in w for w in r.warnings)


def test_yuvj420p_copy_ok():
    assert evaluate(meta(pix_fmt="yuvj420p"), S, DEC, True).mode == COPY


def test_long_gop_warning():
    r = evaluate(meta(keyframe_interval_s=10.0), S, DEC, True)
    assert r.mode == COPY and any("Keyframe interval" in w for w in r.warnings)


def test_hevc_vp9_av1_are_transcoded_with_cpu_warning():
    for codec in ("hevc", "vp9", "av1"):
        r = evaluate(meta(video_codec=codec, video_profile="Main"), S, DEC, True)
        assert r.mode == TRANSCODE, codec
        assert any(codec.upper() in x for x in r.reasons)
        assert any("CPU" in w for w in r.warnings)


def test_10bit_h264_is_not_copied():
    r = evaluate(meta(video_profile="High 10", pix_fmt="yuv420p10le", bit_depth=10), S, DEC, True)
    assert r.mode == TRANSCODE
    assert any("10-bit" in x for x in r.reasons)


def test_422_profile_not_copied():
    r = evaluate(meta(video_profile="High 4:2:2", pix_fmt="yuv422p"), S, DEC, True)
    assert r.mode == TRANSCODE


def test_oversize_and_high_fps_not_copied():
    assert evaluate(meta(width=7680, height=4320), S, DEC, True).mode == TRANSCODE
    assert evaluate(meta(fps=120), S, DEC, True).mode == TRANSCODE


def test_policy_never_rejects_with_reason():
    s = Settings(_env_file=None, transcode_policy="never")
    r = evaluate(meta(video_codec="hevc"), s, DEC, True)
    assert r.mode == REJECT and any("disabled" in x for x in r.reasons)


def test_policy_always_transcodes_everything():
    s = Settings(_env_file=None, transcode_policy="always")
    assert evaluate(meta(), s, DEC, True).mode == TRANSCODE


def test_force_transcode_per_stream():
    assert evaluate(meta(), S, DEC, True, force_transcode=True).mode == TRANSCODE


def test_missing_decoder_or_encoder_rejects():
    assert evaluate(meta(video_codec="av1"), S, frozenset({"h264"}), True).mode == REJECT
    assert evaluate(meta(video_codec="hevc"), S, DEC, False).mode == REJECT


def test_no_video_stream_rejected():
    assert evaluate(meta(video_codec=None), S, DEC, True).mode == REJECT
