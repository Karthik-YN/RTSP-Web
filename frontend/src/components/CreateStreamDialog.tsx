import { useEffect, useMemo, useRef, useState } from "react";
import { api, ApiError, type StreamInput } from "../services/api";
import { formatDuration, formatTime, parseTime, suggestName, validateName } from "../lib/format";
import type { AppConfig, ClockTz, OutputConfig, Stream, ValidateResult, Video } from "../types/api";
import { LivePlayer } from "./LivePlayer";
import { CopyButton, ErrorBox, Modal, Pill, Spinner, WarnBox } from "./common";
import { leadInBody, leadInError, LeadInFields, type LeadInValue } from "./LeadIn";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { useSystemMaybe } from "../hooks/useRigData";

export interface StreamTemplate {
  videoId: number; name?: string; start?: string; end?: string; mode?: "loop" | "once"; forceTranscode?: boolean;
  clock?: boolean; clockTz?: ClockTz;
}

/**
 * A video with keyframes far apart (copy mode) makes every new viewer wait for
 * the next keyframe: up to that many seconds of black screen in VLC or on the
 * edge. Offers to re-encode it with a keyframe every 2 s.
 */
export function FastStartNotice({ video, checked, onChange }: {
  video: Video | null; checked: boolean; onChange: (on: boolean) => void;
}) {
  const gop = video?.keyframe_interval_s ?? 0;
  if (!video || gop <= 4 || video.compat_mode !== "copy") return null;
  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950/40"
      data-testid="long-gop">
      <p>
        This video has a keyframe only every {gop.toFixed(1)} s, so a viewer that connects (VLC, the edge)
        may see a black screen for up to {Math.ceil(gop)} s before the picture appears.
      </p>
      <label className="mt-2 flex items-center gap-2 font-medium">
        <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />
        Re-encode for a fast start (a keyframe every 2 s; uses more server CPU)
      </label>
    </div>
  );
}

/** True when streams from this video are refused because it is not 16:9. */
export function blockedShape(v: Video | null | undefined, out?: OutputConfig): boolean {
  return !!v && !!out?.require_16_9 && v.is_16_9 === false;
}

export const SHAPE_TEXT: Record<string, string> = {
  portrait: "portrait", square: "square", "4:3": "4:3", other: "wide/other", "16:9": "16:9",
};

/** Will this stream be scaled live (costs CPU), or run as-is / from the 720p copy? */
export function outputPlan(v: Video | null, size: string, out?: OutputConfig): "as-is" | "copy" | "live" {
  if (!v || !out || size === "original") return "as-is";
  const [w, h] = size === "standard" ? [out.width, out.height] : size.split("x").map(Number);
  if ((v.display_width ?? v.width) === w && (v.display_height ?? v.height) === h) return "as-is";
  if (size === "standard" && v.copy_720p_id) return "copy";
  return "live";
}

/**
 * Explicit choice when creating a stream: draw the rig's clock (wall-clock time with milliseconds, IST or UTC) on
 * every frame, to compare with the receiver's clock (latency) and line up events. Off by default: it re-encodes.
 */
export function ClockField({ config, value, tz, onChange, idPrefix }: {
  config: AppConfig | null | undefined; value: boolean; tz: ClockTz; onChange: (on: boolean, tz: ClockTz) => void; idPrefix: string;
}) {
  const system = useSystemMaybe();
  if (!config?.features?.includes("clock_overlay")) return null; // older server
  const cap = system?.streams;
  return (
    <div className="rounded-lg bg-surface-2 p-3 ring-1 ring-inset ring-line" data-testid="clock-field">
      <label className="flex items-start gap-2 text-sm">
        <input type="checkbox" className="mt-0.5" checked={value} onChange={(e) => onChange(e.target.checked, tz)}
          aria-describedby={`${idPrefix}-clock-help`} />
        <span><b>Server clock in picture</b> — the rig's time with milliseconds, burned into every frame (top right)</span>
      </label>
      {value && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 pl-6 text-sm" role="radiogroup" aria-label="Clock time zone">
          {(["IST", "UTC"] as const).map((z) => (
            <label key={z} className="flex items-center gap-1.5">
              <input type="radio" name={`${idPrefix}-clock-tz`} checked={tz === z} onChange={() => onChange(true, z)} />
              {z === "IST" ? "IST (India, UTC+5:30)" : "UTC"}
            </label>
          ))}
        </div>
      )}
      <p id={`${idPrefix}-clock-help`} className="mt-1 pl-6 text-xs text-muted">
        {value
          ? <>Re-encodes this stream live: about 1–2 CPU cores at 720p, 4 at 1080p. It counts toward the transcode limit
            {cap ? <> ({cap.transcoding} of {cap.max_transcode} in use)</> : null}. Use it only on streams that need it.</>
          : "Off: the video is sent as it is (no CPU). Turn on for latency tests or to line up events with frames."}
      </p>
    </div>
  );
}

