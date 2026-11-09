#!/usr/bin/env python3
"""Phase 1 spike: lead-in slate and hold-until-viewer with MediaMTX "always available".

Question: can a stream show a per-camera slate until (or for N s after) the
edge connects, then switch to the video, without the edge reconnecting and
without losing, repeating or reordering a frame?

Mechanism under test: MediaMTX path option ``alwaysAvailable`` with
``alwaysAvailableFile`` (a per-stream slate MP4). While no publisher is
connected the slate plays on repeat; readers stay attached across publisher
start, stop and crash. The backend would add one such path per stream through
the MediaMTX API and start the FFmpeg publisher (stream copy, unchanged) when
the lead-in is over.

    backend/.venv/bin/python scripts/spike_leadin.py [--out DIR]

Runs an isolated MediaMTX on free ports; touches nothing else. Writes
spike_leadin_<platform>.json and .md into --out. Exit code 0 = all PASS.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
import urllib.error
import urllib.request
from pathlib import Path

import psutil

REPO = Path(__file__).resolve().parents[1]
X264 = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-sc_threshold", "0"]
DECODE_ERR = re.compile(r"(error while decoding|concealing|corrupt|missing picture|non-existing PPS|"
                        r"no frame|decode_slice_header|Invalid NAL)", re.I)
LEAD_IN_S = 5.0


# ------------------------------------------------------------------ helpers
def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *args], check=True)


def has_filter(name: str) -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return re.search(rf"\s{name}\s", out) is not None


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate,profile,level", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return {"width": s["width"], "height": s["height"], "fps": int(num) / int(den), "fps_text": s["r_frame_rate"],
            "profile": s.get("profile"), "level": s.get("level")}


def keyframes(path: Path) -> list[float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
                        "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True, check=True)
    return [float(x) for x in r.stdout.replace(",", " ").split() if x.strip()]


def parse_framemd5(text: str) -> tuple[list[tuple[int, str]], float]:
    tb, out = 1.0, []
    for ln in text.splitlines():
        m = re.match(r"#tb 0: (\d+)/(\d+)", ln)
        if m:
            tb = int(m.group(1)) / int(m.group(2))
        if ln.startswith("#") or not ln.strip():
            continue
        p = [x.strip() for x in ln.split(",")]
        out.append((int(p[2]), p[5]))  # pts, md5
    return out, tb


def decode_md5s(args: list[str]) -> list[str]:
    r = subprocess.run(["ffmpeg", "-v", "error", *args, "-f", "framemd5", "-"], capture_output=True, text=True,
                       check=True)
    return [m for _, m in parse_framemd5(r.stdout)[0]]


# ------------------------------------------------------------------ media
def make_source(work: Path, key: str) -> Path:
    specs = {
        "h264_1080p25_gop2": ("testsrc2=size=1920x1080:rate=25", ["-g", "50", "-keyint_min", "50"]),
        "h264_720p2997_bframes": ("testsrc2=size=1280x720:rate=30000/1001", ["-g", "60", "-keyint_min", "60", "-bf", "3"]),
        "h264_1080p25_gop10": ("testsrc2=size=1920x1080:rate=25", ["-g", "250", "-keyint_min", "250"]),
        "hevc_1080p25": ("testsrc2=size=1920x1080:rate=25", ["-x265-params", "keyint=50:min-keyint=50:log-level=error"]),
    }
    out = work / f"{key}.mp4"
    if not out.exists():
        src, extra = specs[key]
        codec = ["-c:v", "libx265", "-preset", "veryfast", "-pix_fmt", "yuv420p"] if key.startswith("hevc") \
            else [*X264, "-profile:v", "high"]
        ff("-f", "lavfi", "-i", src, "-t", "40", *codec, *extra, str(out))
    return out


def make_slate(work: Path, name: str, width: int, height: int, fps_text: str, seconds: int = 10) -> tuple[Path, str]:
    """Per-stream slate: black, unique per camera, changes every frame. Matches the source size and rate."""
    out = work / f"slate_{name}_{width}x{height}.mp4"
    if has_filter("drawtext"):
        style = "drawtext"
        vf = (f"color=c=black:size={width}x{height}:rate={fps_text},"
              f"drawtext=text='{name}':fontsize={height // 12}:fontcolor=white:x=(w-tw)/2:y=h/2-th,"
              f"drawtext=text='waiting for video  %{{pts\\:hms}}':fontsize={height // 24}:fontcolor=gray:"
              f"x=(w-tw)/2:y=h/2+th")
    else:  # no FreeType in this FFmpeg build: colour band from the name + moving counter
        style = "band+counter"
        h = sum(ord(c) * (i + 1) for i, c in enumerate(name))
        color = f"0x{(h * 2654435761) & 0xFFFFFF:06x}"
        tw, th = max(160, width // 4 // 2 * 2), max(120, height // 3 // 2 * 2)
        vf = (f"color=c=black:size={width}x{height}:rate={fps_text}[bg];testsrc=size={tw}x{th}:rate={fps_text}[t];"
              f"[bg]drawbox=x=0:y={height - height // 10}:w={width}:h={height // 18}:color={color}:t=fill[b];"
              f"[b][t]overlay=x=(W-w)/2:y=(H-h)/2")
    if not out.exists():
        fps = float(Fraction(fps_text))
        g = max(1, round(fps))
        ff("-f", "lavfi", "-i", vf, "-t", str(seconds), *X264, "-profile:v", "high", "-g", str(g),
           "-keyint_min", str(g), str(out))
    return out, style


def write_playlist(work: Path, src: Path, start_kf: float, name: str) -> Path:
    """Same shape as the product's ffconcat (keyframe inpoint + 0.5 ms), two loop entries."""
    pl = work / f"{name}.ffconcat"
    inpoint = start_kf + 0.0005 if start_kf > 0 else 0.0
    entry = f"file '{src}'\ninpoint {inpoint:.6f}\n"
    pl.write_text("ffconcat version 1.0\n" + entry * 2)
    return pl


