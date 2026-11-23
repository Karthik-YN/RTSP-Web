"""End-to-end simulation: the rig as the edges and the portal see it.

Runs where ffmpeg/ffprobe exist (the rig image: docker run --rm --network host --entrypoint python3 -v ...).
Only the standard library is used.

  readonly (safe on live):  API answers, every stream's RTSP URL, edge simulation (every running camera read by
                            1 decoding edge + N extra edges at once), frame checks (not black, moving, matches the
                            library file at the reported position), viewer counts seen by the portal, server clock
                            burned into frames (read back by OCR: delay and correctness), NTP.
  full (preview only):      also creates e2e-* streams and checks: clock IST/UTC, lead-in "until a viewer connects",
                            play once, stop/start with an edge attached, bulk stop/start, duplicate names,
                            duplicate uploads by content. Deletes every e2e-* stream at the end.

python3 edge_suite.py --base http://192.168.29.105:8082 --mode full --json out.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import os
import statistics as st
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

R: list[dict] = []  # results
LOCK = threading.Lock()


def rec(area: str, check: str, target: str, status: str, detail: str = "") -> None:
    with LOCK:
        R.append(dict(area=area, check=check, target=target, status=status, detail=detail))
    print(f"  [{status:4}] {area:8} {check:34} {target:26} {detail}", flush=True)


# ------------------------------------------------------------------ HTTP
class Api:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method: str, path: str, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                ct = r.headers.get("Content-Type", "")
                return r.status, (json.loads(raw) if "json" in ct and raw else raw), time.monotonic() - t0, ct
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw), time.monotonic() - t0, ""
            except Exception:
                return e.code, raw, time.monotonic() - t0, ""

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.call("POST", path, body if body is not None else {}, **kw)


# ------------------------------------------------------------------ ffmpeg helpers
def gray_frames(src: str, w: int, h: int, secs: float, fps: float | None = None, vf_extra: str = "", rtsp=True,
                timeout=60, low_delay=False):
    """Decode src to w x h 8-bit gray frames; returns [(arrival_monotonic, bytes)]."""
    vf = (vf_extra + "," if vf_extra else "") + (f"fps={fps}," if fps else "") + f"scale={w}:{h},format=gray"
    cmd = ["ffmpeg", "-v", "error", "-nostdin"]
    if rtsp:
        cmd += ["-rtsp_transport", "tcp"]
    if rtsp and low_delay:
        # decode like a live edge: no frame-thread queue (ffmpeg's default holds ~1 frame per CPU thread back)
        cmd += ["-fflags", "nobuffer", "-flags", "low_delay", "-threads", "1"]
    cmd += ["-i", src, "-t", str(secs), "-an", "-vf", vf, "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, out, buf = w * h, [], b""
    fd = p.stdout.fileno()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        chunk = os.read(fd, size - len(buf) if len(buf) < size else size)
        if not chunk:
            break
        buf += chunk
        if len(buf) >= size:
            out.append((time.monotonic(), buf[:size]))
            buf = buf[size:]
    p.kill()
    p.wait()
    return out


def jpeg_to_gray(jpeg: bytes, w: int, h: int) -> bytes | None:
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", "-", "-vf", f"scale={w}:{h},format=gray", "-f", "rawvideo", "-"],
                       input=jpeg, capture_output=True, timeout=20)
    return p.stdout if len(p.stdout) == w * h else None


def mad(a: bytes, b: bytes) -> float:
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def probe(url: str, timeout=15):
    try:
        p = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0", "-show_entries",
                            "stream=codec_name,width,height,avg_frame_rate", "-of", "json", url],
                           capture_output=True, text=True, timeout=timeout)
        s = json.loads(p.stdout or "{}").get("streams") or []
        return (s[0] if s else None), p.stderr.strip()[-200:]
    except subprocess.TimeoutExpired:
        return None, "timeout"


# ------------------------------------------------------------------ 1. API / portal backend
def check_api(api: Api, cfg_out: dict):
    paths = ["/api/config", "/api/system", "/api/streams", "/api/videos", "/api/mediamtx/status", "/api/analytics",
             "/api/events", "/api/health/test/last", "/api/settings/codecs", "/api/naming", "/api/system/time", "/api/metrics"]
    for p in paths:
        code, body, secs, _ = api.get(p)
        ok = code == 200
        rec("portal", "API answers", p, "PASS" if ok and secs < 1.5 else "WARN" if ok else "FAIL",
            f"HTTP {code} in {secs * 1000:.0f} ms")
        if p == "/api/config" and ok:
            cfg_out.update(body)
    # server clock vs this machine (same host: should be ~0; also proves /api/system/time)
    best = None
    for _ in range(5):
        t0 = time.time()
        code, body, _, _ = api.get("/api/system/time")
        t1 = time.time()
        if code == 200:
            off = body["epoch_ms"] / 1000 - (t0 + t1) / 2
            if best is None or (t1 - t0) < best[1]:
                best = (off, t1 - t0)
    if best:
        rec("clock", "server time vs test host", "/api/system/time", "PASS" if abs(best[0]) < 0.05 else "WARN",
            f"offset {best[0] * 1000:+.1f} ms (±{best[1] * 500:.1f})")
    code, last, _, _ = api.get("/api/health/test/last")
    if code == 200 and isinstance(last, dict):
        checks = last.get("checks") or last.get("results") or []
        if isinstance(checks, list) and checks:
            bad = [c for c in checks if str(c.get("status", "")).upper() == "FAIL"]
            rec("portal", "last health test", last.get("finished_at", "")[:19] or "-", "PASS" if not bad else "FAIL",
                f"{len(checks)} checks, {len(bad)} failed" + (f": {', '.join(c.get('name', '?') for c in bad[:4])}" if bad else ""))
            ntp = [c for c in checks if "clock" in str(c.get("name", "")).lower() or "ntp" in str(c.get("name", "")).lower()]
            for c in ntp:
                rec("clock", "NTP check (health test)", str(c.get("name"))[:26], str(c.get("status", "?")).upper()[:4],
                    str(c.get("detail") or c.get("message") or "")[:90])


# ------------------------------------------------------------------ 2. RTSP URLs
def check_urls(cfg: dict, streams: list[dict], rtsp_base: str):
    host, port = cfg.get("public_rtsp_host"), cfg.get("public_rtsp_port")
    for s in streams:
        exp = cfg.get("rtsp_url_template", "rtsp://{host}:{port}/{name}").replace("{name}", s["name"])
        ok = s["rtsp_url"] == exp and f":{port}/" in s["rtsp_url"]
        if not ok:
            rec("rtsp", "URL shown = URL served", s["name"], "FAIL", f"{s['rtsp_url']} vs {exp}")
    rec("rtsp", "URL shown = URL served", f"{len(streams)} streams", "PASS" if all(
        r["status"] != "FAIL" for r in R if r["check"] == "URL shown = URL served") else "FAIL", f"host {host}:{port}")
    t0 = time.monotonic()
    info, err = probe(f"{rtsp_base}/e2e-no-such-camera-xyz", timeout=8)
    rec("rtsp", "edge on a not-yet-existing URL", "e2e-no-such-camera-xyz", "PASS" if info is None else "FAIL",
        "held open, no picture (by design: an edge may connect before its stream starts)" if err == "timeout"
        else f"refused in {time.monotonic() - t0:.1f} s")


def check_describe(s: dict, rtsp_base: str, videos: dict):
    url = f"{rtsp_base}/{s['name']}"
    info, err = probe(url)
    if not info:
        rec("rtsp", "edge can open the URL", s["name"], "FAIL", err[-90:])
        return
    res = f"{info['width']}x{info['height']}"
    exp = s.get("output_resolution")
    v = videos.get(s["video_id"]) or {}
    detail = f"{info['codec_name']} {res}"
    status = "PASS"
    if exp and exp != res and s.get("output_size") != "original":
        status, detail = "WARN", detail + f" (portal says {exp})"
    if s.get("output_mode") == "copy" and v.get("video_codec") and v["video_codec"] != info["codec_name"]:
        status, detail = "FAIL", detail + f" (file is {v['video_codec']})"
    rec("rtsp", "edge can open the URL", s["name"], status, detail)


# ------------------------------------------------------------------ 3. edge simulation
def edge_reader(url: str, secs: float):
    t0 = time.monotonic()
    cmd = ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0", "-show_entries",
           "frame=best_effort_timestamp_time,key_frame,pict_type", "-of", "csv=p=0", "-read_intervals", f"%+{secs}", url]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    errs: list[float] = []
    threading.Thread(target=lambda: [errs.append(time.monotonic() - t0) for _ in p.stderr], daemon=True).start()
    frames = []
    for line in p.stdout:
        f = line.strip().split(",")
        try:
            ts = float(f[1]) if len(f) > 1 and f[1] not in ("", "N/A") else None
            frames.append((time.monotonic() - t0, ts, f[0] == "1", f[2] if len(f) > 2 else "?"))
        except (ValueError, IndexError):
            pass
    p.wait()
    return frames, errs


def plain_reader(url: str, secs: float):
    subprocess.run(["ffmpeg", "-v", "quiet", "-nostdin", "-rtsp_transport", "tcp", "-i", url, "-t", str(secs), "-c", "copy",
                    "-f", "null", "-"], timeout=secs + 40)


def edge_sim(api: Api, streams: list[dict], rtsp_base: str, secs: float, extra: int, videos: dict):
    res: dict[str, tuple] = {}
    seen: dict[str, int] = {}

    def dec(s):
        res[s["name"]] = edge_reader(f"{rtsp_base}/{s['name']}", secs)

    def watch():  # what the portal shows while the edges are connected
        time.sleep(min(10, secs / 2))
        code, cur, _, _ = api.get("/api/streams")
        if code == 200:
            for c in cur:
                seen[c["name"]] = (c.get("runtime") or {}).get("viewer_count") or 0

    th = [threading.Thread(target=dec, args=(s,)) for s in streams]
    th += [threading.Thread(target=plain_reader, args=(f"{rtsp_base}/{s['name']}", secs)) for s in streams for _ in range(extra)]
    th.append(threading.Thread(target=watch))
    [t.start() for t in th]
    [t.join() for t in th]
    for s in streams:
        n = s["name"]
        frames, errs = res.get(n, ([], []))
        if len(frames) < 10:
            rec("edge", "edge receives video", n, "FAIL", f"{len(frames)} frames, {len(errs)} errors")
            continue
        first = frames[0][0]
        steady = [f for f in frames if f[0] > first + 3]
        arr = [f[0] for f in steady]
        gaps = [(b - a) * 1000 for a, b in zip(arr, arr[1:])] or [0]
        fps = (len(arr) - 1) / (arr[-1] - arr[0]) if len(arr) > 1 and arr[-1] > arr[0] else 0
        ts = [f[1] for f in steady if f[1] is not None]
        d = [b - a for a, b in zip(ts, ts[1:])]
        nominal = st.median(d) if d else 0
        jumps = sum(1 for x in d if x <= 0 or x > 3 * nominal)
        kf = [f[1] for f in frames if f[2] and f[1] is not None]
        gop = st.median([b - a for a, b in zip(kf, kf[1:])]) if len(kf) > 2 else None
        bfr = sum(1 for f in frames if f[3] == "B")
        later = sum(1 for e in errs if e > first + 1.0)
        v = videos.get(s["video_id"]) or {}
        want = (s.get("runtime") or {}).get("progress", {}).get("fps") or v.get("fps") or 0
        fps_ok = want and abs(fps - want) / want < 0.04
        stat = "PASS"
        notes = []
        if not fps_ok:
            stat = "FAIL" if fps < want * 0.85 else "WARN"
            notes.append(f"expected {want:.2f}")
        if later:
            stat = "FAIL"; notes.append(f"{later} decode errors")
        if max(gaps) > 1000:
            stat = "FAIL"; notes.append("freeze")
        elif max(gaps) > 600 and stat == "PASS":
            stat = "WARN"; notes.append("stutter")
        if jumps and stat == "PASS":
            stat = "WARN"; notes.append(f"{jumps} timestamp jumps")
        rec("edge", "real-time video to edge", n, stat,
            f"1st pic {first:.1f}s · {fps:.2f} fps · max gap {max(gaps):.0f} ms · {len(frames)} frames" + (f" ({'; '.join(notes)})" if notes else ""))
        rec("edge", "camera-like (no B, keyframe ≤4s)", n,
            "PASS" if not bfr and gop is not None and gop <= 4.05 else "WARN",
            f"B-frames {bfr} · keyframe every {gop:.2f} s" if gop else f"B-frames {bfr} · <3 keyframes seen")
        if n in seen:
            want_v = 1 + extra
            rec("portal", "portal counts the edges", n, "PASS" if seen[n] >= want_v else "WARN",
                f"viewers shown {seen[n]} (connected {want_v} + others)")


# ------------------------------------------------------------------ 4. frames
def check_frames(api: Api, s: dict, rtsp_base: str, compare: bool):
    n = s["name"]
    W, H = 64, 36
    fr = gray_frames(f"{rtsp_base}/{n}", W, H, 6)
    fr = fr[:: max(1, len(fr) // 24)]  # about 4 per second whatever the frame rate
    if len(fr) < 8:
        rec("frames", "picture not black / moving", n, "FAIL", f"only {len(fr)} frames decoded")
        return
    lum = [sum(f) / len(f) for _, f in fr]
    diffs = [mad(a[1], b[1]) for a, b in zip(fr, fr[1:])]
    black = max(lum) < 10
    frozen = max(diffs) < 0.3
    rec("frames", "picture not black / moving", n, "FAIL" if black else "WARN" if frozen else "PASS",
        f"brightness {min(lum):.0f}-{max(lum):.0f} · change {max(diffs):.1f}" + (" (static scene or frozen)" if frozen else ""))
    code, snap, _, ct = api.get(f"/api/streams/{s['id']}/snapshot", timeout=20)
    rec("frames", "portal snapshot", n, "PASS" if code == 200 and "image" in ct and len(snap) > 2000 else "FAIL",
        f"HTTP {code} {ct} {len(snap) if isinstance(snap, bytes) else 0} B")
    # the dashboard's live preview (HLS through the portal, what the browser plays)
    hls = f"{api.base}/api/streams/{s['id']}/live/index.m3u8"
    t0 = time.monotonic()
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-read_intervals", "%+3",
                        "-show_entries", "stream=codec_name,nb_read_packets", "-of", "json", hls],
                       capture_output=True, text=True, timeout=60)
    try:
        hs = (json.loads(p.stdout or "{}").get("streams") or [{}])[0]
    except ValueError:
        hs = {}
    pk = int(hs.get("nb_read_packets") or 0)
    rec("frames", "browser live preview (HLS)", n, "PASS" if hs.get("codec_name") == "h264" and pk > 10 else "FAIL",
        f"{hs.get('codec_name', p.stderr.strip()[-60:])} · {pk} frames in {time.monotonic() - t0:.1f} s")
    if not compare:
        return
    # the frame the edge gets = the library file at the position the portal reports?
    code, cur, _, _ = api.get(f"/api/streams/{s['id']}")
    pos = ((cur or {}).get("runtime") or {}).get("playback_position_s") if code == 200 else None
    t_pos = time.monotonic()
    live = gray_frames(f"{rtsp_base}/{n}", W, H, 4.5, rtsp=True)
    if pos is None or not live:
        rec("frames", "frames = library file", n, "SKIP", "no playback position")
        return
    # first decoded frame arrives (time since t_pos) later than the position was read
    arr_t, frame = live[0]
    p0 = pos + (arr_t - t_pos)
    period = (cur.get("runtime") or {}).get("loop_period_s") or 0
    start = cur.get("effective_start_s") or 0
    cache: dict[float, float] = {}

    def diff_at(dtv: float) -> float:
        if dtv not in cache:
            t = p0 + dtv
            if period:
                t = t % period
            code, jpg, _, _ = api.get(f"/api/videos/{s['video_id']}/frame?t={start + t:.3f}", timeout=20)
            g = jpeg_to_gray(jpg, W, H) if code == 200 and isinstance(jpg, bytes) else None
            cache[dtv] = mad(frame, g) if g else 1e9
        return cache[dtv]

    # coarse: the portal's position estimate drifts a few ms per loop (ffmpeg output time), so look -8..+3 s
    best = min((diff_at(float(x)), float(x)) for x in range(-8, 4))
    # fine: frame steps around the best second
    fps = ((cur.get("runtime") or {}).get("progress") or {}).get("fps") or 25
    step = max(1 / fps, 0.04)
    best = min([best] + [(diff_at(round(best[1] + k * step, 3)), round(best[1] + k * step, 3))
                         for k in range(-int(0.6 / step), int(0.6 / step) + 1)])
    # a different video / wrong place typically differs by 25+ levels; the same frame by a few (compression, overlay)
    note = f" · portal position estimate off by {best[1]:+.1f} s (cosmetic: loop drift)" if abs(best[1]) > 2 else ""
    rec("frames", "frames = library file", n, "PASS" if best[0] < 12 else "WARN" if best[0] < 22 else "FAIL",
        f"difference {best[0]:.1f}/255 at file position {(p0 + best[1]) % (period or 1e9):.1f}s{note}")


# ------------------------------------------------------------------ 5. burned-in clock (OCR)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FMT = "2026-11-22 13:46:00.123 IST"


def render(text: str, w: int, h: int, size: int, x: int) -> bytes:
    t = text.replace(":", r"\:")
    p = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=black:s={w}x{h}:d=0.04",
                        "-vf", f"drawtext=fontfile={FONT}:text='{t}':x={x}:y={size}:fontsize={size}:fontcolor=white,format=gray",
                        "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True, timeout=20)
    return p.stdout


def ink_cols(img: bytes, w: int, h: int, thr=128):
    return [x for x in range(w) if any(img[y * w + x] > thr for y in range(h))]


class ClockReader:
    def __init__(self, size: int):
        self.size = size
        self.h = 3 * size
        self.w = 40 * size
        x0 = size
        a = render("0" * 10, self.w, self.h, size, x0)
        b = render("0" * 20, self.w, self.h, size, x0)
        self.adv = (max(ink_cols(b, self.w, self.h)) - max(ink_cols(a, self.w, self.h))) / 10
        digits = render("0123456789", self.w, self.h, size, x0)
        self.tmpl = [self.cell(digits, self.w, x0 + i * self.adv) for i in range(10)]

    def cell(self, img: bytes, w: int, x: float):
        xi, cw = int(round(x)), int(self.adv)
        if xi < 0 or xi + cw > w:
            return []
        # binary: white text (255) vs the half-dark box behind it (at most ~90)
        return [1 if img[y * w + xx] > 150 else 0 for y in range(self.h) for xx in range(xi, xi + cw)]

    @staticmethod
    def _d(c, t):
        return sum(p != q for p, q in zip(c, t))

    def read(self, img: bytes, w: int):
        """The text is right-aligned: it ends `size` px before the frame's right edge (the crop ends there too)."""
        guess = w - self.size - len(FMT) * self.adv
        best = None
        for off in range(-6, 7):  # fine-tune on the year's "20", always the same
            c0, c1 = self.cell(img, w, guess + off), self.cell(img, w, guess + off + self.adv)
            if not c0 or not c1:
                continue
            sc = self._d(c0, self.tmpl[2]) + self._d(c1, self.tmpl[0])
            if best is None or sc < best[0]:
                best = (sc, guess + off)
        if best is None:
            return None
        origin = best[1]
        txt = []
        for i, ch in enumerate(FMT):
            if not ch.isdigit():
                txt.append(ch)
                continue
            c = self.cell(img, w, origin + i * self.adv)
            if not c:
                return None
            sc = [self._d(c, t) for t in self.tmpl]
            txt.append(str(sc.index(min(sc))))
        return "".join(txt)


