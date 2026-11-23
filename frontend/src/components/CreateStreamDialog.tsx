import { useEffect, useId, useMemo, useRef, useState } from "react";
import { api, ApiError, type StreamInput } from "../services/api";
import { formatDuration, formatTime, parseTime, suggestName, validateName } from "../lib/format";
import type { AppConfig, ClockTz, OutputConfig, Stream, ValidateResult, Video } from "../types/api";
import { LivePlayer } from "./LivePlayer";
import { CopyButton, ErrorBox, Modal, Pill, Spinner, WarnBox } from "./common";
import { leadInBody, leadInError, LeadInFields, type LeadInValue } from "./LeadIn";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { useSystemMaybe } from "../hooks/useRigData";
import { FrameAt, Hint, Section, Segmented, SummaryRow, Timeline, useMediaQuery, VideoPicker } from "./FormParts";
import { AlertIcon, CheckIcon, ChevronIcon, ClockIcon, LoopIcon, PlayIcon } from "./icons";

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
    <div className="space-y-3 rounded-lg p-3 ring-1 ring-inset ring-line" data-testid="clock-field">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 text-sm font-medium text-fg">
            <ClockIcon className="h-4 w-4 text-muted" /> Server clock in picture
            <Hint label="What the server clock does">The server's own time (NTP-synced) is drawn on every frame, top right:
              date, time and milliseconds. Compare it with the receiver's clock to measure delay, or to line up events
              with frames. The video itself is unchanged otherwise.</Hint>
          </div>
          <p className="mt-0.5 text-xs text-muted">Date, time and ms burned into every frame, for latency tests.</p>
        </div>
        <Segmented name={`${idPrefix}-clock-on`} ariaLabel="Server time as text on the frames" value={value ? "on" : "off"}
          onChange={(v) => onChange(v === "on", tz)}
          options={[{ value: "off", label: "Off" }, { value: "on", label: "On" }]} />
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className={`text-xs font-medium ${value ? "text-muted" : "text-subtle"}`}>Time standard</span>
        <Segmented size="sm" name={`${idPrefix}-clock-tz`} ariaLabel="Clock time zone" value={tz} disabled={!value}
          onChange={(z) => onChange(true, z)}
          options={[{ value: "IST", label: "IST (UTC+5:30)" }, { value: "UTC", label: "UTC" }]} />
      </div>
      <p id={`${idPrefix}-clock-help`} className={`text-xs ${value ? "text-amber-700 dark:text-amber-300" : "text-subtle"}`}>
        {value
          ? <>Re-encodes this stream live: about 1–2 CPU cores at 720p, 4 at 1080p. Counts toward the transcode limit
            {cap ? <> ({cap.transcoding} of {cap.max_transcode} in use)</> : null}.</>
          : "Off: the video is sent as it is, no CPU cost."}
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
      <div className="mb-1.5 text-sm font-medium text-fg">Picture size</div>
      <div className="flex flex-wrap items-center gap-2">
        <Segmented name={`${idPrefix}-out`} ariaLabel="Output size" value={extra.includes(value) ? "other" : value}
          onChange={(v) => v !== "other" && onChange(v)}
          options={[
            { value: "original", label: `Same as the file${orig && ` (${orig})`}` },
            { value: "standard", label: `${out.width}×${out.height}` },
            ...(extra.includes(value) ? [{ value: "other", label: value.replace("x", "×") }] : []),
          ]} />
        {isAdmin && extra.length > 0 && (
          <select className="input h-9 w-auto py-1 text-sm" aria-label="Other output size"
            value={extra.includes(value) ? value : ""} onChange={(e) => e.target.value && onChange(e.target.value)}>
            <option value="">Other size…</option>
            {extra.map((s) => <option key={s} value={s}>{s.replace("x", "×")}</option>)}
          </select>
        )}
      </div>
      <p className={`mt-1.5 text-xs ${plan === "live" ? "text-amber-700 dark:text-amber-300" : "text-muted"}`} data-testid="output-plan">
        {plan === "as-is" && "Streams the file as it is."}
        {plan === "copy" && "Uses the ready 720p copy of this video: no live transcoding."}
        {plan === "live" && "Scaled live on the server (about one CPU core per stream). Make a 720p copy in the library to stream it without that cost."}
      </p>
    </div>
  );
}

