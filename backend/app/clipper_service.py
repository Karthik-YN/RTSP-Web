"""Video clipper as a side service, so the clipper works without restarting the main app.

It runs in its own container next to the main app (``docker-compose.clipper.yml``), shares the
library volume, and:

* proxies every request to the main app (``RTSPM_CLIPPER_UPSTREAM``), so the whole dashboard works
  through it and the clipper page (/clip/<id>) can create streams, read the library, etc.;
* adds ``clipper`` to ``/api/config`` features and answers the clip endpoints itself:
  ``GET /api/videos/{id}/clip-name``, ``POST /api/videos/{id}/clip``; its jobs are merged into
  ``GET /api/videos/conversions`` and cancelled through ``DELETE /api/videos/conversions/{id}``;
* writes the finished clip into the library folder (never overwriting) and asks the main app to
  pick it up with its normal ``POST /api/videos/refresh``.

The main app is never restarted or changed. Once it runs a version with the built-in clipper
(``clipper`` in its own features), this service is no longer needed.

Run: ``uvicorn app.clipper_service:app --host 0.0.0.0 --port 8083`` (from backend/).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from .database import utcnow
from .services import naming
from .services.converter import MAX_CLIP_SEGMENTS, MIN_SEGMENT_S, Converter, Job

log = logging.getLogger("clipper")

UPSTREAM = os.environ.get("RTSPM_CLIPPER_UPSTREAM", "http://127.0.0.1:8081").rstrip("/")
VIDEO_DIR = Path(os.environ.get("RTSPM_VIDEO_DIR", "/data/videos"))
TMP_DIR = Path(os.environ.get("RTSPM_CLIPPER_TMP", "/data/state/clipper-tmp"))  # same volume: os.link works
SETTINGS = SimpleNamespace(
    ffmpeg_bin=os.environ.get("RTSPM_FFMPEG_BIN", "ffmpeg"),
    convert_preset=os.environ.get("RTSPM_CONVERT_PRESET", "veryfast"),
    convert_crf=int(os.environ.get("RTSPM_CONVERT_CRF", "20")),
    x264_gop_seconds=float(os.environ.get("RTSPM_X264_GOP_SECONDS", "2")),
    convert_threads=int(os.environ.get("RTSPM_CLIPPER_THREADS", "4")),  # keep CPU for the live streams
)
MAX_PARALLEL = int(os.environ.get("RTSPM_CLIPPER_PARALLEL", "1"))
HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
       "proxy-authenticate", "host", "content-length"}

app = FastAPI(title="RTSP Studio clipper", docs_url=None, redoc_url=None, openapi_url=None)
# the main dashboard (another port) asks /clipper/ping whether this service is there
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])

client = httpx.AsyncClient(base_url=UPSTREAM, timeout=httpx.Timeout(60.0, read=None))
tool = Converter.__new__(Converter)  # only its pure helpers: clip_name, clip_command
tool.s = SETTINGS
jobs: dict[str, Job] = {}
procs: dict[str, asyncio.subprocess.Process] = {}
sem = asyncio.Semaphore(MAX_PARALLEL)


def _fwd_headers(request: Request) -> dict[str, str]:
    h = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    h.setdefault("accept-encoding", "identity")  # only what the client asked for (bytes pass through raw)
    h["x-forwarded-for"] = request.client.host if request.client else ""
    return h


async def upstream_json(method: str, path: str, request: Request | None = None, **kw):
    r = await client.request(method, path, headers=_fwd_headers(request) if request else None, **kw)
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail") if r.headers.get("content-type", "").startswith(
            "application/json") else r.text)
    return r.json()


# ------------------------------------------------------------------ own endpoints
@app.get("/clipper/ping")
async def ping():
    return {"ok": True, "service": "clipper", "jobs": sum(j.active for j in jobs.values())}


@app.get("/api/config")
async def config(request: Request):
    cfg = await upstream_json("GET", "/api/config", request)
    feats = cfg.setdefault("features", [])
    if "clipper" not in feats:
        feats.append("clipper")
    return cfg


async def _names_and_codes(request: Request) -> tuple[list[str], set[str] | None]:
    videos = await upstream_json("GET", "/api/videos", request)
    names = [v["filename"] for v in videos] + [PurePosixPath(j.target).name for j in jobs.values() if j.active]
    codes = None
    with contextlib.suppress(Exception):
        conv = await upstream_json("GET", "/api/naming", request)
        if conv.get("mode") == "enforce":
            codes = {c["code"] for c in conv.get("codes", [])}
    return names, codes


@app.get("/api/videos/{video_id}/clip-name")
async def clip_name(video_id: int, request: Request):
    v = await upstream_json("GET", f"/api/videos/{video_id}", request)
    names, codes = await _names_and_codes(request)
    try:
        return {"name": tool.clip_name(SimpleNamespace(rel_path=v["rel_path"]), names, codes)}
    except ValueError as exc:
        raise HTTPException(409, str(exc))


class ClipIn(BaseModel):
    segments: list[tuple[float, float]]
    name: str


def _err(status: int, message: str):
    return JSONResponse({"detail": {"message": message}}, status_code=status)


@app.post("/api/videos/{video_id}/clip", status_code=202)
async def clip(video_id: int, body: ClipIn, request: Request):
    v = await upstream_json("GET", f"/api/videos/{video_id}", request)
    if v.get("status") != "READY" or v.get("compat_mode") == "reject":
        return _err(409, f"'{v['filename']}' can't be clipped: it is not a readable video.")
    segs = body.segments
    if not segs:
        return _err(422, "Mark at least one segment.")
    if len(segs) > MAX_CLIP_SEGMENTS:
        return _err(422, f"At most {MAX_CLIP_SEGMENTS} segments per clip.")
    dur = v.get("duration_s") or 0
    clean = []
    for i, (a, b) in enumerate(segs, 1):
        a, b = sorted((float(a), float(b)))
        a, b = max(0.0, a), (min(b, dur) if dur > 0 else b)
        if b - a < MIN_SEGMENT_S:
            return _err(422, f"Segment {i} is shorter than {MIN_SEGMENT_S:g} s.")
        clean.append((round(a, 3), round(b, 3)))
    name = body.name.strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return _err(422, "Give the clip a file name.")
    if not name.lower().endswith(".mp4"):
        name = PurePosixPath(name).stem + ".mp4" if PurePosixPath(name).suffix else name + ".mp4"
    names, codes = await _names_and_codes(request)
    if codes is not None:
        chk = naming.check(name, codes)
        if not chk.ok:
            hint = None
            with contextlib.suppress(Exception):
                hint = tool.clip_name(SimpleNamespace(rel_path=v["rel_path"]), names, codes)
            return _err(422, f"'{name}' does not follow the naming convention WHAT_WHERE_NN: " + " ".join(chk.errors)
                        + (f" Suggested: {hint}" if hint else ""))
    parent = PurePosixPath(v["rel_path"]).parent
    rel = str(parent / name) if str(parent) not in ("", ".") else name
    if name in names or (VIDEO_DIR / rel).exists():
        return _err(409, f"'{name}' already exists in the library: pick another name.")
    src = (VIDEO_DIR / v["rel_path"]).resolve()
    if not src.is_relative_to(VIDEO_DIR.resolve()) or not src.is_file():
        return _err(404, "The video file is missing on the server.")
    job = Job(uuid.uuid4().hex[:12], video_id, v["rel_path"], rel, None, "clip", None, clean)
    jobs[job.id] = job
    job.task = asyncio.create_task(_run(job, SimpleNamespace(fps=v.get("fps")), src))
    return job.view()


async def _run(job: Job, v, src: Path) -> None:
    tmp = TMP_DIR / f"clip-{job.id}.mp4"
    try:
        async with sem:
            if job.state == "cancelled":
                return
            job.state, job.started_mono = "running", time.monotonic()
            TMP_DIR.mkdir(parents=True, exist_ok=True)
            cmd = tool.clip_command(v, src, tmp, job.segments)
            proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE,
                                                        preexec_fn=lambda: os.nice(10))
            procs[job.id] = proc
            total = sum(b - a for a, b in job.segments) or 1
            err_task = asyncio.create_task(proc.stderr.read())
            async for raw in proc.stdout:
                line = raw.decode(errors="replace").strip()
                if line.startswith("out_time_us="):
                    with contextlib.suppress(ValueError):
                        job.progress = min(0.97, max(0.0, int(line.split("=", 1)[1]) / 1e6 / total))
            rc = await proc.wait()
            stderr = (await err_task).decode(errors="replace")
            if job.state == "cancelled":
                return
            if rc != 0:
                last = " ".join(ln for ln in stderr.strip().splitlines()[-3:]).replace(str(src), "source")
                raise RuntimeError("Clip failed: " + last[:300])
            old = time.time() - 120  # finished: don't let the library think it is still being copied
            os.utime(tmp, (old, old))
            final = VIDEO_DIR / job.target
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(tmp, final)  # never overwrites
            except FileExistsError:
                raise RuntimeError(f"'{job.target}' appeared meanwhile; not overwritten.")
            job.progress = 0.99
            job.result_video_id = await _ingest(PurePosixPath(job.target).name)
            job.progress, job.state = 1.0, "done"
            log.info("clip %s done: %s", job.id, job.target)
    except Exception as exc:
        if job.state != "cancelled":
            job.state, job.error = "failed", str(exc)
            log.warning("clip %s failed: %s", job.id, exc)
    finally:
        procs.pop(job.id, None)
        with contextlib.suppress(OSError):
            tmp.unlink()
        job.finished_at = job.finished_at or utcnow().isoformat()


async def _ingest(filename: str) -> int:
    """Ask the main app to rescan the library until the new file is in it."""
    for _ in range(30):
        with contextlib.suppress(Exception):
            await client.post("/api/videos/refresh")
            r = await client.get("/api/videos", params={"search": filename})
            hit = next((x for x in r.json() if x["filename"] == filename), None)
            if hit is not None:
                return hit["id"]
        await asyncio.sleep(2)
    raise RuntimeError(f"'{filename}' was saved, but the library has not listed it yet: press Refresh in the Library.")


@app.get("/api/videos/conversions")
async def conversions(request: Request):
    theirs = await upstream_json("GET", "/api/videos/conversions", request)
    mine = [j.view() for j in sorted(jobs.values(), key=lambda j: j.created_at, reverse=True)[:50]]
    return mine + theirs


@app.delete("/api/videos/conversions/{job_id}")
async def cancel(job_id: str, request: Request):
    job = jobs.get(job_id)
    if job is None:
        return await upstream_json("DELETE", f"/api/videos/conversions/{job_id}", request)
    if job.active:
        job.state = "cancelled"
        p = procs.get(job_id)
        if p and p.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                p.terminate()
        job.finished_at = utcnow().isoformat()
    return job.view()


# ------------------------------------------------------------------ everything else: the main app
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def proxy(path: str, request: Request):
    url = httpx.URL(path="/" + path, query=request.url.query.encode())
    req = client.build_request(request.method, url, headers=_fwd_headers(request),
                               content=request.stream() if request.method not in ("GET", "HEAD") else None)
    r = await client.send(req, stream=True)
    # raw bytes pass through untouched, so content-encoding/content-length stay valid
    headers = {k: v for k, v in r.headers.items() if k.lower() not in HOP - {"content-length"}}
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=headers,
                             background=BackgroundTask(r.aclose))


@app.on_event("shutdown")
async def _close():
    for j in list(jobs.values()):
        if j.active:
            await cancel(j.id, None)  # type: ignore[arg-type]
    await client.aclose()