/** Output size of a new stream: the same as the file (default: no scaling), the standard 16:9 size, or (admins) another size. */
export function OutputSizeField({ out, video, value, onChange, isAdmin, idPrefix }: {
  out?: OutputConfig; video: Video | null; value: string; onChange: (v: string) => void; isAdmin: boolean; idPrefix: string;
}) {
  if (!out) return null;
  const std = `${out.width}x${out.height}`;
  const extra = out.sizes.filter((s) => s !== std);
  const plan = outputPlan(video, value, out);
  const orig = video ? `${video.display_width ?? video.width}×${video.display_height ?? video.height}` : "";
  return (
    <div data-testid="output-size">
      <span className="label">Output size</span>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
        <label className="flex items-center gap-1.5">
          <input type="radio" name={`${idPrefix}-out`} checked={value === "original"} onChange={() => onChange("original")} />
          Same as the file{orig && ` (${orig})`}
        </label>
        <label className="flex items-center gap-1.5">
          <input type="radio" name={`${idPrefix}-out`} checked={value === "standard"} onChange={() => onChange("standard")} />
          {out.width}×{out.height}
        </label>
        {isAdmin && extra.length > 0 && (
          <select className="input h-8 w-auto py-1 text-sm" aria-label="Other output size"
            value={extra.includes(value) ? value : ""} onChange={(e) => e.target.value && onChange(e.target.value)}>
            <option value="">Other size…</option>
            {extra.map((s) => <option key={s} value={s}>{s.replace("x", "×")}</option>)}
          </select>
        )}
      </div>
      <p className="mt-1 text-xs text-muted" data-testid="output-plan">
        {plan === "as-is" && "Streams the file as it is."}
        {plan === "copy" && "Uses the ready 720p copy of this video: no live transcoding."}
        {plan === "live" && "Scaled live on the server (about one CPU core per stream). Make a 720p copy in the library to stream it without that cost."}
      </p>
    </div>
  );
}

