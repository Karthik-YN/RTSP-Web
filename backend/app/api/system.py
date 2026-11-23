from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select

from .. import __version__
from ..models import DesiredState, Stream, StreamStatus
from ..schemas import EventOut, MessageOut
from ..services.auth import User
from ..services.encoders import INFO as ENC_INFO
from ..services.ffprobe_service import ffmpeg_version
from ..services.mediamtx_service import UP
from ..services.network_info import interface_addresses
from .deps import Context, current_user, get_ctx, request_rtsp_hosts, require_operator

router = APIRouter(tags=["system"])


@router.get("/api/health")
def health(strict: bool = False, ctx: Context = Depends(get_ctx)):
    """Unauthenticated liveness/readiness probe (no sensitive data).

    ``?strict=true`` returns 503 unless MediaMTX is up and recovery finished.
    """
    mtx = ctx.mediamtx.state
    rec = ctx.recovery.report.get("state")
    ok = mtx == UP and rec in ("done", None)
    body = {"status": "ok" if ok else "degraded", "app": "ok", "mediamtx": mtx, "recovery": rec,
            "version": __version__}
    return JSONResponse(body, status_code=200 if ok or not strict else 503)


@router.get("/api/config")
def config(request: Request, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    s = ctx.settings
    warnings = list(ctx.network.warnings()) if ctx.network else []
    if s.public_rtsp_host in ("localhost", "127.0.0.1", "::1"):
        warnings.append("RTSPM_PUBLIC_RTSP_HOST is not set: RTSP URLs show 'localhost', which other "
                        "machines cannot use. Set it to the server's fixed DNS name or IP.")
    return {
        "version": __version__,
        "public_rtsp_host": s.public_rtsp_host,
        "public_rtsp_port": s.public_rtsp_port,
        "rtsp_hosts": request_rtsp_hosts(request, ctx),
        "server_addresses": ctx.network.addresses if ctx.network else interface_addresses(),
        "rtsp_url_template": s.public_rtsp_url("{name}"),
        "stream_name_pattern": s.stream_name_pattern,
        "reserved_names": s.reserved_names,
        "max_active_streams": ctx.supervisor.max_active(),
        "max_transcode_streams": ctx.supervisor.max_transcode(),
        "transcode_policy": s.transcode_policy,
        "copy_codecs": s.copy_codecs, "copy_pix_fmts": s.copy_pix_fmts, "copy_profiles": s.copy_profiles,
        "output_profile": {"codec": "h264", "encoder": ENC_INFO[ctx.supervisor.encoders.active()]["encoder"],
                           "profile": s.x264_profile,
                           "preset": s.x264_preset, "gop_seconds": s.x264_gop_seconds,
                           "max": f"{s.transcode_max_width}x{s.transcode_max_height}",
                           "fps_mode": s.output_fps_mode},
        "output": {"width": s.output_width, "height": s.output_height, "default": s.output_default,
                   "sizes": s.standard_sizes, "require_16_9": s.require_16_9},
        "preview": {"threshold_s": s.preview_threshold_s, "seconds": s.preview_seconds},
        # features this server has (the dashboard shows their controls only when listed)
        "features": ["clock_overlay", "codecs", "naming", "duplicate_check"],
        "clock": {"zones": ["IST", "UTC"], "default": "IST"},
        "min_segment_s": s.min_segment_s,
        "allowed_extensions": s.allowed_extensions,
        "upload_max_bytes": s.upload_max_bytes,
        "default_lead_in_mode": s.default_lead_in_mode,
        "default_lead_in_s": s.default_lead_in_s,
        "auth_enabled": s.auth_enabled,
        "ffmpeg_version": ffmpeg_version(s.ffmpeg_bin),
        "warnings": warnings,
    }


class EncoderIn(BaseModel):
    choice: Literal["auto", "cpu", "nvidia", "intel", "vaapi"] | None = None  # None = back to the default


@router.get("/api/system/encoders")
def get_encoders(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Encoders for live transcoding on this machine: which work, which one is used, and the choice."""
    return ctx.supervisor.encoders.view()


@router.put("/api/system/encoders")
def set_encoder(body: EncoderIn, ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Choose the encoder (auto/cpu/nvidia/intel/vaapi). New and restarted transcoding streams use it."""
    enc = ctx.supervisor.encoders
    ctx.supervisor.limits.set_str("encoder", body.choice, user.username)
    view = enc.view()
    ctx.events.add("settings.encoder", f"encoder choice: {view['choice']} (using {view['active_label']})",
                   username=user.username)
    return view


@router.post("/api/system/encoders/detect")
async def detect_encoders(ctx: Context = Depends(get_ctx), _: User = Depends(require_operator)):
    """Test every encoder again (e.g. after passing a GPU into the container)."""
    import asyncio
    await asyncio.to_thread(ctx.supervisor.encoders.detect)
    return ctx.supervisor.encoders.view()


@router.get("/api/system")
def system(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    smp = dict(ctx.sysmon.sample or {})
    with ctx.db.session() as s:
        rows = list(s.scalars(select(Stream)))
    names = {r.name for r in rows}
    desired = [r for r in rows if r.desired_state == DesiredState.RUNNING]
    running = [r for r in rows if r.status == StreamStatus.RUNNING]
    transcoding = [r for r in desired if r.output_mode == "transcode"]
    s_ = ctx.settings
    cap, tcap = ctx.supervisor.max_active(), ctx.supervisor.max_transcode()
    warnings = ctx.sysmon.warnings()
    if len(desired) >= cap:
        warnings.append(f"Stream capacity reached ({len(desired)}/{cap}).")
    elif len(desired) >= 0.8 * cap:
        warnings.append(f"Approaching stream capacity ({len(desired)}/{cap}).")
    errors = [r.name for r in rows if r.status == StreamStatus.ERROR]
    ingest_bps, egress_bps = ctx.mediamtx.total_bps(names) if ctx.mediamtx.state == UP else (None, None)
    if egress_bps and egress_bps / 1e6 >= s_.net_warn_mbps:
        warnings.append(f"RTSP egress is {egress_bps / 1e6:.0f} Mbit/s (warning threshold {s_.net_warn_mbps:.0f}); "
                        "the network may be saturated.")
    return {
        **smp,
        "streams": {
            "total": len(rows), "desired_running": len(desired), "running": len(running),
            "errors": len(errors), "error_names": errors,
            "transcoding": len(transcoding), "max_active": cap,
            "max_transcode": tcap,
            "capacity_percent": round(len(desired) / cap * 100) if cap else None,
        },
        "start_queue": {"spacing_s": s_.start_spacing_s, "ready_wait_s": s_.start_ready_wait_s,
                        "phase_margin_s": s_.start_phase_margin_s, "queued": ctx.supervisor.gate.pending(),
                        "launching": ctx.supervisor.gate.current is not None},
        "viewers": ctx.mediamtx.total_readers(names) if ctx.mediamtx.state == UP else None,
        "rtsp_ingest_bps": round(ingest_bps) if ingest_bps is not None else None,
        "rtsp_egress_bps": round(egress_bps) if egress_bps is not None else None,
        "warnings": warnings,
        "upload_in_flight_bytes": ctx.library.uploads.total(),
        "library": {"last_scan_at": ctx.library.last_scan_at.isoformat() if ctx.library.last_scan_at else None,
                    **ctx.library.last_scan_summary},
    }


@router.get("/api/system/recovery")
def recovery(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Timings of the last startup recovery (evidence for the 2-minute target)."""
    return ctx.recovery.report


@router.get("/api/events", response_model=list[EventOut])
def events(limit: int = Query(default=100, ge=1, le=1000), stream_id: int | None = None,
           kind: str | None = None, ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    return [EventOut(id=e.id, ts=e.ts, level=e.level, kind=e.kind, stream_id=e.stream_id,
                     stream_name=e.stream_name, username=e.username, message=e.message, data=e.data_json)
            for e in ctx.events.recent(limit, stream_id=stream_id, kind=kind)]


# ------------------------------------------------------------------ MediaMTX

@router.get("/api/mediamtx/status", tags=["mediamtx"])
def mediamtx_status(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    st = ctx.mediamtx.status()
    st["paths"] = [
        {"name": p.name, "ready": p.ready, "source": p.source_type, "tracks": p.tracks,
         "readers": len(p.readers), "bytes_received": p.bytes_received}
        for p in ctx.mediamtx.paths.values()
    ]
    return st


@router.post("/api/mediamtx/start", response_model=MessageOut, tags=["mediamtx"])
async def mediamtx_start(ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    try:
        msg = await ctx.mediamtx.controller.start()
    except Exception as exc:
        raise HTTPException(502, f"Could not start MediaMTX: {exc}")
    ctx.events.add("mediamtx.start", msg, username=user.username)
    return MessageOut(message=msg, status=ctx.mediamtx.status())


@router.post("/api/mediamtx/restart", response_model=MessageOut, tags=["mediamtx"])
async def mediamtx_restart(ctx: Context = Depends(get_ctx), user: User = Depends(require_operator)):
    """Restart MediaMTX. Publishers drop and are republished automatically."""
    try:
        msg = await ctx.mediamtx.controller.restart()
    except Exception as exc:
        raise HTTPException(502, f"Could not restart MediaMTX: {exc}")
    ctx.events.add("mediamtx.restart", msg, username=user.username, level="WARNING")
    return MessageOut(message=msg, status=ctx.mediamtx.status())


# ------------------------------------------------------------------ Prometheus

@router.get("/api/metrics", tags=["system"], response_class=PlainTextResponse)
def metrics(ctx: Context = Depends(get_ctx), _: User = Depends(current_user)):
    """Prometheus text format (scrape with Basic auth)."""
    with ctx.db.session() as s:
        rows = list(s.scalars(select(Stream)))
    smp = ctx.sysmon.sample or {}
    mtx = ctx.mediamtx
    out: list[str] = []

    def m(name: str, help_: str, kind: str, samples: list[tuple[str, float]]):
        out.append(f"# HELP rtspm_{name} {help_}")
        out.append(f"# TYPE rtspm_{name} {kind}")
        for labels, value in samples:
            out.append(f"rtspm_{name}{labels} {value}")

    def esc(v: str) -> str:
        return v.replace("\\", "\\\\").replace('"', '\\"')

    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    m("streams", "Streams by status", "gauge", [(f'{{status="{k}"}}', v) for k, v in sorted(by_status.items())])
    m("stream_up", "1 if the stream is RUNNING", "gauge",
      [(f'{{stream="{esc(r.name)}"}}', 1 if r.status == StreamStatus.RUNNING else 0) for r in rows])
    m("stream_restarts_total", "Automatic publisher restarts", "counter",
      [(f'{{stream="{esc(r.name)}"}}', r.restart_count) for r in rows])
    m("stream_loops_total", "Completed loops", "counter", [(f'{{stream="{esc(r.name)}"}}', r.loop_count) for r in rows])
    paths = mtx.paths if mtx.state == UP else {}
    m("stream_viewers", "Connected RTSP readers", "gauge",
      [(f'{{stream="{esc(r.name)}"}}', len(paths[r.name].readers) if r.name in paths else 0) for r in rows])
    m("stream_egress_bits_per_second", "RTSP bitrate sent to readers", "gauge",
      [(f'{{stream="{esc(r.name)}"}}', round(paths[r.name].egress_bps or 0)) for r in rows if r.name in paths])
    m("mediamtx_up", "1 if the MediaMTX Control API answers", "gauge", [("", 1 if mtx.state == UP else 0)])
    m("mediamtx_recoveries_total", "MediaMTX outages recovered", "counter", [("", mtx.recoveries)])
    m("cpu_percent", "Host CPU utilisation", "gauge", [("", smp.get("cpu_percent", 0))])
    m("memory_percent", "Host memory utilisation", "gauge", [("", (smp.get("memory") or {}).get("percent", 0))])
    vd = (smp.get("disks") or {}).get("videos") or {}
    m("video_disk_free_bytes", "Free space on the video disk", "gauge", [("", vd.get("free", 0))])
    m("ffmpeg_processes", "Running FFmpeg publishers", "gauge", [("", smp.get("ffmpeg_processes", 0))])
    return "\n".join(out) + "\n"


@router.get("/api/system/time")
def server_time(_: User = Depends(current_user)):
    """The server's clock (ms), for the dashboard's rig clock: compare it with a phone/laptop clock or a camera
    picture. The dashboard measures the round trip and corrects for half of it."""
    import time as _t
    return {"epoch_ms": round(_t.time() * 1000, 1)}
