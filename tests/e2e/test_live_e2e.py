"""End-to-end checks against a RUNNING RIG-02 (the real container, not a test instance).

    scripts/e2e_live.sh                       # runs this inside the rig's image, host network
    RIG02_E2E_URL=http://127.0.0.1:8081 pytest tests/e2e -p no:cacheprovider

Keeps it small: a few short generated clips and at most 3 test streams at a time, all named
``zz_e2e_*`` / ``zz-e2e-*``; everything it creates is removed at the end. Existing streams and
videos are only read, never changed.

Optional: RIG02_E2E_USER / RIG02_E2E_PASS when login is on; RIG02_E2E_REPORT=<file.json> to
save the measurements (delays, frame counts, page sizes).
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest

URL = os.environ.get("RIG02_E2E_URL", "").rstrip("/")
pytestmark = pytest.mark.skipif(not URL, reason="set RIG02_E2E_URL to run the live end-to-end checks")

PREFIX = "zz_e2e"
FPS = 25
MEASURE: dict = {}


def note(key: str, value) -> None:
    MEASURE[key] = value


# ------------------------------------------------------------------ helpers

@pytest.fixture(scope="module")
def c():
    auth = None
    if os.environ.get("RIG02_E2E_USER"):
        auth = (os.environ["RIG02_E2E_USER"], os.environ.get("RIG02_E2E_PASS", ""))
    with httpx.Client(base_url=URL, auth=auth, timeout=120) as client:
        yield client


def ok(r: httpx.Response, *codes: int) -> httpx.Response:
    codes = codes or (200,)
    assert r.status_code in codes, f"{r.request.method} {r.request.url.path} -> {r.status_code}: {r.text[:400]}"
    return r


def wait(pred, timeout: float, desc: str, interval: float = 0.5):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out after {timeout:g}s waiting for {desc} (last={last!r})")


def make_clip(path: Path, w: int, h: int, seconds: float, gop: int = FPS) -> None:
    """A bright test pattern with a running counter: never black, every frame different."""
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={FPS}",
         "-t", str(seconds), "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
         "-pix_fmt", "yuv420p", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
         "-movflags", "+faststart", str(path)], check=True)


def rtsp_read(url: str, seconds: float, timeout: float = 90) -> dict:
    """Read an RTSP URL for ``seconds``; returns packets and stream info."""
    t0 = time.monotonic()
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", f"%+{seconds}",
         "-show_entries", "packet=pts_time,flags:stream=codec_type,codec_name,profile,width,height,"
         "pix_fmt,avg_frame_rate,r_frame_rate", "-select_streams", "v", "-of", "json", url],
        capture_output=True, timeout=timeout)
    wall = time.monotonic() - t0
    if p.returncode != 0:
        raise AssertionError(f"ffprobe {url}: {p.stderr.decode(errors='replace')[-400:]}")
    d = json.loads(p.stdout or b"{}")
    pkts = [x for x in d.get("packets", []) if x.get("pts_time") not in (None, "N/A")]
    # A reader joins mid-stream: packets before the first keyframe (e.g. a B-frame of the previous group)
    # can't be shown, a player drops them and starts at the keyframe. Count from there, like a player.
    k = next((i for i, x in enumerate(pkts) if "K" in x.get("flags", "")), None)
    joined = 0
    if k is not None:
        k_pts = float(pkts[k]["pts_time"])
        joined = len(pkts) - len(pkts[k:])
        pkts = [x for x in pkts[k:] if float(x["pts_time"]) >= k_pts]
    pts = [float(x["pts_time"]) for x in pkts]
    return {"pts": pts, "keyframes": sum("K" in x.get("flags", "") for x in pkts), "skipped_before_keyframe": joined,
            "stream": (d.get("streams") or [{}])[0], "wall": wall}


def check_continuous(r: dict, fps: float, label: str) -> dict:
    """Every frame present, in order, at the right rate: no drops, no duplicates, no gaps."""
    pts = sorted(r["pts"])
    assert len(pts) > fps * 3, f"{label}: only {len(pts)} frames"
    step = 1 / fps
    deltas = [b - a for a, b in zip(pts, pts[1:])]
    gaps = [d for d in deltas if d > step * 1.5]
    gap_at = [round(pts[i], 3) for i, d in enumerate(deltas) if d > step * 1.5]
    dups = [d for d in deltas if d < step * 0.5]
    span = pts[-1] - pts[0]
    expected = round(span * fps) + 1
    out = {"frames": len(pts), "expected": expected, "span_s": round(span, 2), "gaps": len(gaps),
           "duplicates": len(dups), "max_gap_ms": round(max(deltas) * 1000, 1), "gap_after_pts": gap_at,
           "first_pts": round(pts[0], 3)}
    assert not gaps, f"{label}: {len(gaps)} gaps (missing frames), largest {max(gaps):.3f}s; {out}"
    assert not dups, f"{label}: {len(dups)} duplicate timestamps; {out}"
    assert abs(len(pts) - expected) <= 1, f"{label}: frame count {len(pts)} != expected {expected}; {out}"
    return out


_DEC_ERR = re.compile(r"\[h264 @|error|corrupt|non-existing|concealing", re.I)


def first_frames_luma(url: str, seconds: float = 2) -> tuple[list[float], str, dict]:
    """What a viewer joining now actually sees in the first seconds.

    Returns the brightness (Y, 16-235) of each shown frame, decoder errors that happen AFTER the first
    shown frame (those would be visible), and info about the join: whether the first shown frame is a
    keyframe and how many decoder complaints came before it (frames received before the first keyframe
    can't be decoded; the player drops them, the viewer just waits up to one keyframe interval).
    """
    p = subprocess.run(
        ["ffmpeg", "-hide_banner", "-rtsp_transport", "tcp", "-i", url, "-t", str(seconds),
         "-vf", "showinfo,signalstats,metadata=print:key=lavfi.signalstats.YAVG", "-f", "null", "-"],
        capture_output=True, timeout=60)
    lines = p.stderr.decode(errors="replace").splitlines()
    first = next((i for i, ln in enumerate(lines) if re.search(r"Parsed_showinfo.* n:\s*0 ", ln)), len(lines))
    ys = [float(m) for ln in lines for m in re.findall(r"lavfi\.signalstats\.YAVG=([\d.]+)", ln)]
    is_err = lambda ln: _DEC_ERR.search(ln) and "Last message repeated" not in ln and "Parsed_" not in ln  # noqa: E731
    after = "\n".join(ln for ln in lines[first + 1:] if is_err(ln))
    info = {"first_shown_is_keyframe": bool(first < len(lines) and re.search(r"iskey:1", lines[first])),
            "dropped_before_first_keyframe_msgs": sum(1 for ln in lines[:first] if is_err(ln))}
    return ys, after, info


def stream(c, sid: int) -> dict:
    return ok(c.get(f"/api/streams/{sid}")).json()


def wait_running(c, sid: int, timeout: float = 60) -> dict:
    return wait(lambda: (s := stream(c, sid))["status"] == "RUNNING" and s["runtime"].get("path_ready") and s,
                timeout, f"stream {sid} RUNNING")


def create_stream(c, name: str, video_id: int, **kw) -> dict:
    body = {"name": name, "video_id": video_id, "mode": "loop", "lead_in_mode": "none", **kw}
    return ok(c.post("/api/streams", json=body), 201).json()


def rtsp_url(s: dict, host: str | None = None) -> str:
    """The URL as the dashboard shows it, read from this machine (optionally via another host)."""
    u = s["rtsp_url"]
    if host:
        u = re.sub(r"^rtsp://[^/]+", f"rtsp://{host}", u)
    return u


# ------------------------------------------------------------------ shared state

@pytest.fixture(scope="module")
def lab(c):
    """Uploads three clips (720p, 1080p, 4:3), cleans up streams/videos/links afterwards."""
    tmp = Path(tempfile.mkdtemp(prefix="rig02-e2e-"))
    st = {"tmp": tmp, "streams": [], "videos": [], "slots": []}
    clips = {"720": (1280, 720, 12), "1080": (1920, 1080, 8), "43": (640, 480, 6)}
    for key, (w, h, secs) in clips.items():
        make_clip(tmp / f"zz-e2e-{key}.mp4", w, h, secs)
    st["clips"] = {k: tmp / f"zz-e2e-{k}.mp4" for k in clips}
    yield st
    for sid in st["streams"]:
        c.delete(f"/api/streams/{sid}")
    for slot in st["slots"]:
        c.delete(f"/api/analytics/slots/{slot}")
    vids = ok(c.get("/api/videos")).json()
    for v in vids:  # also the converted copies made from our clips
        if v["rel_path"].startswith("zz-e2e-"):
            c.delete(f"/api/videos/{v['id']}")
    report = os.environ.get("RIG02_E2E_REPORT")
    if report:
        Path(report).write_text(json.dumps(MEASURE, indent=2, default=str))


# ================================================================== 1. platform

def test_01_health_and_mediamtx(c):
    h = ok(c.get("/api/health")).json()
    assert h["status"] == "ok" and h["mediamtx"] == "UP" and h["recovery"] == "done", h


def test_02_config_and_system(c):
    cfg = ok(c.get("/api/config")).json()
    sysinfo = ok(c.get("/api/system")).json()
    assert cfg["public_rtsp_port"] and cfg["rtsp_url_template"].startswith("rtsp://")
    assert sysinfo["cpu_count"] > 0 and sysinfo["memory"]["total"] > 0
    note("auth_enabled", cfg["auth_enabled"])
    note("rtsp_hosts", cfg["rtsp_hosts"])
    note("cpu_count", sysinfo["cpu_count"])


def test_03_built_in_health_test(c):
    r = ok(c.post("/api/health/test", timeout=300)).json()
    fails = [x for x in r["checks"] if x["status"] == "FAIL"]
    note("health_test", {"overall": r["overall"], **r["summary"],
                         "warnings": [x.get("name", x.get("id")) for x in r["checks"] if x["status"] == "WARN"]})
    assert not fails, fails


def test_04_encoder_hardware_is_reported(c):
    """Which encoders this machine can use (CPU always; NVIDIA / Intel / VAAPI if present) and the one in use."""
    enc = ok(c.get("/api/system/encoders")).json()
    names = {e["id"]: e for e in enc["encoders"]}
    assert names["cpu"]["available"] is True
    assert enc["active"] in names and names[enc["active"]]["available"]
    assert {"cpu", "nvidia", "intel", "vaapi"} <= set(names)
    note("encoders", {k: (v["available"], v.get("reason")) for k, v in names.items()} | {"active": enc["active"]})
    if enc["overridden"]:
        return  # someone chose an encoder on purpose: leave it
    r = ok(c.put("/api/system/encoders", json={"choice": "cpu"})).json()
    assert r["choice"] == "cpu" and r["active"] == "cpu"
    r = ok(c.put("/api/system/encoders", json={"choice": None})).json()
    assert r["choice"] == "auto" and not r["overridden"] and r["active"] == enc["active"]
    assert ok(c.get("/api/config")).json()["output_profile"]["encoder"] == names[enc["active"]]["encoder"]


# ================================================================== 2. existing data intact

def test_10_existing_streams_and_videos_have_metadata(c):
    streams = ok(c.get("/api/streams")).json()
    videos = ok(c.get("/api/videos")).json()
    real = [v for v in videos if not v["rel_path"].startswith("zz-e2e-")]
    assert len([s for s in streams if not s["name"].startswith(PREFIX)]) >= 1
    for v in real:
        if v["status"] != "READY":
            continue
        for k in ("duration_s", "width", "height", "fps", "video_codec", "size_bytes", "shape", "compat_mode"):
            assert v.get(k) not in (None, ""), f"{v['rel_path']}: no {k}"
        assert v["uploader"], v
    note("existing", {"streams": len(streams), "videos": len(real),
                      "running": sum(s["status"] == "RUNNING" for s in streams)})


def test_11_thumbnail_and_frame_grab(c):
    v = next(v for v in ok(c.get("/api/videos")).json() if v["status"] == "READY")
    r = ok(c.get(f"/api/videos/{v['id']}/thumbnail"))
    assert r.headers["content-type"].startswith("image/") and len(r.content) > 500
    r = ok(c.get(f"/api/videos/{v['id']}/frame", params={"t": 1}))
    assert r.content[:2] == b"\xff\xd8", "frame grab is not a JPEG"


# ================================================================== 3. upload / download / metadata

def test_20_upload_simple_and_metadata(c, lab):
    path = lab["clips"]["720"]
    r = ok(c.post("/api/videos/upload", params={"filename": path.name}, content=path.read_bytes(),
                  headers={"content-type": "application/octet-stream"}), 201).json()
    lab["videos"].append(r["id"])
    lab["v720"] = r
    v = wait(lambda: (x := ok(c.get(f"/api/videos/{r['id']}")).json())["status"] == "READY" and x, 60, "probe")
    assert (v["width"], v["height"]) == (1280, 720)
    assert abs(v["fps"] - FPS) < 0.01 and abs(v["duration_s"] - 12) < 0.2
    assert v["video_codec"] == "h264" and v["compat_mode"] == "copy" and v["is_16_9"] is True
    assert v["uploader"] and v["uploaded_at"]
    note("upload_uploader_shown", v["uploader"])


def test_21_upload_resumable_in_chunks(c, lab):
    for key in ("1080", "43"):
        path = lab["clips"][key]
        data = path.read_bytes()
        sess = ok(c.post("/api/videos/uploads", json={"filename": path.name, "size": len(data)}), 201).json()
        sid = sess["id"]
        off, res = 0, None
        chunk = max(64 * 1024, len(data) // 3)  # several pieces
        while off < len(data):
            res = c.put(f"/api/videos/uploads/{sid}", params={"offset": off}, content=data[off:off + chunk])
            ok(res, 200, 201)
            off += chunk
        assert res.status_code == 201, "last chunk should return the finished video"
        v = res.json()
        lab["videos"].append(v["id"])
        lab[f"v{key}"] = v
    v = wait(lambda: (x := ok(c.get(f"/api/videos/{lab['v1080']['id']}")).json())["status"] == "READY" and x, 60, "probe")
    assert (v["width"], v["height"]) == (1920, 1080)
    v43 = ok(c.get(f"/api/videos/{lab['v43']['id']}")).json()
    assert v43["shape"] == "4:3" and v43["is_16_9"] is False


def test_22_download_is_byte_identical(c, lab):
    r = ok(c.get(f"/api/videos/{lab['v720']['id']}/file", params={"download": True}))
    assert "attachment" in r.headers.get("content-disposition", "")
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(lab["clips"]["720"].read_bytes()).hexdigest()


def test_23_duplicate_upload_refused(c, lab):
    path = lab["clips"]["720"]
    chk = ok(c.post("/api/videos/upload/check", json={"filename": path.name, "size": path.stat().st_size})).json()
    assert chk["exists"] is True


def test_24_search_finds_upload(c, lab):
    vids = ok(c.get("/api/videos", params={"search": "zz-e2e"})).json()
    assert {lab["v720"]["id"], lab["v1080"]["id"], lab["v43"]["id"]} <= {v["id"] for v in vids}


# ================================================================== 4. analytics (fast access sidebar)

def _empty_analytics(c, n: int) -> list[dict]:
    cats = ok(c.get("/api/analytics")).json()
    return [a for cat in cats for a in cat["analytics"] if a["slots"] == 0][:n]


def test_30_attach_one_video_to_two_analytics(c, lab):
    targets = _empty_analytics(c, 2)
    assert len(targets) == 2, "need two analytics without videos for this test"
    vid = lab["v720"]["id"]
    ok(c.post("/api/analytics/assign", json={"video_ids": [vid], "analytics_ids": [a["id"] for a in targets]}))
    again = ok(c.post("/api/analytics/assign", json={"video_ids": [vid], "analytics_ids": [targets[0]["id"]]})).json()
    after = {a["id"]: a for cat in ok(c.get("/api/analytics")).json() for a in cat["analytics"]}
    for a in targets:
        assert after[a["id"]]["videos"] == 1, f"{a['name']} should list the video in the sidebar"
        lab["slots"] += after[a["id"]]["slot_ids"]
    tags = {t["id"] for t in ok(c.get(f"/api/videos/{vid}")).json()["analytics"]}
    assert tags == {a["id"] for a in targets}
    lab["analytics"] = targets
    note("assign_repeat_response", again)


def test_31_run_stream_from_analytics_counts_in_sidebar(c, lab):
    a = lab["analytics"][0]
    slot = next(s for s in lab["slots"] if s in {x for x in after_slots(c, a["id"])})
    r = ok(c.post(f"/api/analytics/slots/{slot}/streams",
                  json={"count": 1, "name_prefix": f"{PREFIX}_slot", "output_size": "original"}), 201).json()
    created = r["created"]
    assert created, r
    sid = created[0]["id"]
    lab["streams"].append(sid)
    wait(lambda: stream(c, sid)["status"] in ("RUNNING", "LEAD_IN"), 60, "slot stream live")
    side = wait(lambda: next((x for cat in ok(c.get("/api/analytics")).json() for x in cat["analytics"]
                              if x["id"] == a["id"] and x["running"] == 1), None), 30, "sidebar running=1")
    assert side["streams"] == 1
    ok(c.delete(f"/api/streams/{sid}"), 204)
    lab["streams"].remove(sid)


def after_slots(c, aid: int) -> list[int]:
    return next(x for cat in ok(c.get("/api/analytics")).json() for x in cat["analytics"] if x["id"] == aid)["slot_ids"]


# ================================================================== 5. streams: RTSP, frames, delays

def test_40_validation_errors_are_clear(c, lab):
    r = c.post("/api/streams", json={"name": "bad name!", "video_id": lab["v720"]["id"]})
    assert r.status_code in (400, 422)
    r = c.post("/api/streams", json={"name": f"{PREFIX}_43", "video_id": lab["v43"]["id"]})
    assert r.status_code in (400, 409, 422) and "16:9" in r.text, r.text


def test_41_copy_stream_startup_delay_and_first_frames(c, lab):
    t0 = time.monotonic()
    s = create_stream(c, f"{PREFIX}_copy", lab["v720"]["id"], output_size="original")
    lab["streams"].append(s["id"])
    lab["s_copy"] = s
    s = wait_running(c, s["id"])
    note("startup_copy_api_to_running_s", round(time.monotonic() - t0, 2))
    t1 = time.monotonic()
    ys, errors, info = first_frames_luma(rtsp_url(s), 2)
    note("join_to_2s_decoded_s", round(time.monotonic() - t1, 2))
    assert ys, "no frames decoded"
    assert info["first_shown_is_keyframe"], info
    assert min(ys[:5]) > 40, f"first frames are blank/dark: {ys[:5]}"
    assert not errors, f"decoder errors in shown frames (grey/corrupt picture):\n{errors}"
    note("first_frames_luma", [round(y) for y in ys[:5]])
    note("join_info_copy", info)
    assert s["output_mode"] == "copy" and s["output_resolution"] == "1280x720"


def test_42_no_dropped_frames_across_loop(c, lab):
    """20 s read of a 12 s clip in loop: crosses the loop point; every frame must arrive on time."""
    s = lab["s_copy"]
    r = rtsp_read(rtsp_url(s), 20)
    res = check_continuous(r, FPS, "copy stream")
    st = r["stream"]
    assert st["codec_name"] == "h264" and (st["width"], st["height"]) == (1280, 720)
    pace = (r["pts"][-1] - r["pts"][0]) / r["wall"]
    assert 0.85 < pace < 1.1, f"not real time: {pace:.2f}x"
    res["pace"] = round(pace, 3)
    note("frames_copy_loop", res)
    prog = stream(c, s["id"])["runtime"]["progress"]
    assert prog["drop_frames"] == 0 and prog["dup_frames"] == 0, prog
    assert stream(c, s["id"])["loop_count"] >= 1


def test_43_viewer_count(c, lab):
    s = lab["s_copy"]
    with cf.ThreadPoolExecutor(1) as ex:
        fut = ex.submit(rtsp_read, rtsp_url(s), 8)
        n = wait(lambda: stream(c, s["id"])["runtime"]["viewer_count"], 8, "viewer counted")
        fut.result()
    assert n >= 1
    wait(lambda: stream(c, s["id"])["runtime"]["viewer_count"] == 0, 15, "viewer gone")
    assert stream(c, s["id"])["runtime"]["viewer_peak_today"] >= 1


def test_44_stop_start_restart(c, lab):
    sid = lab["s_copy"]["id"]
    ok(c.post(f"/api/streams/{sid}/stop"))
    wait(lambda: stream(c, sid)["status"] == "STOPPED", 30, "stopped")
    # A stopped stream sends nothing. By design (mediamtx.yml "early readers") a camera that connects now
    # is held for up to 60 s instead of refused, and gets the video as soon as the stream starts again.
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-read_intervals", "%+2",
                            "-show_entries", "packet=pts_time", "-of", "json", lab["s_copy"]["rtsp_url"]],
                           capture_output=True, timeout=6)
        assert p.returncode != 0 or not json.loads(p.stdout or b"{}").get("packets"), "stopped stream still plays"
    except subprocess.TimeoutExpired:
        pass  # held, no video: correct
    t0 = time.monotonic()
    with cf.ThreadPoolExecutor(1) as ex:
        early = ex.submit(rtsp_read, rtsp_url(lab["s_copy"]), 6)  # connects while still stopped
        time.sleep(2)
        ok(c.post(f"/api/streams/{sid}/start"))
        s = wait_running(c, sid)
        note("restart_after_stop_s", round(time.monotonic() - t0 - 2, 2))
        r = early.result()
    note("early_reader_got_video_frames", len(r["pts"]))
    check_continuous(r, FPS, "camera waiting while stopped, then started")
    before = stream(c, sid)["pid"]
    ok(c.post(f"/api/streams/{sid}/restart"))
    s = wait(lambda: (x := stream(c, sid))["status"] == "RUNNING" and x["pid"] != before and x, 60, "restarted")
    check_continuous(rtsp_read(rtsp_url(s), 6), FPS, "after restart")


def test_45_lead_in_slate_is_not_blank(c, lab):
    """Default lead-in: a slate before the video (never a black or grey screen)."""
    s = create_stream(c, f"{PREFIX}_leadin", lab["v720"]["id"], output_size="original",
                      lead_in_mode="fixed", lead_in_s=5)
    lab["streams"].append(s["id"])
    wait(lambda: stream(c, s["id"])["status"] in ("LEAD_IN", "RUNNING"), 30, "lead-in")
    ys, errors, info = first_frames_luma(s["rtsp_url"], 2)
    assert ys and min(ys) > 16.5, f"slate frames look black: {ys[:5]}"
    assert not errors, errors
    assert info["first_shown_is_keyframe"], info
    note("first_frames_slate_luma", [round(y) for y in ys[:5]])
    wait_running(c, s["id"], 30)
    ok(c.delete(f"/api/streams/{s['id']}"), 204)
    lab["streams"].remove(s["id"])


def test_46_three_streams_at_once_transcode_and_aspect(c, lab):
    """Copy 720p + live transcode 1080p->720p + a 4:3 clip made 16:9 (pad); all read at the same time."""
    job = ok(c.post(f"/api/videos/{lab['v43']['id']}/convert", json={"profile": "16x9", "fit": "pad"}), 202).json()
    conv = wait(lambda: next((v for v in ok(c.get("/api/videos")).json()
                              if v["rel_path"].startswith("zz-e2e-43 (720p 16x9)") and v["status"] == "READY"), None),
                120, f"16:9 copy ({job.get('id')})")
    assert (conv["width"], conv["height"]) == (1280, 720) and conv["is_16_9"]
    t0 = time.monotonic()
    s_tc = create_stream(c, f"{PREFIX}_tc", lab["v1080"]["id"], output_size="standard")
    lab["streams"].append(s_tc["id"])
    s_43 = create_stream(c, f"{PREFIX}_43", conv["id"], output_size="original")
    lab["streams"].append(s_43["id"])
    s_tc = wait_running(c, s_tc["id"])
    note("startup_transcode_api_to_running_s", round(time.monotonic() - t0, 2))
    s_43 = wait_running(c, s_43["id"])
    assert s_tc["output_mode"] == "transcode" and s_tc["output_resolution"] == "1280x720"
    detail = stream(c, s_tc["id"])
    note("transcode_encoder_args", [a for a in (detail.get("ffmpeg_command") or []) if "264" in a or "vaapi" in a])
    urls = {"copy": rtsp_url(lab["s_copy"]), "transcode": rtsp_url(s_tc), "4:3->16:9": rtsp_url(s_43)}
    with cf.ThreadPoolExecutor(3) as ex:
        reads = {k: ex.submit(rtsp_read, u, 15) for k, u in urls.items()}
        results = {k: f.result() for k, f in reads.items()}
    out = {}
    for k, r in results.items():
        out[k] = check_continuous(r, FPS, k)
        assert (r["stream"]["width"], r["stream"]["height"]) == (1280, 720), (k, r["stream"])
    note("frames_three_parallel", out)


def test_47_hls_preview_and_snapshot(c, lab):
    sid = lab["s_copy"]["id"]
    t0 = time.monotonic()
    r = wait(lambda: (x := c.get(f"/api/streams/{sid}/live/index.m3u8")).status_code == 200 and x, 90, "HLS playlist")
    assert "#EXTM3U" in r.text
    note("browser_preview_first_playlist_s", round(time.monotonic() - t0, 2))
    snap = ok(c.get(f"/api/streams/{sid}/snapshot"))
    assert snap.content[:2] == b"\xff\xd8"


def test_48_reachable_on_every_rtsp_address(c, lab):
    """LAN and Tailscale addresses offered by the dashboard both serve the stream (and the web page)."""
    hosts = [h["host"] for h in ok(c.get("/api/config")).json()["rtsp_hosts"]]
    port = ok(c.get("/api/config")).json()["public_rtsp_port"]
    web_port = httpx.URL(URL).port
    seen = {}
    for h in hosts:
        r = rtsp_read(rtsp_url(lab["s_copy"], f"{h}:{port}"), 3)
        assert len(r["pts"]) > FPS * 2, h
        assert httpx.get(f"http://{h}:{web_port}/api/health", timeout=10).status_code == 200, h
        seen[h] = len(r["pts"])
    note("reachable_hosts", seen)


def test_49_events_logs_history(c, lab):
    sid = lab["s_copy"]["id"]
    items = ok(c.get("/api/events", params={"limit": 200})).json()
    kinds = {e["kind"] for e in items if e.get("stream_id") == sid}
    assert kinds, "no events recorded for the test stream"
    assert ok(c.get(f"/api/streams/{sid}/logs")).json()["lines"]
    h = ok(c.get(f"/api/streams/{sid}/history", params={"hours": 1})).json()
    assert h
    note("event_kinds_for_test_stream", sorted(kinds))


# ================================================================== 6. capacity / scaling

def test_50_capacity_limits_and_validation(c):
    cap = ok(c.get("/api/settings/capacity")).json()
    assert cap["max_active_streams"]["value"] >= 1
    assert 0 <= cap["max_transcode_streams"]["value"] <= 64
    r = c.put("/api/settings/capacity", json={"max_active_streams": cap["max_active_streams"]["max"] + 1})
    assert r.status_code == 422
    after = ok(c.get("/api/settings/capacity")).json()
    assert after["max_active_streams"]["value"] == cap["max_active_streams"]["value"], "refused change must not apply"
    note("capacity", {k: v["value"] for k, v in cap.items()})


def test_51_metrics_endpoint(c):
    r = ok(c.get("/api/metrics"))
    assert "rtspm" in r.text or "stream" in r.text


# ================================================================== 7. web pages (served, fast)

@pytest.mark.parametrize("path", ["/", "/streams", "/library", "/analytics/1", "/events", "/health"])
def test_60_pages_served_fast(c, path):
    t0 = time.monotonic()
    r = ok(c.get(path))
    ms = (time.monotonic() - t0) * 1000
    assert "<div id=\"root\"" in r.text or "id=root" in r.text
    assert ms < 1000, f"{path} took {ms:.0f} ms"
    MEASURE.setdefault("page_ms", {})[path] = round(ms)


def test_61_api_responses_fast(c):
    out = {}
    for p in ("/api/streams", "/api/videos", "/api/analytics", "/api/system", "/api/health"):
        t0 = time.monotonic()
        ok(c.get(p))
        out[p] = round((time.monotonic() - t0) * 1000)
    note("api_ms", out)
    slow = {k: v for k, v in out.items() if v > 1500}
    assert not slow, slow


def test_62_assets_compressed_and_cached(c):
    html = ok(c.get("/")).text
    js = re.findall(r'src="(/assets/[^"]+\.js)"', html)
    assert js
    r = ok(c.get(js[0], headers={"accept-encoding": "gzip"}))
    assert r.headers.get("content-encoding") == "gzip"
    note("main_js_kb_gzip", round(int(r.headers.get("content-length") or len(r.content)) / 1024))
    assert "max-age" in r.headers.get("cache-control", "") or "immutable" in r.headers.get("cache-control", "")
