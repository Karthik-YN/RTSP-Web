/**
 * Video clipper: watch a library video at any speed, mark segments on the fly, join them into a new
 * library video. Browser version of the desktop tool (video_clipper.py): the cut is done on the
 * server (POST /api/videos/{id}/clip), camera-style, so the result streams in copy mode.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../services/api";
import { useRig } from "../hooks/useRigData";
import { useCreateStream } from "../components/AppShell";
import { ErrorBox, Spinner, WarnBox } from "../components/common";
import { formatDuration, formatTime } from "../lib/format";
import type { Conversion } from "../types/api";

type Seg = [number, number];

export const SPEEDS = [0.25, 0.5, 1, 1.5, 2, 3, 4, 6, 8, 16, 32, 64, 72];
const NATIVE_MAX = 16; // browsers play up to 16x; faster = 16x plus forward jumps
const TURBO_TICK_MS = 250;
const MIN_SEG = 0.2;

/** Clean, ordered-as-given segments with the open clip closed at ``now``. Exported for tests. */
export function closeClip(segs: Seg[], openStart: number, now: number): Seg[] {
  const a = Math.min(openStart, now), b = Math.max(openStart, now);
  return b - a >= MIN_SEG ? [...segs, [a, b]] : segs;
}

export const totalOf = (segs: Seg[]) => segs.reduce((n, [a, b]) => n + (b - a), 0);

const storeKey = (id: number) => `rig02.clipper.${id}`;
function loadSegs(id: number): Seg[] {
  try {
    const raw = JSON.parse(localStorage.getItem(storeKey(id)) ?? "[]");
    return Array.isArray(raw) ? raw.filter((s) => Array.isArray(s) && s.length === 2 && s.every(Number.isFinite)) : [];
  } catch { return []; }
}
function saveSegs(id: number, segs: Seg[]) {
  try { localStorage.setItem(storeKey(id), JSON.stringify(segs)); } catch { /* private mode: not kept */ }
}