export function CreateStreamDialog({ videos, streams, config, initialVideoId, template, onClose, onCreated, isAdmin }: {
  videos: Video[]; streams: Stream[]; config: AppConfig; initialVideoId?: number; template?: StreamTemplate; isAdmin?: boolean;
  onClose: () => void; onCreated: () => void;
}) {
  const addr = useRtspAddress();
  const ready = useMemo(() => videos.filter((v) => v.status === "READY" && v.compat_mode !== "reject"), [videos]);
  const existing = useMemo(() => streams.map((s) => s.name), [streams]);
  const [videoId, setVideoId] = useState<number | null>(template?.videoId ?? initialVideoId ?? ready[0]?.id ?? null);
  const video = ready.find((v) => v.id === videoId) ?? null;
  const [name, setName] = useState(() => template?.name ? suggestName(template.name, existing)
    : video ? suggestName(video.filename, existing) : "");
  const [nameTouched, setNameTouched] = useState(!!template?.name);
  const [start, setStart] = useState(template?.start ?? "");
  const [end, setEnd] = useState(template?.end ?? "");
  const [mode, setMode] = useState<"loop" | "once">(template?.mode ?? "loop");
  const [forceTranscode, setForceTranscode] = useState(template?.forceTranscode ?? false);
  const [outputSize, setOutputSize] = useState<string>("original"); // same size as the file: no scaling unless asked
  const [clock, setClock] = useState<{ on: boolean; tz: ClockTz }>({ on: template?.clock ?? false, tz: template?.clockTz ?? "IST" });
  const [lead, setLead] = useState<LeadInValue>({
    mode: config.default_lead_in_mode ?? "fixed", seconds: config.default_lead_in_s ?? 5, holdMax: null });
  const [slider, setSlider] = useState(0);
  const [server, setServer] = useState<ValidateResult | null>(null);
  const [checking, setChecking] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [created, setCreated] = useState<Stream | null>(null);
  const [filter, setFilter] = useState("");

  // suggest a name from the file unless the user typed one
  useEffect(() => {
    if (video && !nameTouched) setName(suggestName(video.filename, existing));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId]);

  const st = parseTime(start);
  const en = parseTime(end);
  const nameError = validateName(name, config.stream_name_pattern, config.reserved_names, existing);
  const dur = video?.duration_s ?? null;
  let rangeError: string | null = st.error || en.error;
  if (!rangeError && dur !== null) {
    const s = st.seconds ?? 0;
    if (s >= dur) rangeError = `Start is beyond the end of the file (${formatTime(dur)}).`;
    else if (en.seconds !== null && en.seconds <= s) rangeError = "End must be after start.";
    else if (en.seconds !== null && en.seconds > dur + 0.001) rangeError = `End is beyond the end of the file (${formatTime(dur)}).`;
  }

  const body: StreamInput | null = video ? {
    name, video_id: video.id, start: start.trim() || null, end: end.trim() || null, mode,
    force_transcode: forceTranscode, output_size: outputSize,
    ...(config.features?.includes("clock_overlay") ? { clock: clock.on, clock_tz: clock.tz } : {}),
  } : null;

  // debounced server-side dry run (keyframe alignment, codec decision, warnings)
  const reqSeq = useRef(0);
  useEffect(() => {
    setServer(null);
    if (!body || rangeError) return;
    const my = ++reqSeq.current;
    setChecking(true);
    const t = setTimeout(async () => {
      try {
        const r = await api.validate(body);
        if (my === reqSeq.current) setServer(r);
      } catch (e) {
        if (my === reqSeq.current) setServer({ ok: false, errors: [{ field: null, message: (e as ApiError).message }] } as ValidateResult);
      } finally {
        if (my === reqSeq.current) setChecking(false);
      }
    }, 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [videoId, name, start, end, mode, forceTranscode, outputSize, rangeError, clock.on, clock.tz]);

  const serverErrors = server?.errors ?? [];
  const canSubmit = !!body && !nameError && !rangeError && !submitting && server?.ok === true
    && (lead.mode === "none" || !leadInError(String(lead.seconds)));

  const submit = async () => {
    if (!body) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const s = await api.create({ ...body, start_now: true, ...leadInBody(lead) });
      setCreated(s);
      onCreated();
    } catch (e) {
      setSubmitError((e as ApiError).message);
    } finally {
      setSubmitting(false);
    }
  };

  if (created) {
    return (
      <Modal title="Stream created" onClose={onClose}>
        <p className="text-sm text-fg">
          <b>{created.name}</b> is starting. Paste this URL into the Video Analytics portal as the camera's RTSP URL:
        </p>
        <div className="mt-3 flex items-center gap-2 rounded-md bg-slate-950 px-3 py-2 font-mono text-sm text-emerald-300">
          <span className="flex-1 break-all" data-testid="created-url">{addr.url(created.rtsp_url)}</span>
          <CopyButton text={addr.url(created.rtsp_url)} label="Copy URL" />
        </div>
        <LivePlayer streamId={created.id} className="mt-3" />
        <p className="mt-3 text-xs text-muted">
          The edge can be pointed at the URL right away; if it connects before the first frame is published, the
          server holds the connection until the stream is live.
        </p>
        <div className="mt-4 flex justify-end"><button className="btn-primary" onClick={onClose}>Done</button></div>
      </Modal>
    );
  }

  const shown = ready.filter((v) => v.filename.toLowerCase().includes(filter.toLowerCase()) || v.id === videoId);

  return (
    <Modal title={template ? "Clone stream" : "New stream"} onClose={onClose} wide>
      {ready.length === 0 ? (
        <WarnBox>No usable videos in the library yet. Upload one or copy files into the server folder, then press Refresh.</WarnBox>
      ) : (
        <form className="grid gap-4 md:grid-cols-2" onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
          <div className="md:col-span-2">
            <label className="label" htmlFor="video">1. Video</label>
            <div className="flex gap-2">
              <input className="input max-w-[12rem]" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />
              <select id="video" className="input" value={videoId ?? ""} onChange={(e) => { setVideoId(Number(e.target.value)); setSlider(0); }}>
                {shown.map((v) => (
                  <option key={v.id} value={v.id} disabled={blockedShape(v, config.output)}>
                    {v.rel_path} — {formatTime(v.duration_s, false)} · {v.width}x{v.height} · {v.fps?.toFixed(2)} fps · {v.video_codec}
                    {v.compat_mode === "transcode" ? " (transcode)" : ""}
                    {blockedShape(v, config.output) ? ` (not 16:9: ${SHAPE_TEXT[v.shape ?? "other"]})` : ""}
                  </option>
                ))}
              </select>
            </div>
            {video && video.compat_mode === "transcode" && (
              <div className="mt-2"><WarnBox>{video.compat_reasons.join(" ")} It will be transcoded to H.264 (CPU cost).</WarnBox></div>
            )}
          </div>

          <div>
            <label className="label" htmlFor="name">2. Stream name</label>
            <input id="name" className={`input font-mono ${nameError ? "input-error" : ""}`} value={name}
              onChange={(e) => { setName(e.target.value); setNameTouched(true); }} autoComplete="off" />
            {nameError ? <p className="mt-1 text-xs text-red-600">{nameError}</p>
              : <p className="mt-1 truncate font-mono text-xs text-muted">{addr.url(config.rtsp_url_template.replace("{name}", name))}</p>}
          </div>

          <div>
            <span className="label">3. Playback</span>
            <div className="flex gap-4 pt-1 text-sm">
              <label className="flex items-center gap-1.5"><input type="radio" checked={mode === "loop"} onChange={() => setMode("loop")} /> Loop</label>
              <label className="flex items-center gap-1.5"><input type="radio" checked={mode === "once"} onChange={() => setMode("once")} /> Play once</label>
              <span className="text-subtle">Speed 1x (fixed)</span>
            </div>
            <span className="label mt-3">Lead-in (before the video)</span>
            <LeadInFields value={lead} onChange={setLead} idPrefix="create-lead" />
          </div>

          <div className="md:col-span-2">
            <span className="label">4. Start / end (HH:MM:SS)</span>
            {video && dur !== null && (
              <div className="flex flex-col gap-2 rounded-md border border-line bg-surface-2 p-3 md:flex-row">
                <FramePreview videoId={video.id} t={slider} />
                <div className="flex-1">
                  <input type="range" className="w-full" min={0} max={Math.max(0, dur - 0.04)} step={Math.max(0.04, dur / 2000)}
                    value={slider} onChange={(e) => setSlider(Number(e.target.value))} aria-label="Timeline" />
                  <div className="flex items-center justify-between font-mono text-xs text-muted">
                    <span>00:00:00</span><span className="font-semibold text-fg">{formatTime(slider)}</span><span>{formatTime(dur)}</span>
                  </div>
                  <div className="mt-2 flex gap-2">
                    <button type="button" className="btn-secondary btn-xs" onClick={() => setStart(formatTime(slider))}>Set as start</button>
                    <button type="button" className="btn-secondary btn-xs" onClick={() => setEnd(formatTime(slider))}>Set as end</button>
                  </div>
                </div>
              </div>
            )}
            {video && dur !== null && dur > 6 && <FrameStrip videoId={video.id} duration={dur} current={slider} onPick={setSlider} />}
            <div className="mt-2 grid grid-cols-2 gap-3">
              <div>
                <input aria-label="Start time" className={`input font-mono ${st.error ? "input-error" : ""}`} placeholder="00:00:00 (beginning)"
                  value={start} onChange={(e) => setStart(e.target.value)} />
                <p className="mt-1 text-xs text-muted">{st.error ?? (st.seconds !== null ? `= ${formatTime(st.seconds)}` : "from the beginning")}</p>
              </div>
              <div>
                <input aria-label="End time" className={`input font-mono ${en.error ? "input-error" : ""}`} placeholder="optional (end of file)"
                  value={end} onChange={(e) => setEnd(e.target.value)} />
                <p className="mt-1 text-xs text-muted">{en.error ?? (en.seconds !== null ? `= ${formatTime(en.seconds)}` : "to the end of the file")}</p>
              </div>
            </div>
          </div>

          <div className="md:col-span-2 space-y-3">
            <OutputSizeField out={config.output} video={video} value={outputSize} onChange={setOutputSize}
              isAdmin={!!isAdmin} idPrefix="create" />
            {outputPlan(video, outputSize, config.output) !== "live" &&
              <FastStartNotice video={video} checked={forceTranscode} onChange={setForceTranscode} />}
            <ClockField config={config} value={clock.on} tz={clock.tz} onChange={(on, tz) => setClock({ on, tz })} idPrefix="create" />
          </div>

          <div className="md:col-span-2">
            <details className="text-sm">
              <summary className="cursor-pointer text-muted">Advanced</summary>
              <label className="mt-2 flex items-center gap-2">
                <input type="checkbox" checked={forceTranscode} onChange={(e) => setForceTranscode(e.target.checked)} />
                Always transcode to H.264 (normally only incompatible files are transcoded)
              </label>
            </details>
          </div>

          <div className="md:col-span-2 space-y-2">
            {rangeError && <ErrorBox>{rangeError}</ErrorBox>}
            {checking && <div className="flex items-center gap-2 text-xs text-muted"><Spinner /> Checking…</div>}
            {serverErrors.map((e, i) => <ErrorBox key={i}>{e.message}</ErrorBox>)}
            {server?.ok && (
              <div className="rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-900 dark:text-emerald-200" data-testid="preview">
                Will publish {server.output_mode === "copy" ? <Pill tone="ok">stream copy</Pill> : <Pill tone="warn">H.264 transcode</Pill>}{" "}
                from <b className="font-mono">{formatTime(server.effective_start_s)}</b> to{" "}
                <b className="font-mono">{formatTime(server.effective_end_s)}</b>
                {" "}({formatDuration(server.period_s)}{mode === "loop" ? ", looped" : ", once"}).
                {server.start_s !== null && server.effective_start_s !== null && Math.abs(server.start_s - server.effective_start_s) > 0.001 && (
                  <div className="mt-1 text-xs text-emerald-800 dark:text-emerald-300">Start/end are aligned to the nearest earlier keyframe so every loop starts with a clean picture.</div>
                )}
              </div>
            )}
            {server?.warnings?.map((w, i) => <WarnBox key={i}>{w}</WarnBox>)}
            {submitError && <ErrorBox>{submitError}</ErrorBox>}
          </div>

          <div className="md:col-span-2 flex justify-end gap-2">
            <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary" disabled={!canSubmit}>
              {submitting ? <Spinner /> : null} Create &amp; start
            </button>
          </div>
        </form>
      )}
    </Modal>
  );
}

function FramePreview({ videoId, t }: { videoId: number; t: number }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    const h = setTimeout(() => setSrc(`/api/videos/${videoId}/frame?t=${t.toFixed(2)}`), 250);
    return () => clearTimeout(h);
  }, [videoId, t]);
  return (
    <div className="flex h-[90px] w-40 shrink-0 items-center justify-center overflow-hidden rounded bg-slate-800">
      {src ? <img src={src} alt="Frame preview" className="max-h-full max-w-full" /> : <Spinner />}
    </div>
  );
}

/** Six evenly spaced thumbnails across the file; click one to jump the timeline there. */
function FrameStrip({ videoId, duration, current, onPick }: {
  videoId: number; duration: number; current: number; onPick: (t: number) => void;
}) {
  const points = Array.from({ length: 6 }, (_, i) => (duration * (i + 0.5)) / 6);
  return (
    <div className="mt-2 grid grid-cols-6 gap-1.5" aria-label="Jump to a point in the video">
      {points.map((t) => {
        const active = Math.abs(current - t) < duration / 12;
        return (
          <button key={t} type="button" onClick={() => onPick(t)} title={`Jump to ${formatTime(t, false)}`}
            className={`group relative aspect-video overflow-hidden rounded-md bg-slate-800 ring-2 transition ${active ? "ring-brand" : "ring-transparent hover:ring-line"}`}>
            <img src={`/api/videos/${videoId}/frame?t=${t.toFixed(1)}`} alt="" loading="lazy" className="h-full w-full object-cover" />
            <span className="absolute inset-x-0 bottom-0 bg-black/60 py-0.5 text-center font-mono text-[10px] text-white">{formatTime(t, false)}</span>
          </button>
        );
      })}
    </div>
  );
}