def product_transcode_args(info: dict) -> list[str]:
    """The product's own libx264 settings (services/ffmpeg_manager.transcode_args), with default settings."""
    sys.path.insert(0, str(REPO / "backend"))
    from types import SimpleNamespace

    from app.config import Settings
    from app.services.ffmpeg_manager import transcode_args
    plan = SimpleNamespace(fps=info["fps"], width=info["width"], height=info["height"])
    return transcode_args(plan, Settings(_env_file=None))


def publisher(pl: Path, url: str, log: Path, codec: list[str] | None = None) -> subprocess.Popen:
    """The product's publisher command (services/ffmpeg_manager.build_command): copy, or `codec` to transcode."""
    return subprocess.Popen(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning", "-re", "-f", "concat",
                             "-safe", "0", "-i", str(pl), "-map", "0:v:0", "-an", "-sn", "-dn",
                             *(codec or ["-c:v", "copy"]), "-f", "rtsp", "-rtsp_transport", "tcp", url],
                            stdout=subprocess.DEVNULL, stderr=open(log, "w"))


# ------------------------------------------------------------------ MediaMTX
class Mtx:
    def __init__(self, work: Path):
        self.work = work
        self.rtsp, self.api, self.metrics, self.hls = free_port(), free_port(), free_port(), free_port()
        text = (REPO / "deployment" / "mediamtx.yml").read_text()
        text = text.replace("rtspAddress: :8554", f"rtspAddress: 127.0.0.1:{self.rtsp}")
        text = text.replace("127.0.0.1:9997", f"127.0.0.1:{self.api}")
        text = text.replace("127.0.0.1:9998", f"127.0.0.1:{self.metrics}")
        text = text.replace("127.0.0.1:8888", f"127.0.0.1:{self.hls}")
        self.cfg = work / "mediamtx.yml"
        self.cfg.write_text(text)
        self.log = work / "mediamtx.log"
        self.log.write_text("")  # session counts are read from this run's log only
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(["mediamtx", str(self.cfg)], stdout=open(self.log, "a"),
                                     stderr=subprocess.STDOUT, cwd=self.work)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                self.call("GET", "/v3/paths/list")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("MediaMTX did not start")

    def stop(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(10)

    def call(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.api}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read()
                return r.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def add_slate_path(self, name: str, slate: Path) -> None:
        # runOnDemand (the deployment's "hold early readers" command) can't be combined with alwaysAvailable
        st, body = self.call("POST", f"/v3/config/paths/add/{name}", {
            "alwaysAvailable": True, "alwaysAvailableFile": str(slate), "runOnDemand": "", "runOnUnDemand": ""})
        if st != 200:
            raise RuntimeError(f"add path {name}: {st} {body}")

    def path(self, name: str) -> dict | None:
        st, body = self.call("GET", f"/v3/paths/get/{name}")
        return body if st == 200 else None

    def url(self, name: str) -> str:
        return f"rtsp://127.0.0.1:{self.rtsp}/{name}"

    def reader_sessions(self, name: str) -> int:
        """How many RTSP sessions were ever created to READ this path (from the log)."""
        text = self.log.read_text()
        return len(re.findall(rf"is reading from path '{re.escape(name)}'", text))


# ------------------------------------------------------------------ readers
class FFReader:
    """ffmpeg RTSP reader that decodes every frame (framemd5) and logs decoder warnings."""

    def __init__(self, url: str, seconds: float, out: Path):
        self.out, self.log = out, out.with_suffix(".log")
        self.proc = subprocess.Popen(["ffmpeg", "-nostdin", "-y", "-v", "warning", "-rtsp_transport", "tcp", "-i", url,
                                      "-t", str(seconds), "-f", "framemd5", str(out)],
                                     stdout=subprocess.DEVNULL, stderr=open(self.log, "w"))

    def wait(self, timeout: float = 120) -> None:
        self.proc.wait(timeout)

    def frames(self) -> tuple[list[tuple[int, str]], float]:
        return parse_framemd5(self.out.read_text()) if self.out.exists() else ([], 1.0)

    def decode_errors(self) -> list[str]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [ln for ln in lines if DECODE_ERR.search(ln)]


class GstReader:
    """GStreamer reader (rtspsrc -> avdec_h264): the kind of pipeline many edges use."""

    available = shutil.which("gst-launch-1.0") is not None and subprocess.run(
        ["gst-inspect-1.0", "rtspsrc"], capture_output=True).returncode == 0

    def __init__(self, url: str, seconds: float, out: Path):
        self.log = out
        pipe = (f"rtspsrc location={url} protocols=tcp latency=200 ! rtph264depay ! h264parse ! avdec_h264 ! "
                f"videoconvert ! fakesink sync=false silent=false")
        self.proc = subprocess.Popen(["timeout", "-s", "INT", str(int(seconds)), "gst-launch-1.0", "-e", "-v",
                                      *pipe.split()],
                                     stdout=open(out, "w"), stderr=subprocess.STDOUT)

    def wait(self, timeout: float = 120) -> None:
        self.proc.wait(timeout)

    def result(self) -> dict:
        text = self.log.read_text(errors="replace")
        return {"frames": len(re.findall(r"fakesink0.*chain", text)),
                "errors": [ln for ln in text.splitlines() if re.search(r"ERROR|WARNING", ln)][:5],
                "caps_changes": len(set(re.findall(r"width=\(int\)(\d+), height=\(int\)(\d+)", text)))}


# ------------------------------------------------------------------ analysis
def classify(frames: list[tuple[int, str]], src_md5: dict[str, int], slate_md5: set[str]) -> list[tuple[str, int | None, int]]:
    seq = []
    for pts, m in frames:
        if m in src_md5:
            seq.append(("V", src_md5[m], pts))
        elif m in slate_md5:
            seq.append(("S", None, pts))
        else:
            seq.append(("?", None, pts))
    return seq


def summarize(seq, tb: float, fps: float) -> dict:
    segs = []
    for kind, idx, pts in seq:
        if not segs or segs[-1]["kind"] != kind:
            segs.append({"kind": kind, "frames": 0, "first_src_frame": idx})
        segs[-1]["frames"] += 1
    skipped = repeated = 0
    for a, b in zip(seq, seq[1:]):
        if a[0] == b[0] == "V" and a[1] is not None and b[1] is not None:  # None = transcoded frame
            d = b[1] - a[1]
            skipped += max(0, d - 1)
            repeated += 1 if d < 1 else 0
    frame_ticks = 1 / fps / tb
    back = sum(1 for a, b in zip(seq, seq[1:]) if b[2] <= a[2])
    gaps = [round((b[2] - a[2]) / frame_ticks, 2) for a, b in zip(seq, seq[1:]) if (b[2] - a[2]) > 1.5 * frame_ticks]
    return {"segments": [f"{s['kind']}x{s['frames']}" + (f"(from src frame {s['first_src_frame']})"
                                                        if s["kind"] == "V" else "") for s in segs],
            "segment_list": segs, "unknown_frames": sum(1 for x in seq if x[0] == "?"),
            "video_frames_skipped": skipped, "video_frames_repeated": repeated,
            "timestamps_not_increasing": back, "gaps_over_1.5_frames": gaps}


# ------------------------------------------------------------------ tests
class Spike:
    def __init__(self, out: Path, work: Path):
        self.out = out
        self.work = work
        self.work.mkdir(parents=True, exist_ok=True)
        self.mtx = Mtx(self.work)
        self.results: list[dict] = []
        self.n = 0

    def name(self, base: str) -> str:
        self.n += 1
        return f"{base}-{self.n}"

    def record(self, test: str, case: str, ok: bool | None, **details) -> None:
        verdict = details.pop("verdict", None) or ("PASS" if ok else ("NOT RUN" if ok is None else "FAIL"))
        self.results.append({"test": test, "case": case, "verdict": verdict, **details})
        print(f"[{verdict}] {test} / {case}: " + json.dumps({k: v for k, v in details.items()
                                                            if k not in ("segment_list",)})[:600], flush=True)

    def source_case(self, key: str):
        src = make_source(self.work, key)
        info = probe(src)
        kfs = keyframes(src)
        start_kf = next((k for k in kfs if k >= 4.0), kfs[0])  # configured start: first keyframe at/after 4 s
        return src, info, start_kf

    def expected(self, src: Path, pl: Path) -> tuple[dict[str, int], str]:
        src_list = decode_md5s(["-i", str(src)])
        first = decode_md5s(["-f", "concat", "-safe", "0", "-i", str(pl), "-frames:v", "1"])[0]
        return {m: i for i, m in enumerate(src_list)}, first

    # T1-T3: reader connected during the slate stays attached; switch is clean; video starts at the start frame
    def t_switch(self, key: str, slate_size: tuple[int, int] | None = None) -> None:
        transcode = key.startswith("hevc")  # H.265 sources are transcoded to H.264 by the product
        src, info, start_kf = self.source_case(key)
        name = self.name("sw")
        w, h = slate_size or (info["width"], info["height"])
        slate, style = make_slate(self.work, name, w, h, info["fps_text"])
        pl = write_playlist(self.work, src, start_kf, name)
        src_md5, first_expected = self.expected(src, pl)
        slate_md5 = set(decode_md5s(["-i", str(slate)]))
        self.mtx.add_slate_path(name, slate)
        rd = FFReader(self.mtx.url(name), 22, self.work / f"{name}.framemd5")
        gst = GstReader(self.mtx.url(name), 22, self.work / f"{name}.gst.log") if GstReader.available else None
        time.sleep(LEAD_IN_S)
        pub = publisher(pl, self.mtx.url(name), self.work / f"{name}.pub.log",
                        product_transcode_args(info) if transcode else None)
        rd.wait()
        if gst:
            gst.wait()
        pub.terminate()
        pub.wait(10)
        frames, tb = rd.frames()
        seq = classify(frames, src_md5, slate_md5)
        if transcode:  # re-encoded frames can't match the source checksums: every non-slate frame is video
            seq = [("V" if k == "?" else k, i, pts) for k, i, pts in seq]
        s = summarize(seq, tb, info["fps"])
        v = next((x for x in s["segment_list"] if x["kind"] == "V"), None)
        first_ok = bool(v) and decode_first_matches(v, src_md5, first_expected)
        sessions = self.mtx.reader_sessions(name)
        expected_sessions = 2 if gst else 1
        errors = rd.decode_errors()
        case = key + (f" + slate {w}x{h}" if slate_size else "")
        kinds = [x["kind"] for x in s["segment_list"]]
        self.record("T1 reader stays connected", case, sessions == expected_sessions,
                    reader_sessions_created=sessions, readers=expected_sessions)
        pause_ms = [round(g * 1000 / info["fps"]) for g in s["gaps_over_1.5_frames"]]
        clean = (kinds == ["S", "V"] and s["video_frames_skipped"] == 0 and s["video_frames_repeated"] == 0
                 and s["timestamps_not_increasing"] == 0 and s["unknown_frames"] == 0 and not errors
                 and all(ms <= 500 for ms in pause_ms))
        extra = {"verdict": "INFO"} if slate_size else {}  # a different-size slate is avoided by design
        if transcode:  # frame identity can't be checked after re-encoding (skip/repeat counts don't apply)
            s["video_frames_skipped"] = s["video_frames_repeated"] = 0
        self.record("T2 clean switch", case, clean, **extra, pause_at_switch_ms=pause_ms,
                    slate_style=style, segments=s["segments"], skipped=s["video_frames_skipped"],
                    repeated=s["video_frames_repeated"], ts_not_increasing=s["timestamps_not_increasing"],
                    gaps=s["gaps_over_1.5_frames"], unknown=s["unknown_frames"], decode_errors=errors[:3])
        if transcode:
            self.record("T3 starts at the configured frame", case + " (transcoded)", None,
                        reason="re-encoded frames can't be matched to the source by checksum")
        else:
            self.record("T3 starts at the configured frame", case, first_ok, **extra,
                        start_keyframe_s=round(start_kf, 3),
                        first_video_src_frame=v["first_src_frame"] if v else None)
        if gst:
            g = gst.result()
            self.record("T1/T2 GStreamer reader", case, g["frames"] > 0 and not g["errors"], **g)
        else:
            self.record("T1/T2 GStreamer reader", case, None, reason="GStreamer RTSP plugins not installed here")

    # T4: hold until the first viewer, then LEAD_IN_S, then the video
    def t_hold(self) -> None:
        key = "h264_1080p25_gop2"
        src, info, start_kf = self.source_case(key)
        name = self.name("hold")
        slate, _ = make_slate(self.work, name, info["width"], info["height"], info["fps_text"])
        pl = write_playlist(self.work, src, start_kf, name)
        src_md5, first_expected = self.expected(src, pl)
        slate_md5 = set(decode_md5s(["-i", str(slate)]))
        self.mtx.add_slate_path(name, slate)
        # nothing published while nobody watches
        time.sleep(3)
        idle = self.mtx.path(name) or {}
        idle_publisher = bool(idle.get("online")) or idle.get("source") is not None
        rd = FFReader(self.mtx.url(name), 16, self.work / f"{name}.framemd5")
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:  # what the backend monitor would do (it polls every 1 s)
            p = self.mtx.path(name)
            if p and p.get("readers"):
                break
            time.sleep(0.25)
        detected = time.monotonic() - t0
        t_detect = time.monotonic()
        time.sleep(LEAD_IN_S)
        pub = publisher(pl, self.mtx.url(name), self.work / f"{name}.pub.log")
        while time.monotonic() - t_detect < LEAD_IN_S + 10 and not (self.mtx.path(name) or {}).get("online"):
            time.sleep(0.05)
        video_after_s = time.monotonic() - t_detect
        rd.wait()
        pub.terminate()
        pub.wait(10)
        frames, tb = rd.frames()
        s = summarize(classify(frames, src_md5, slate_md5), tb, info["fps"])
        segs = s["segment_list"]
        slate_frames = segs[0]["frames"] if segs and segs[0]["kind"] == "S" else 0
        v = next((x for x in segs if x["kind"] == "V"), None)
        ok = (not idle_publisher and [x["kind"] for x in segs] == ["S", "V"]
              and LEAD_IN_S - 0.5 <= video_after_s <= LEAD_IN_S + 0.5
              and decode_first_matches(v, src_md5, first_expected) and s["video_frames_skipped"] == 0)
        self.record("T4 hold until viewer", key, ok, reader_detected_after_s=round(detected, 2),
                    video_online_after_detect_s=round(video_after_s, 2), lead_in_s=LEAD_IN_S,
                    publisher_before_viewer=idle_publisher, slate_frames_decoded_by_reader=slate_frames,
                    segments=s["segments"])

    # T5: lead-in frames differ between cameras (the edge must not drop them as duplicates)
    def t_unique(self) -> None:
        names = [self.name("cam"), self.name("cam")]
        md5s = {}
        for n in names:
            slate, style = make_slate(self.work, n, 1920, 1080, "25")
            self.mtx.add_slate_path(n, slate)
        readers = {n: FFReader(self.mtx.url(n), 6, self.work / f"{n}.framemd5") for n in names}
        for n, r in readers.items():
            r.wait()
            md5s[n] = [m for _, m in r.frames()[0]]
        a, b = (md5s[n] for n in names)
        common = len(set(a) & set(b))
        repeats_a = sum(1 for x, y in zip(a, a[1:]) if x == y)
        self.record("T5 lead-in frames unique per camera", f"slate style {style}", common == 0 and len(a) > 50,
                    frames_a=len(a), frames_b=len(b), identical_frames_across_cameras=common,
                    consecutive_identical_frames_in_one_slate=repeats_a)

    # T6: publisher crash -> slate -> publisher back, reader never reconnects
    def t_crash(self) -> None:
        key = "h264_1080p25_gop2"
        src, info, start_kf = self.source_case(key)
        name = self.name("crash")
        slate, _ = make_slate(self.work, name, info["width"], info["height"], info["fps_text"])
        pl = write_playlist(self.work, src, start_kf, name)
        src_md5, first_expected = self.expected(src, pl)
        slate_md5 = set(decode_md5s(["-i", str(slate)]))
        self.mtx.add_slate_path(name, slate)
        rd = FFReader(self.mtx.url(name), 26, self.work / f"{name}.framemd5")
        time.sleep(2)
        pub = publisher(pl, self.mtx.url(name), self.work / f"{name}.pub.log")
        time.sleep(8)
        pub.send_signal(signal.SIGKILL)  # crash, no RTSP teardown
        pub.wait()
        time.sleep(5)
        pub = publisher(pl, self.mtx.url(name), self.work / f"{name}.pub2.log")
        rd.wait()
        pub.terminate()
        pub.wait(10)
        frames, tb = rd.frames()
        s = summarize(classify(frames, src_md5, slate_md5), tb, info["fps"])
        kinds = [x["kind"] for x in s["segment_list"]]
        vs = [x for x in s["segment_list"] if x["kind"] == "V"]
        ok = (kinds == ["S", "V", "S", "V"] and self.mtx.reader_sessions(name) == 1
              and all(decode_first_matches(v, src_md5, first_expected) for v in vs)
              and s["timestamps_not_increasing"] == 0 and not rd.decode_errors())
        self.record("T6 publisher crash keeps the reader", key, ok, segments=s["segments"],
                    pauses_ms=[round(g * 1000 / info["fps"]) for g in s["gaps_over_1.5_frames"]],
                    reader_sessions_created=self.mtx.reader_sessions(name),
                    ts_not_increasing=s["timestamps_not_increasing"], decode_errors=rd.decode_errors()[:3])

    # T7: MediaMTX restart forgets API-added paths; re-adding them restores the slate
    def t_mtx_restart(self) -> None:
        name = self.name("mtx")
        slate, _ = make_slate(self.work, name, 1280, 720, "25")
        self.mtx.add_slate_path(name, slate)
        self.mtx.stop()
        self.mtx.start()
        forgotten = self.mtx.call("GET", f"/v3/config/paths/get/{name}")[0] == 404
        self.mtx.add_slate_path(name, slate)
        rd = FFReader(self.mtx.url(name), 4, self.work / f"{name}.framemd5")
        rd.wait()
        slate_md5 = set(decode_md5s(["-i", str(slate)]))
        frames = rd.frames()[0]
        ok = forgotten and len(frames) > 50 and all(m in slate_md5 for _, m in frames)
        self.record("T7 MediaMTX restart", "re-add path config on MediaMTX up", ok,
                    config_lost_on_restart=forgotten, frames_after_readd=len(frames))

    # T8: cost of streams waiting on their slate
    def t_cost(self, n: int = 20) -> None:
        names = [self.name("cost") for _ in range(n)]
        slate, _ = make_slate(self.work, "cost", 1920, 1080, "25")
        proc = psutil.Process(self.mtx.proc.pid)

        def sample(seconds=10.0):
            proc.cpu_percent(None)
            time.sleep(seconds)
            return round(proc.cpu_percent(None), 1), round(proc.memory_info().rss / 1e6, 1)

        base = sample(5)
        for nm in names:
            self.mtx.add_slate_path(nm, slate)
        idle = sample()
        readers = [subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-rtsp_transport", "tcp", "-i",
                                     self.mtx.url(nm), "-t", "25", "-c", "copy", "-f", "null", "-"]) for nm in names]
        time.sleep(3)
        watched = sample()
        for r in readers:
            r.wait(60)
        self.record("T8 cost of waiting streams", f"{n} streams, 1080p25 slate", True,
                    mediamtx_cpu_pct_and_rss_mb={"no_paths": base, f"{n}_paths_no_readers": idle,
                                                 f"{n}_paths_{n}_readers": watched},
                    cpu_count=psutil.cpu_count())

    def run(self) -> int:
        self.mtx.start()
        try:
            for key in ("h264_1080p25_gop2", "h264_720p2997_bframes", "h264_1080p25_gop10", "hevc_1080p25"):
                self.t_switch(key)
            self.t_switch("h264_1080p25_gop2", slate_size=(1280, 720))  # slate at a different resolution
            self.t_hold()
            self.t_unique()
            self.t_crash()
            self.t_mtx_restart()
            self.t_cost()
        finally:
            self.mtx.stop()
        return self.report()

    def report(self) -> int:
        ff_ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
        mtx_ver = subprocess.run(["mediamtx", "--version"], capture_output=True, text=True).stdout.strip()
        meta = {"platform": f"{platform.system()} {platform.release()} {platform.machine()}", "ffmpeg": ff_ver,
                "mediamtx": mtx_ver, "gstreamer": GstReader.available, "drawtext": has_filter("drawtext"),
                "date": time.strftime("%Y-%m-%d %H:%M")}
        tag = platform.system().lower()
        (self.out / f"spike_leadin_{tag}.json").write_text(json.dumps({"meta": meta, "results": self.results},
                                                                      indent=1, default=str))
        lines = [f"# Lead-in spike results ({meta['platform']})", "",
                 f"{meta['ffmpeg']} · MediaMTX {meta['mediamtx']} · GStreamer RTSP: {meta['gstreamer']} · "
                 f"drawtext: {meta['drawtext']} · {meta['date']}", "",
                 "| Test | Case | Verdict | Details |", "|---|---|---|---|"]
        for r in self.results:
            d = {k: v for k, v in r.items() if k not in ("test", "case", "verdict", "segment_list")}
            lines.append(f"| {r['test']} | {r['case']} | **{r['verdict']}** | `{json.dumps(d)[:400]}` |")
        (self.out / f"spike_leadin_{tag}.md").write_text("\n".join(lines) + "\n")
        failed = [r for r in self.results if r["verdict"] == "FAIL"]
        print(f"\n{len(self.results) - len(failed)}/{len(self.results)} not failed; report in {self.out}")
        return 1 if failed else 0


def decode_first_matches(v: dict | None, src_md5: dict[str, int], first_expected: str) -> bool:
    return bool(v) and v["first_src_frame"] == src_md5.get(first_expected)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "reports" / "spike_leadin", help="reports")
    ap.add_argument("--work", type=Path, default=None, help="generated videos and logs (default: a temp folder)")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    sys.exit(Spike(a.out, a.work or Path(tempfile.mkdtemp(prefix="spike_leadin_"))).run())