const LEAD_SHORT = (l: LeadInValue) => l.mode === "none" ? "none"
  : l.mode === "hold" ? `until a viewer, then ${l.seconds} s` : `${l.seconds} s slate`;

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
  const wide = useMediaQuery("(min-width: 1024px)");
  const formId = useId();

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
  const leadErr = lead.mode !== "none" ? leadInError(String(lead.seconds)) : null;
  const canSubmit = !!body && !nameError && !rangeError && !submitting && server?.ok === true && !leadErr;

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

  const title = template ? "Clone stream" : "New stream";
  if (ready.length === 0) {
    return (
      <Modal title={title} onClose={onClose} wide>
        <WarnBox>No usable videos in the library yet. Upload one or copy files into the server folder, then press Refresh.</WarnBox>
      </Modal>
    );
  }

  const url = addr.url(config.rtsp_url_template.replace("{name}", name));
  const plan = outputPlan(video, outputSize, config.output);
  const hasClock = !!config.features?.includes("clock_overlay");
  const sizeText = outputSize === "original"
    ? (video ? `${video.display_width ?? video.width}×${video.display_height ?? video.height} (as the file)` : "as the file")
    : outputSize === "standard" && config.output ? `${config.output.width}×${config.output.height}` : outputSize.replace("x", "×");
  const reencode = server?.ok ? server.output_mode !== "copy" : null;
  const tall = (video?.display_height ?? video?.height ?? 0) > 720;

  // what the footer says next to the button: why it is disabled, or that all is set
  const status: { tone: "muted" | "bad" | "ok"; text: string } =
    !video ? { tone: "muted", text: "Choose a video" }
      : nameError ? { tone: "bad", text: "Check the stream name" }
        : rangeError ? { tone: "bad", text: "Check the start / end time" }
          : leadErr ? { tone: "bad", text: "Check the lead-in seconds (Advanced)" }
            : checking || !server ? { tone: "muted", text: "Checking with the server…" }
              : !server.ok ? { tone: "bad", text: "The server found a problem (see Summary)" }
                : { tone: "ok", text: `Ready · starts at once${reencode ? " · re-encoded" : " · sent as it is"}` };

  const setStartAt = (t: number) => setStart(t <= 0 ? "" : formatTime(t, false));
  const setEndAt = (t: number) => setEnd(dur !== null && t >= dur - 0.5 ? "" : formatTime(t, false));

  const summary = (
    <aside aria-label="Summary" className="space-y-3">
      <h3 className="text-sm font-semibold text-fg">Summary</h3>
      {wide && video && <FrameAt videoId={video.id} t={slider} />}
      <dl className="divide-y divide-line rounded-lg bg-surface-2/60 px-3 ring-1 ring-inset ring-line">
        <SummaryRow label="Plays">
          <span className="font-mono">{formatTime(st.seconds ?? 0, false)} → {en.seconds !== null ? formatTime(en.seconds, false) : formatTime(dur, false)}</span>
        </SummaryRow>
        <SummaryRow label="Repeat">{mode === "loop" ? "Loop forever" : "Play once"} · 1×</SummaryRow>
        <SummaryRow label="Picture">{sizeText}</SummaryRow>
        {hasClock && <SummaryRow label="Clock">{clock.on ? `On · ${clock.tz}` : "Off"}</SummaryRow>}
        <SummaryRow label="Lead-in">{LEAD_SHORT(lead)}</SummaryRow>
        <SummaryRow label="Server CPU">
          {reencode === null ? <span className="text-muted">…</span>
            : reencode ? <span className="text-amber-700 dark:text-amber-300">{tall ? "about 3–4 cores" : "about 1–2 cores"}</span>
              : <span className="text-emerald-700 dark:text-emerald-300">almost none</span>}
        </SummaryRow>
      </dl>

      <div className="rounded-lg bg-slate-950 p-3">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">RTSP URL for the edge</div>
        <div className="mt-1 flex items-center gap-2">
          <span className="min-w-0 flex-1 break-all font-mono text-xs text-emerald-300">{url}</span>
          {!nameError && <CopyButton text={url} label="Copy" />}
        </div>
      </div>

      <div aria-live="polite" className="space-y-2">
        {checking && <div className="flex items-center gap-2 text-xs text-muted"><Spinner /> Checking with the server…</div>}
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

    </aside>
  );

  const footer = (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <p aria-live="polite" data-testid="create-status"
        className={`mr-auto flex min-w-0 items-center gap-1.5 text-sm ${status.tone === "bad" ? "text-red-600 dark:text-red-400"
          : status.tone === "ok" ? "text-emerald-700 dark:text-emerald-300" : "text-muted"}`}>
        {status.tone === "ok" ? <CheckIcon className="h-4 w-4 shrink-0" /> : status.tone === "bad" ? <AlertIcon className="h-4 w-4 shrink-0" /> : null}
        <span className="truncate">{status.text}</span>
      </p>
      <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
      <button type="submit" form={formId} className="btn-primary" disabled={!canSubmit}>
        {submitting ? <Spinner /> : <PlayIcon className="h-3.5 w-3.5" />} Create &amp; start
      </button>
    </div>
  );

  return (
    <Modal title={title} subtitle="Plays a library video on an RTSP URL, like a camera" onClose={onClose} size="xl" footer={footer}>
      <form id={formId} className="grid gap-x-8 gap-y-6 lg:grid-cols-[minmax(0,1fr)_21rem]"
        onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        <div className="min-w-0 space-y-7">
          <Section title="Video" aside={`${ready.length} in the library`}>
            <VideoPicker id="video" videos={ready} value={videoId}
              onChange={(id) => { setVideoId(id); setSlider(0); }}
              blocked={(v) => blockedShape(v, config.output) ? `not 16:9 (${SHAPE_TEXT[v.shape ?? "other"]})` : null} />
            {video && video.compat_mode === "transcode" && (
              <WarnBox>{video.compat_reasons.join(" ")} It will be transcoded to H.264 (CPU cost).</WarnBox>
            )}
          </Section>

          <Section title="Stream name">
            <div>
              <label className="sr-only" htmlFor="name">Stream name</label>
              <input id="name" className={`input font-mono ${nameError ? "input-error" : ""}`} value={name}
                aria-invalid={!!nameError} aria-describedby="name-help"
                onChange={(e) => { setName(e.target.value); setNameTouched(true); }} autoComplete="off" spellCheck={false} />
              <p id="name-help" className={`mt-1 text-xs ${nameError ? "text-red-600 dark:text-red-400" : "text-muted"}`}>
                {nameError ?? <>Becomes the end of the URL: <span className="font-mono">{url}</span></>}
              </p>
            </div>
          </Section>

          <Section title="What plays" aside={dur !== null ? `File length ${formatTime(dur, false)}` : undefined}>
            {video && dur !== null && (
              <div className="space-y-2">
                {!wide && <FrameAt videoId={video.id} t={slider} className="mx-auto max-w-sm" />}
                <Timeline videoId={video.id} duration={dur} start={st.seconds} end={en.seconds} playhead={slider}
                  onPlayhead={setSlider} onStart={setStartAt} onEnd={setEndAt} />
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="text-muted">Playhead <b className="font-mono text-fg">{formatTime(slider, false)}</b></span>
                  <button type="button" className="btn-secondary btn-xs" onClick={() => setStartAt(slider)}>Set as start</button>
                  <button type="button" className="btn-secondary btn-xs" onClick={() => setEndAt(slider)}>Set as end</button>
                  <span className="ml-auto text-subtle max-sm:hidden">Drag the blue handles, or type the times</span>
                </div>
              </div>
            )}
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label htmlFor="start" className="mb-1 block text-sm font-medium text-fg">Start time</label>
                <input id="start" className={`input font-mono ${st.error ? "input-error" : ""}`} placeholder="00:00:00"
                  value={start} onChange={(e) => setStart(e.target.value)} />
                <p className="mt-1 text-xs text-muted">{st.error ?? (st.seconds !== null ? `= ${formatTime(st.seconds)}` : "from the beginning")}</p>
              </div>
              <div>
                <label htmlFor="end" className="mb-1 block text-sm font-medium text-fg">End time</label>
                <input id="end" className={`input font-mono ${en.error ? "input-error" : ""}`} placeholder={dur !== null ? formatTime(dur, false) : "end of file"}
                  value={end} onChange={(e) => setEnd(e.target.value)} />
                <p className="mt-1 text-xs text-muted">{en.error ?? (en.seconds !== null ? `= ${formatTime(en.seconds)}` : "to the end of the file")}</p>
              </div>
            </div>
            {rangeError && !st.error && !en.error && <ErrorBox>{rangeError}</ErrorBox>}
            <div className="flex flex-wrap items-center gap-3">
              <Segmented name="create-mode" ariaLabel="Playback" value={mode} onChange={setMode}
                options={[{ value: "loop", label: <span className="inline-flex items-center gap-1.5"><LoopIcon className="h-3.5 w-3.5" /> Loop</span> },
                  { value: "once", label: "Play once" }]} />
              <span className="text-xs text-subtle">Speed 1× (fixed)</span>
            </div>
          </Section>

          <Section title="Output">
            <OutputSizeField out={config.output} video={video} value={outputSize} onChange={setOutputSize}
              isAdmin={!!isAdmin} idPrefix="create" />
            {plan !== "live" && <FastStartNotice video={video} checked={forceTranscode} onChange={setForceTranscode} />}
            <ClockField config={config} value={clock.on} tz={clock.tz} onChange={(on, tz) => setClock({ on, tz })} idPrefix="create" />
          </Section>

          <details className="group rounded-lg ring-1 ring-inset ring-line [&_summary::-webkit-details-marker]:hidden">
            <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 text-sm hover:bg-surface-2">
              <ChevronIcon className="h-4 w-4 text-muted transition-transform group-open:rotate-90" />
              <span className="font-medium text-fg">Advanced</span>
              <span className="truncate text-xs text-muted">Lead-in: {LEAD_SHORT(lead)} · {forceTranscode ? "always transcode" : "transcode only if needed"}</span>
            </summary>
            <div className="space-y-4 border-t border-line px-3 py-3">
              <div>
                <div className="mb-1 flex items-center gap-1.5 text-sm font-medium text-fg">Lead-in (before the video)
                  <Hint label="What the lead-in does">A short slate (black, the stream's name and a clock) published before
                    the video, so an edge that connects a little late still sees the video from its start.</Hint>
                </div>
                <LeadInFields value={lead} onChange={setLead} idPrefix="create-lead" />
              </div>
              <label className="flex items-start gap-2 text-sm">
                <input type="checkbox" className="mt-0.5" checked={forceTranscode} onChange={(e) => setForceTranscode(e.target.checked)} />
                <span>Always transcode to H.264 <span className="block text-xs text-muted">Normally only incompatible files are transcoded; this costs server CPU.</span></span>
              </label>
            </div>
          </details>

          {!wide && summary}
        </div>
        {wide && <div className="min-w-0"><div className="sticky top-4">{summary}</div></div>}
      </form>
    </Modal>
  );
}
