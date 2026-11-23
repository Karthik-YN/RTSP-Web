"""Live-transcode encoder: detection, choice (auto / fixed / fallback), FFmpeg arguments, API."""

from pathlib import Path

import pytest

from app.config import Settings
from app.services import encoders as E
from app.services.ffmpeg_manager import PublishPlan, build_command, loop_entries, transcode_args

S = Settings(_env_file=None)


def plan(**kw):
    base = dict(stream_id=7, name="cam1", source_path=Path("/v/clip.mp4"), inpoint_abs=0.0, outpoint_abs=None,
                duration_s=60.0, mode="loop", output_mode="transcode", publish_url="rtsp://127.0.0.1:8554/cam1",
                playlist_path=Path("/r/p.ffconcat"), fps=25.0, width=1920, height=1080,
                out_width=1280, out_height=720)
    base.update(kw)
    p = PublishPlan(**base)
    p.entries = loop_entries(p.period_s, S)
    return p


def opt(args, flag):
    return args[args.index(flag) + 1]


def test_cpu_is_the_default_and_unchanged():
    a = transcode_args(plan(), S)
    assert opt(a, "-c:v") == "libx264" and opt(a, "-preset") == S.x264_preset
    assert "hwupload" not in opt(a, "-vf")
    cmd = build_command(plan(), S)
    assert not {"-vaapi_device", "-init_hw_device", "-filter_hw_device"} & set(cmd)  # no GPU setup for CPU


@pytest.mark.parametrize("enc,codec", [("nvidia", "h264_nvenc"), ("intel", "h264_qsv"), ("vaapi", "h264_vaapi")])
def test_gpu_arguments(enc, codec, monkeypatch):
    monkeypatch.setattr(E, "render_node", lambda: "/dev/dri/renderD129")
    p = plan(encoder=enc)
    a = transcode_args(p, S)
    assert opt(a, "-c:v") == codec
    assert opt(a, "-g") == "50" and "-force_key_frames" in a and opt(a, "-bf") == "0"  # same keyframe rules
    assert opt(a, "-maxrate") == f"{S.x264_maxrate_kbps}k"
    vf = opt(a, "-vf")
    assert vf.startswith("scale=1280:720") or "1280" in vf  # still scaled to the standard size
    cmd = build_command(p, S)
    i = cmd.index("-i")
    if enc == "nvidia":
        assert "hwupload" not in vf and "-vaapi_device" not in cmd
    else:
        assert vf.endswith("hwupload") or "hwupload=extra_hw_frames" in vf
        dev = "-vaapi_device" if enc == "vaapi" else "-init_hw_device"
        assert cmd.index(dev) < i and "renderD129" in cmd[cmd.index(dev) + 1]


def test_gpu_upload_added_even_without_scaling(monkeypatch):
    a = transcode_args(plan(encoder="vaapi", out_width=None, out_height=None, width=1280, height=720), S)
    assert opt(a, "-vf") == "format=nv12,hwupload"


def test_copy_mode_never_uses_gpu_options():
    cmd = build_command(plan(output_mode="copy", encoder="vaapi"), S)
    assert "-vaapi_device" not in cmd and opt(cmd, "-c:v") == "copy"


class FakeRS:
    def __init__(self, v=None):
        self.v = v

    def get_str(self, key):
        return self.v


def svc(results: dict, choice=None):
    s = E.EncoderService(S, FakeRS(choice))
    s.results = {k: {"id": k, **E.INFO[k], "available": k in results, "reason": None, "test_ms": 1}
                 for k in ("cpu", "nvidia", "intel", "vaapi")}
    s.checked_at = 1.0
    return s


def test_auto_picks_best_working():
    assert svc({"cpu"}).active() == "cpu"  # laptop without GPU
    assert svc({"cpu", "vaapi"}).active() == "vaapi"
    assert svc({"cpu", "intel", "vaapi"}).active() == "intel"
    assert svc({"cpu", "nvidia", "intel"}).active() == "nvidia"


def test_fixed_choice_and_fallback():
    assert svc({"cpu", "nvidia"}, "cpu").active() == "cpu"  # GPU present but CPU chosen
    s = svc({"cpu"}, "nvidia")
    v = s.view()
    assert v["active"] == "cpu" and v["choice"] == "nvidia" and "doesn't work here" in v["note"]


def test_detection_without_devices_reports_reasons(monkeypatch):
    monkeypatch.setattr(E, "render_node", lambda: None)
    monkeypatch.setattr(E, "nvidia_present", lambda: False)
    s = E.EncoderService(S)
    calls = []
    real = E.subprocess.run

    def run(argv, **kw):
        calls.append(argv)
        return real(argv, **kw)
    monkeypatch.setattr(E.subprocess, "run", run)
    r = s.detect()
    assert [c for c in calls if "libx264" in c] and not [c for c in calls if "h264_nvenc" in c]
    assert r["nvidia"]["available"] is False and "NVIDIA" in r["nvidia"]["reason"]
    assert r["vaapi"]["available"] is False and "/dev/dri" in r["vaapi"]["reason"]


def test_api_encoders(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False)
    with rig:
        c = rig.client
        v = c.get("/api/system/encoders").json()
        ids = {e["id"]: e for e in v["encoders"]}
        assert set(ids) == {"cpu", "nvidia", "intel", "vaapi"} and ids["cpu"]["available"]
        assert v["choice"] == "auto" and v["active"] in ids
        assert c.put("/api/system/encoders", json={"choice": "gpu"}).status_code == 422
        r = c.put("/api/system/encoders", json={"choice": "cpu"}).json()
        assert r["choice"] == "cpu" and r["active"] == "cpu" and r["overridden"]
        assert c.get("/api/config").json()["output_profile"]["encoder"] == "libx264"
        assert c.post("/api/system/encoders/detect").status_code == 200
    with rig:  # saved across restarts
        c = rig.client
        assert c.get("/api/system/encoders").json()["choice"] == "cpu"
        r = c.put("/api/system/encoders", json={"choice": None}).json()
        assert r["choice"] == "auto" and not r["overridden"]
        assert any(e["kind"] == "settings.encoder" for e in c.get("/api/events?limit=20").json())