export function ClipperPage() {
  const id = Number(useParams().id);
  const [params] = useSearchParams();
  const { videos, user, refreshVideos } = useRig();
  const openCreate = useCreateStream();
  const video = videos?.find((v) => v.id === id) ?? null;

  const ref = useRef<HTMLVideoElement>(null);
  const [now, setNow] = useState(0);
  const [dur, setDur] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [segs, setSegs] = useState<Seg[]>(() => {
    const kept = loadSegs(id);
    const s = Number(params.get("start")), e = Number(params.get("end"));
    // the dialog's start/end become the first segment (unless segments were kept from an earlier visit)
    return kept.length === 0 && params.has("start") && params.has("end") && e - s >= MIN_SEG ? [[s, e]] : kept;
  });
  const [openStart, setOpenStart] = useState<number | null>(null);
  const [sel, setSel] = useState<number | null>(null);
  const [name, setName] = useState("");
  const [job, setJob] = useState<Conversion | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState(false);
  const seeded = useRef(false);

  useEffect(() => { document.title = `Clipper · ${video?.filename ?? "video"}`; }, [video?.filename]);
  useEffect(() => saveSegs(id, segs), [id, segs]);
  useEffect(() => {
    api.clipName(id).then((r) => setName((n) => n || r.name)).catch(() => {});
  }, [id, job?.state === "done"]); // eslint-disable-line react-hooks/exhaustive-deps

  const fps = video?.fps && video.fps > 0 ? video.fps : 25;
  const v = () => ref.current;

  // ---------------------------------------------------------------- playback
  const seek = useCallback((t: number) => {
    const el = v(); if (!el) return;
    el.currentTime = Math.max(0, Math.min(t, (el.duration || dur) - 0.01));
    setNow(el.currentTime);
  }, [dur]);
  const toggle = useCallback(() => { const el = v(); if (!el) return; if (el.paused) void el.play(); else el.pause(); }, []);
  const step = (frames: number) => { const el = v(); if (!el) return; el.pause(); seek(el.currentTime + frames / fps); };

  useEffect(() => { const el = v(); if (el) el.playbackRate = Math.min(speed, NATIVE_MAX); }, [speed]);
  useEffect(() => { // above 16x: catch up with jumps
    if (!playing || speed <= NATIVE_MAX) return;
    const t = setInterval(() => { const el = v(); if (el) el.currentTime += (speed - NATIVE_MAX) * TURBO_TICK_MS / 1000; }, TURBO_TICK_MS);
    return () => clearInterval(t);
  }, [playing, speed]);
  useEffect(() => { // smooth clock for the timeline while playing
    if (!playing) return;
    let raf = 0;
    const tick = () => { const el = v(); if (el) setNow(el.currentTime); raf = requestAnimationFrame(tick); };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing]);

  const onLoaded = () => {
    const el = v(); if (!el) return;
    setDur(el.duration || video?.duration_s || 0);
    el.playbackRate = Math.min(speed, NATIVE_MAX);
    if (seeded.current) return;
    seeded.current = true;
    const s = Number(params.get("start"));
    if (params.has("start") && Number.isFinite(s)) seek(s);
  };

  // ---------------------------------------------------------------- segments
  const toggleClip = useCallback(() => {
    const t = v()?.currentTime ?? now;
    if (openStart === null) { setOpenStart(t); return; }
    setSegs((s) => { const next = closeClip(s, openStart, t); if (next !== s) setSel(next.length - 1); return next; });
    setOpenStart(null);
  }, [openStart, now]);
  const removeSel = () => { if (sel === null) return; setSegs((s) => s.filter((_, i) => i !== sel)); setSel(null); };
  const move = (d: -1 | 1) => {
    if (sel === null) return;
    const j = sel + d; if (j < 0 || j >= segs.length) return;
    setSegs((s) => { const n = [...s]; [n[sel], n[j]] = [n[j], n[sel]]; return n; }); setSel(j);
  };
  const nudge = (which: 0 | 1) => {
    if (sel === null) return;
    const t = v()?.currentTime ?? now;
    setSegs((s) => s.map((g, i) => {
      if (i !== sel) return g;
      const n: Seg = [...g]; n[which] = t;
      return n[1] - n[0] >= MIN_SEG ? n : g;
    }));
  };

  // ---------------------------------------------------------------- keyboard (same keys as the desktop tool)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.ctrlKey || e.metaKey || e.altKey) return;
      const el = v(); if (!el) return;
      const k = e.key;
      const idx = SPEEDS.indexOf(speed);
      const map: Record<string, () => void> = {
        " ": toggle, c: toggleClip, C: toggleClip,
        ArrowLeft: () => seek(el.currentTime - (e.shiftKey ? 30 : 5)),
        ArrowRight: () => seek(el.currentTime + (e.shiftKey ? 30 : 5)),
        ",": () => step(-1), ".": () => step(1),
        "[": () => setSpeed(SPEEDS[Math.max(0, idx - 1)]), "]": () => setSpeed(SPEEDS[Math.min(SPEEDS.length - 1, idx + 1)]),
        "1": () => setSpeed(1), "2": () => setSpeed(2), "4": () => setSpeed(4), "8": () => setSpeed(8),
        "3": () => setSpeed(32), "6": () => setSpeed(64), "7": () => setSpeed(72),
        Delete: removeSel,
      };
      const fn = map[k];
      if (fn) { e.preventDefault(); fn(); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }); // re-bound every render: handlers read fresh state

  // ---------------------------------------------------------------- export
  const running = job !== null && (job.state === "queued" || job.state === "running");
  useEffect(() => {
    if (!running || !job) return;
    const t = setInterval(async () => {
      try {
        const j = (await api.conversions()).find((x) => x.id === job.id);
        if (!j) return;
        setJob(j);
        if (j.state === "done") refreshVideos();
        if (j.state === "failed") setError(j.error ?? "The clip could not be made.");
      } catch { /* next tick */ }
    }, 1000);
    return () => clearInterval(t);
  }, [running, job?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = async () => {
    setError(null);
    try {
      setJob(await api.clipVideo(id, segs, name.trim()));
    } catch (e) { setError(e instanceof ApiError ? e.message : String(e)); }
  };
  const cancel = async () => { if (job) { try { setJob(await api.cancelConversion(job.id)); } catch { /* gone */ } } };

  const total = useMemo(() => totalOf(segs), [segs]);
  if (!Number.isFinite(id)) return <div className="p-6"><ErrorBox>No video selected.</ErrorBox></div>;
  if (videos === null) return <div className="p-6"><Spinner /></div>;
  if (!video) return <div className="p-6"><ErrorBox>Video {id} is not in the library.</ErrorBox></div>;

  const D = dur || video.duration_s || 0; // file length from the library until the browser has loaded it
  const pct = (t: number) => (D > 0 ? `${(100 * t) / D}%` : "0%");
  const canWrite = user.can_write;

  return (
    <div className="mx-auto max-w-[100rem] space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h1 className="text-xl font-semibold text-fg">✂ Clipper</h1>
        <span className="font-mono text-sm text-muted">{video.filename}</span>
        <span className="text-xs text-subtle">{formatDuration(video.duration_s)} · {video.width}×{video.height}{video.fps ? ` · ${video.fps_text ?? video.fps} fps` : ""}</span>
      </div>

      {video.browser_playable === false && (
        <WarnBox>
          This browser can't play this file ({video.browser_note ?? "codec not supported"}). Make an H.264 copy in the{" "}
          <Link className="underline" to="/library">Library</Link> (⋯ → Convert) and clip the copy.
        </WarnBox>
      )}

      <div className="grid gap-4 xl:grid-cols-[1fr_22rem]">
        {/* ---------------- player ---------------- */}
        <section className="space-y-3">
          <div className="overflow-hidden rounded-lg bg-black">
            <video ref={ref} src={`/api/videos/${id}/file`} className="mx-auto max-h-[65vh] w-full" preload="auto" playsInline
              onLoadedMetadata={onLoaded} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)}
              onSeeked={() => setNow(v()?.currentTime ?? 0)} onTimeUpdate={() => !playing && setNow(v()?.currentTime ?? 0)}
              onError={() => setLoadError(true)} onClick={toggle} />
          </div>
          {loadError && <ErrorBox>The video could not be loaded in the browser.</ErrorBox>}

          {/* timeline: click to seek; green = segments, red = clip being recorded */}
          <div role="slider" aria-label="Timeline" aria-valuemin={0} aria-valuemax={Math.round(dur)} aria-valuenow={Math.round(now)}
            tabIndex={-1} data-testid="timeline"
            className="relative h-8 cursor-pointer rounded-md bg-surface-2 ring-1 ring-inset ring-line"
            onPointerDown={(e) => {
              const r = e.currentTarget.getBoundingClientRect();
              seek(((e.clientX - r.left) / r.width) * dur);
            }}>
            {segs.map(([a, b], i) => (
              <div key={i} className={`absolute inset-y-1 rounded-sm ${i === sel ? "bg-emerald-400" : "bg-emerald-500/70"}`}
                style={{ left: pct(a), width: `max(2px, ${pct(b - a)})` }} />
            ))}
            {openStart !== null && (
              <div className="absolute inset-y-1 rounded-sm bg-red-500/80"
                style={{ left: pct(Math.min(openStart, now)), width: `max(2px, ${pct(Math.abs(now - openStart))})` }} />
            )}
            <div className="absolute inset-y-0 w-0.5 bg-brand" style={{ left: pct(now) }} />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button className="btn-secondary" onClick={() => seek(now - 10)}>⏪ 10s</button>
            <button className="btn-primary min-w-[5.5rem]" onClick={toggle}>{playing ? "⏸ Pause" : "▶ Play"}</button>
            <button className="btn-secondary" onClick={() => seek(now + 10)}>10s ⏩</button>
            <span className="font-mono text-sm text-fg">{formatTime(now)} / {formatTime(dur, false)}</span>
            <div className="ml-auto flex flex-wrap items-center gap-1" role="group" aria-label="Speed">
              {[1, 2, 4, 8, 16, 32, 64, 72].map((s) => (
                <button key={s} className={`btn-xs ${speed === s ? "btn-primary" : "btn-secondary"}`} onClick={() => setSpeed(s)}>{s}x</button>
              ))}
              <select className="input w-auto py-1 text-xs" aria-label="Exact speed" value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
                {SPEEDS.map((s) => <option key={s} value={s}>{s}x</option>)}
              </select>
            </div>
            <button data-testid="clip-toggle"
              className={`${openStart === null ? "btn-secondary" : "btn-danger"} min-w-[10rem]`} onClick={toggleClip}>
              {openStart === null ? "● Start clip (C)" : `■ Stop clip (C) · ${formatTime(Math.abs(now - openStart), false)}`}
            </button>
          </div>
          <p className="text-xs text-subtle">
            Space play/pause · C start/stop clip · ←/→ 5 s · Shift+←/→ 30 s · , / . one frame · [ / ] slower/faster ·
            1/2/4/8 speed · 3/6/7 = 32x/64x/72x · Del remove segment
          </p>
        </section>

        {/* ---------------- segments + save ---------------- */}
        <aside className="space-y-3">
          <div className="flex items-baseline justify-between">
            <h2 className="text-sm font-semibold text-fg">Segments ({segs.length})</h2>
            <span className="text-xs text-muted">total {formatTime(total, false)}</span>
          </div>
          <ol className="max-h-72 space-y-1 overflow-auto rounded-lg bg-surface-2/60 p-1 ring-1 ring-inset ring-line" data-testid="segments">
            {segs.length === 0 && <li className="px-2 py-3 text-xs text-muted">Play the video and press <b>C</b> at the start and end of each part you want.</li>}
            {segs.map(([a, b], i) => (
              <li key={i}>
                <button className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left font-mono text-xs ${i === sel ? "bg-brand-soft text-fg" : "text-muted hover:bg-surface"}`}
                  onClick={() => { setSel(i); seek(a); }}>
                  <span className="w-5 text-subtle">{i + 1}.</span>
                  <span>{formatTime(a)} → {formatTime(b)}</span>
                  <span className="ml-auto">{formatTime(b - a, false)}</span>
                </button>
              </li>
            ))}
          </ol>
          <div className="flex flex-wrap gap-1">
            <button className="btn-secondary btn-xs" disabled={sel === null} onClick={() => move(-1)}>↑ Up</button>
            <button className="btn-secondary btn-xs" disabled={sel === null} onClick={() => move(1)}>↓ Down</button>
            <button className="btn-secondary btn-xs" disabled={sel === null} onClick={() => nudge(0)} title="Set the selected segment's start to the playhead">Start = now</button>
            <button className="btn-secondary btn-xs" disabled={sel === null} onClick={() => nudge(1)} title="Set the selected segment's end to the playhead">End = now</button>
            <button className="btn-secondary btn-xs" disabled={sel === null} onClick={removeSel}>Remove</button>
            <button className="btn-secondary btn-xs" disabled={segs.length < 2} onClick={() => setSegs((s) => [...s].sort((x, y) => x[0] - y[0]))}>Sort by time</button>
            <button className="btn-secondary btn-xs" disabled={segs.length === 0}
              onClick={() => { if (confirm("Remove all segments?")) { setSegs([]); setSel(null); } }}>Clear</button>
          </div>
          <p className="text-xs text-subtle">Segments are joined in this order. They are kept in this browser if you close the tab.</p>

          <div className="space-y-2 rounded-lg p-3 ring-1 ring-inset ring-line">
            <label htmlFor="clip-name" className="block text-sm font-medium text-fg">Save as (library file name)</label>
            <input id="clip-name" className="input font-mono" value={name} onChange={(e) => setName(e.target.value)}
              placeholder="ALPR_toll-s1_02.mp4" disabled={running} />
            <p className="text-xs text-subtle">
              Frame-exact cuts, re-encoded like a camera (H.264, no B-frames, keyframe every 2 s, no audio), so it streams without extra CPU.
            </p>
            {!canWrite && <WarnBox>Your account can't add videos.</WarnBox>}
            {error && <ErrorBox>{error}</ErrorBox>}
            {running && job ? (
              <div className="space-y-1" aria-live="polite">
                <div className="h-2 overflow-hidden rounded bg-surface-2"><div className="h-full bg-brand transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} /></div>
                <div className="flex items-center gap-2 text-xs text-muted">
                  <Spinner /> {job.state === "queued" ? "Waiting for a free converter…" : `Making the clip… ${Math.round(job.progress * 100)}%`}
                  {job.eta_s !== null && <span>· about {formatDuration(job.eta_s)} left</span>}
                  <button className="btn-ghost btn-xs ml-auto" onClick={cancel}>Cancel</button>
                </div>
              </div>
            ) : (
              <button className="btn-primary w-full" data-testid="clip-save"
                disabled={!canWrite || segs.length === 0 || !name.trim() || openStart !== null} onClick={save}>
                ⬇ Join {segs.length > 1 ? `${segs.length} segments ` : ""}and save to library
              </button>
            )}
            {openStart !== null && <p className="text-xs text-amber-700 dark:text-amber-300">Stop the clip being recorded first (C).</p>}
            {job?.state === "done" && job.result_video_id !== null && (
              <div className="space-y-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 p-2 text-sm text-emerald-900 dark:text-emerald-200" data-testid="clip-done">
                <div>Saved <b className="font-mono">{job.target.split("/").pop()}</b> to the library.</div>
                <div className="flex flex-wrap gap-2">
                  <button className="btn-primary btn-xs" onClick={() => openCreate(job.result_video_id!)}>New stream with this clip</button>
                  <Link className="btn-secondary btn-xs" to="/library">Open library</Link>
                </div>
                <p className="text-xs">It is also in the video list of the New stream dialog in your other tab.</p>
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
