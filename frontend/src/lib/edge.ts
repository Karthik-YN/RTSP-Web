// Pure helpers for what an edge device sees: frame health, who is reading, CSV export, keyframe hints.
import type { Conversion, Reader, Stream, Video } from "../types/api";
import { formatDuration } from "./format";

// ------------------------------------------------------------------ frame health
export type FrameHealthState = "healthy" | "slow" | "frozen";

export interface FrameHealth {
  state: FrameHealthState;
  /** actual publisher fps (last report) */
  fps: number;
  /** seconds since the last progress report */
  ageS: number | null;
  reason: string;
}

/** A running publisher's progress is stale after this many seconds. */
export const FROZEN_AFTER_S = 10;

/**
 * Is a RUNNING stream sending frames at real-time pace? null when not running or no progress yet.
 * expectedFps: the source video's fps (null: unknown, only speed and staleness are checked).
 */
export function frameHealth(s: Stream, expectedFps: number | null | undefined, now: number = Date.now()): FrameHealth | null {
  const p = s.runtime?.progress;
  if (s.status !== "RUNNING" || !p) return null;
  const fps = Number(p.fps) || 0;
  const t = p.updated_at ? new Date(p.updated_at).getTime() : NaN;
  const ageS = Number.isFinite(t) ? Math.max(0, (now - t) / 1000) : null;
  if (ageS !== null && ageS > FROZEN_AFTER_S) {
    return { state: "frozen", fps, ageS,
      reason: `No new frames for ${Math.round(ageS)} s: the publisher is running but its progress stopped. Edges see a frozen picture.` };
  }
  const exp = expectedFps && expectedFps > 0 ? expectedFps : null;
  const speed = p.speed;
  const slowFps = exp !== null && fps < 0.9 * exp;
  const slowSpeed = speed != null && speed < 0.97;
  if (slowFps || slowSpeed) {
    const why = [
      slowFps ? `${fps.toFixed(1)} fps of ${exp!.toFixed(exp! % 1 ? 2 : 0)} expected` : null,
      slowSpeed ? `speed ${speed!.toFixed(2)}x` : null,
    ].filter(Boolean).join(", ");
    return { state: "slow", fps, ageS,
      reason: `Frames are arriving slower than real time (${why}). Edges may see stutter or growing delay; check CPU or transcoding load.` };
  }
  const parts = [`${fps.toFixed(1)} fps`, exp !== null ? `video is ${exp.toFixed(exp % 1 ? 2 : 0)} fps` : null,
    speed != null ? `speed ${speed.toFixed(2)}x` : null].filter(Boolean).join(" · ");
  return { state: "healthy", fps, ageS, reason: `Frames on time: ${parts}.` };
}

// ------------------------------------------------------------------ readers
/** "192.168.1.5:58588" -> "192.168.1.5", "[fe80::1]:554" -> "fe80::1". */
export function stripPort(addr: string): string {
  const a = addr.trim();
  const v6 = /^\[([^\]]+)\](?::\d+)?$/.exec(a);
  if (v6) return v6[1];
  const parts = a.split(":");
  return parts.length === 2 ? parts[0] : a; // several colons and no brackets: a bare IPv6 address
}

function hhmm(iso: string | null): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** One line per reader address: "IP · TCP · since HH:MM", duplicates as "IP ×4 · …" (earliest since). */
export function readerLines(readers: Reader[] | null | undefined): string[] {
  const groups = new Map<string, { ip: string; transport: string | null; n: number; since: string | null; t: number }>();
  for (const r of readers ?? []) {
    const ip = r.remote_addr ? stripPort(r.remote_addr) : (r.type ?? "unknown");
    const key = `${ip}|${r.transport ?? ""}`;
    const t = r.since ? new Date(r.since).getTime() : Infinity;
    const g = groups.get(key);
    if (g) {
      g.n += 1;
      if (t < g.t) { g.t = t; g.since = r.since; }
    } else groups.set(key, { ip, transport: r.transport, n: 1, since: r.since, t });
  }
  return [...groups.values()].map((g) => [
    g.n > 1 ? `${g.ip} ×${g.n}` : g.ip,
    g.transport,
    hhmm(g.since) ? `since ${hhmm(g.since)}` : null,
  ].filter(Boolean).join(" · "));
}