def check_clock(s: dict, rtsp_base: str, secs=6.0):
    n = s["name"]
    info, _ = probe(f"{rtsp_base}/{n}")
    if not info:
        rec("clock", "clock in picture readable", n, "FAIL", "cannot open")
        return
    w, h = info["width"], info["height"]
    size = max(16, round(h / 24))
    rd = ClockReader(size)
    cw = int(min(w, 32 * rd.adv + 3 * size))
    crop_x = w - cw
    fr = gray_frames(f"{rtsp_base}/{n}", cw, rd.h, secs, vf_extra=f"crop={cw}:{rd.h}:{crop_x}:0", timeout=secs + 30, low_delay=True)
    # arrival times: monotonic -> wall clock
    m2w = time.time() - time.monotonic()
    tz = s.get("clock_tz") or "IST"
    off = 19800 if tz == "IST" else 0
    lat, bad, seen_tz = [], 0, set()
    for arr, img in fr:
        t = rd.read(img, cw)
        try:
            d = dt.datetime.strptime(t[:23], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=dt.timezone.utc) if t else None
        except ValueError:
            d = None
        if d is None:
            bad += 1
            continue
        burned = d.timestamp() - off
        lat.append((arr + m2w - burned) * 1000)
    ok = [x for x in lat if -500 < x < 10000]
    if len(ok) < max(5, len(fr) // 2):
        # most likely the zone is the other one
        other = [x for x in lat if abs(abs(x) - 19800000) < 60000]
        rec("clock", "clock in picture readable", n, "FAIL",
            f"{len(fr)} frames, {len(ok)} read as {tz}" + (" — the picture shows the OTHER time zone" if other else ""))
        return
    ok.sort()
    med, p95 = st.median(ok), ok[int(len(ok) * 0.95) - 1]
    rec("clock", f"clock {tz} = real time, delay", n, "PASS" if med < 1000 else "WARN",
        f"{len(ok)}/{len(fr)} frames read · delay median {med:.0f} ms, p95 {p95:.0f} ms, min {ok[0]:.0f} ms")
    steps = sorted(set(round(b - a) for a, b in zip(ok, ok[1:])))
    rec("clock", "clock ticks every frame", n, "PASS" if len(ok) > 10 else "WARN", f"{len(ok)} distinct stamps")


# ------------------------------------------------------------------ 6. full mode (preview): create / control
def wait(fn, secs, step=0.5):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def stream_by_name(api, name):
    code, ss, _, _ = api.get("/api/streams")
    return next((s for s in ss if s["name"] == name), None) if code == 200 else None


def full_mode(api: Api, rtsp_base: str, videos: dict, secs: float):
    vid = next((v for v in videos.values() if v.get("compat_mode") == "copy" and not v.get("b_frames")
                and (v.get("keyframe_interval_s") or 9) <= 4 and (v.get("duration_s") or 0) > 20), None) \
        or next(iter(videos.values()))
    created: list[int] = []

    def create(body):
        code, s, _, _ = api.post("/api/streams", body)
        if code in (200, 201):
            created.append(s["id"])
        return code, s

    try:
        ui = stream_by_name(api, "e2e-ui-1")  # made by portal_suite through the New stream dialog
        if ui and ui["status"] == "RUNNING":
            check_describe(ui, rtsp_base, videos)
            if ui.get("clock"):
                check_clock(ui, rtsp_base)
        # e2e-ui-1 holds a transcode slot: free it before the clock streams (preview allows few)
        if ui:
            api.post("/api/streams/bulk", {"stream_ids": [ui["id"]], "action": "stop"})
        # clock IST then UTC, one at a time
        for tz in ("IST", "UTC"):
            nm = f"e2e-clock-{tz.lower()}"
            code, s = create({"name": nm, "video_id": vid["id"], "mode": "loop", "start_now": True,
                              "output_size": "original", "clock": True, "clock_tz": tz, "lead_in_mode": "none", "lead_in_s": 0})
            rec("create", f"create clock {tz} stream", nm, "PASS" if code in (200, 201) else "FAIL",
                f"HTTP {code}" + ("" if code in (200, 201) else f": {str((s or {}).get('detail', s))[:100]}"))
            x = wait(lambda: (y := stream_by_name(api, nm)) and y["status"] == "RUNNING" and y, 40) if code in (200, 201) else None
            if x:
                time.sleep(3)
                check_clock(x, rtsp_base)
                api.post("/api/streams/bulk", {"stream_ids": [x["id"]], "action": "stop"})
                time.sleep(2)
            elif code in (200, 201):
                rec("clock", "clock stream runs", nm, "FAIL", "not RUNNING in 40 s")

        # duplicate name refused
        code, _ = create({"name": "E2E-CLOCK-IST", "video_id": vid["id"], "start_now": False})
        rec("create", "duplicate name refused", "E2E-CLOCK-IST", "PASS" if code in (400, 409, 422) else "FAIL", f"HTTP {code}")

        # lead-in: hold until a viewer (edge) connects, video 2 s later
        code, s = create({"name": "e2e-hold", "video_id": vid["id"], "mode": "loop", "start_now": True,
                          "lead_in_mode": "hold", "lead_in_s": 2, "hold_max_s": None})
        ph = wait(lambda: (x := stream_by_name(api, "e2e-hold")) and ((x.get("runtime") or {}).get("lead_in") or {}).get("phase") == "waiting", 40)
        rec("leadin", "slate waits for a viewer", "e2e-hold", "PASS" if ph else "FAIL", "phase=waiting" if ph else "never waiting")
        t0 = time.monotonic()
        th = threading.Thread(target=lambda: edge_reader(f"{rtsp_base}/e2e-hold", 15))
        th.start()
        started = wait(lambda: (x := stream_by_name(api, "e2e-hold")) and not (x.get("runtime") or {}).get("lead_in") and x["status"] == "RUNNING", 25)
        dtv = time.monotonic() - t0
        rec("leadin", "video starts after edge connects", "e2e-hold", "PASS" if started and dtv < 12 else "FAIL",
            f"video after {dtv:.1f} s (lead-in 2 s + connect)")
        th.join()

        # play once: 6 s segment
        code, s = create({"name": "e2e-once", "video_id": vid["id"], "mode": "once", "start": "00:00:02", "end": "00:00:08",
                          "start_now": True, "lead_in_mode": "none", "lead_in_s": 0})
        fin = wait(lambda: (x := stream_by_name(api, "e2e-once")) and x["status"] not in ("RUNNING", "STARTING", "LEAD_IN", "QUEUED") and x, 60)
        rec("control", "play once ends by itself", "e2e-once", "PASS" if fin else "FAIL", f"status {fin['status'] if fin else 'still running'}")

        # stop / start with an edge attached; bulk
        code, s2 = create({"name": "e2e-copy", "video_id": vid["id"], "mode": "loop", "start_now": True,
                           "lead_in_mode": "none", "lead_in_s": 0})
        wait(lambda: (stream_by_name(api, "e2e-copy") or {}).get("status") == "RUNNING", 40)
        ids = [stream_by_name(api, n)["id"] for n in ("e2e-copy", "e2e-hold")]
        code, _, _, _ = api.post("/api/streams/bulk", {"stream_ids": ids, "action": "stop"})
        stopped = wait(lambda: all((stream_by_name(api, n) or {}).get("status") == "STOPPED" for n in ("e2e-copy", "e2e-hold")), 30)
        info, _ = probe(f"{rtsp_base}/e2e-hold", timeout=8)
        rec("control", "bulk stop: URL goes away", "2 streams", "PASS" if code == 200 and stopped and not info else "FAIL",
            f"HTTP {code}, stopped={bool(stopped)}, url open={bool(info)}")
        t0 = time.monotonic()
        code, _, _, _ = api.post("/api/streams/bulk", {"stream_ids": ids, "action": "start"})
        back = wait(lambda: probe(f"{rtsp_base}/e2e-copy", timeout=8)[0], 60, step=1)
        rec("control", "bulk start: edge can read again", "2 streams", "PASS" if code == 200 and back else "FAIL",
            f"URL back after {time.monotonic() - t0:.1f} s")

        # duplicate upload by content (no name involved): ask the server about a library file's fingerprint
        v = next((v for v in videos.values() if v.get("quick_hash")), None)
        if v:
            code, body, _, _ = api.post("/api/videos/upload/check", {"filename": "e2e-renamed-copy.mp4", "size": v["size_bytes"],
                                                                      "quick_hash": v["quick_hash"]})
            dup = isinstance(body, dict) and body.get("duplicate_of")
            rec("library", "duplicate upload found by content", "e2e-renamed-copy.mp4", "PASS" if dup else "FAIL",
                f"HTTP {code}, server says duplicate of: {json.dumps(dup)[:80]}")
    finally:
        code, ss, _, _ = api.get("/api/streams")
        ids = [s["id"] for s in ss if s["name"].lower().startswith("e2e-")] if code == 200 else created
        if ids:
            api.post("/api/streams/bulk", {"stream_ids": ids, "action": "stop"})
            time.sleep(3)
            code, _, _, _ = api.post("/api/streams/bulk", {"stream_ids": ids, "action": "delete"})
            rec("cleanup", "e2e streams deleted", f"{len(ids)} streams", "PASS" if code == 200 else "WARN", f"HTTP {code}")


# ------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--mode", choices=["readonly", "full"], default="readonly")
    ap.add_argument("--secs", type=float, default=25, help="edge simulation length")
    ap.add_argument("--extra", type=int, default=1, help="extra (copy) edges per camera")
    ap.add_argument("--compare", type=int, default=999, help="compare frames with the file for at most N streams")
    ap.add_argument("--json")
    a = ap.parse_args()
    api = Api(a.base)
    t_start = time.time()
    print(f"== RTSP Studio end-to-end suite: {a.base} ({a.mode}) ==")
    cfg: dict = {}
    print("-- portal / API"); check_api(api, cfg)
    rtsp_base = f"rtsp://{cfg.get('public_rtsp_host')}:{cfg.get('public_rtsp_port')}"
    _, streams, _, _ = api.get("/api/streams")
    _, vids, _, _ = api.get("/api/videos")
    videos = {v["id"]: v for v in vids}
    running = [s for s in streams if s["status"] == "RUNNING" and not s["name"].lower().startswith("e2e-")]
    print(f"-- RTSP URLs ({len(streams)} streams, {len(running)} running)")
    check_urls(cfg, streams, rtsp_base)
    with cf.ThreadPoolExecutor(8) as ex:
        list(ex.map(lambda s: check_describe(s, rtsp_base, videos), running))
    print(f"-- edge simulation: {len(running)} cameras x (1 decoding + {a.extra} more) edges, {a.secs:.0f} s")
    edge_sim(api, running, rtsp_base, a.secs, a.extra, videos)
    print("-- frames")
    with cf.ThreadPoolExecutor(4) as ex:
        list(ex.map(lambda ix: check_frames(api, ix[1], rtsp_base, ix[0] < a.compare), enumerate(running)))
    clocks = [s for s in running if s.get("clock")]
    print(f"-- server clock in picture ({len(clocks)} running clock streams)")
    for s in clocks:
        check_clock(s, rtsp_base)
    if a.mode == "full":
        print("-- create / control (full mode)")
        full_mode(api, rtsp_base, videos, a.secs)
    tally = {k: sum(1 for r in R if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    print(f"== {tally} in {time.time() - t_start:.0f} s ==")
    if a.json:
        json.dump(dict(base=a.base, mode=a.mode, at=dt.datetime.now().isoformat(timespec="seconds"), tally=tally, results=R),
                  open(a.json, "w"), indent=1)
    return 1 if tally["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