/** Hover text for a viewer count. */
export function readersTitle(s: Stream): string {
  const lines = readerLines(s.runtime?.readers);
  return lines.length ? `RTSP readers now:\n${lines.join("\n")}` : "RTSP readers now (edges, testers): none";
}

// ------------------------------------------------------------------ CSV for edges
export const EDGE_CSV_COLUMNS = ["stream", "rtsp_url", "status", "resolution", "fps", "codec", "keyframe_s", "video"] as const;

export function csvField(v: string | number | null | undefined): string {
  const t = v == null ? "" : String(v);
  return /[",\r\n]/.test(t) || /^\s|\s$/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
}

/** One row per stream with what an edge needs to configure the camera (RFC 4180, CRLF line ends). */
export function buildEdgeCsv(streams: Stream[], videos: Video[] | null | undefined, url: (s: Stream) => string = (s) => s.rtsp_url): string {
  const byId = new Map((videos ?? []).map((v) => [v.id, v]));
  const rows = [...streams]
    .sort((x, y) => x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: "base" }))
    .map((s) => {
      const v = s.video_id != null ? byId.get(s.video_id) : undefined;
      return [
        s.name, url(s), s.status, s.output_resolution ?? "",
        v?.fps ? v.fps.toFixed(2) : "",
        "h264", // streams are always sent as H.264 (copied or transcoded)
        v?.keyframe_interval_s ? v.keyframe_interval_s.toFixed(1) : "",
        v?.rel_path ?? s.source_rel_path ?? "",
      ];
    });
  return [[...EDGE_CSV_COLUMNS], ...rows].map((r) => r.map(csvField).join(",")).join("\r\n") + "\r\n";
}

export function slug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9_]+/g, "-").replace(/^-+|-+$/g, "") || "streams";
}

/** "ALPR-streams.csv" from the analytics code, else a slug of its name. */
export function edgeCsvFilename(name: string, code?: string | null): string {
  return `${code?.trim() || slug(name)}-streams.csv`;
}

/** Save text as a file through a temporary <a download>. */
export function downloadText(filename: string, text: string, type = "text/csv;charset=utf-8"): void {
  const blob = new Blob([text], { type });
  const href = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = href;
  a.download = filename;
  a.style.display = "none";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 0);
}

// ------------------------------------------------------------------ conversion queue
export interface ConversionSummary {
  active: number;
  /** progress 0..100 of the running job, null while only queued ones exist */
  percent: number | null;
  title: string;
}

/** Top-bar summary of queued/running conversions; null when nothing is active. */
export function conversionSummary(jobs: Conversion[] | null | undefined): ConversionSummary | null {
  const active = (Array.isArray(jobs) ? jobs : []).filter((j) => j && (j.state === "running" || j.state === "queued"));
  if (!active.length) return null;
  active.sort((a, b) => (a.state === b.state ? 0 : a.state === "running" ? -1 : 1));
  const running = active.find((j) => j.state === "running");
  const lines = active.map((j) => {
    const what = `${j.source} → ${j.target}`;
    if (j.state === "queued") return `${what} · queued`;
    const pct = `${Math.round((j.progress ?? 0) * 100)}%`;
    return `${what} · ${pct}${j.eta_s != null ? ` · ETA ${formatDuration(j.eta_s)}` : ""}`;
  });
  return {
    active: active.length,
    percent: running ? Math.round(Math.max(0, Math.min(1, running.progress ?? 0)) * 100) : null,
    title: `Video conversions:\n${lines.join("\n")}`,
  };
}

// ------------------------------------------------------------------ keyframe hint
export const CAMERA_KEYFRAME_MAX_S = 2.5;

/** "Keyframe every 4.2 s" for videos whose long keyframe gap delays an edge's first picture. */
export function keyframeHint(v: Pick<Video, "keyframe_interval_s">): { text: string; title: string } | null {
  const k = v.keyframe_interval_s;
  if (k == null || !Number.isFinite(k) || k <= CAMERA_KEYFRAME_MAX_S) return null;
  const n = String(Number(k.toFixed(1)));
  return {
    text: `Keyframe every ${n} s`,
    title: `Edges wait up to ${n} s for the first picture after connecting. Make a camera copy (keyframe every 2 s).`,
  };
}
