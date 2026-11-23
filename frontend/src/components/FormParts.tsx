/**
 * Building blocks for the stream forms (New stream, New stream from an analytics video): one look for sections,
 * choices, help and the video / time pickers. Presentation only: every value is owned by the form using them.
 */
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { formatTime } from "../lib/format";
import type { Video } from "../types/api";
import { FilmIcon, SearchIcon } from "./icons";

export function useMediaQuery(q: string): boolean {
  const get = () => typeof window !== "undefined" && !!window.matchMedia?.(q).matches;
  const [m, setM] = useState(get);
  useEffect(() => {
    const mq = window.matchMedia?.(q);
    if (!mq) return;
    const on = () => setM(mq.matches);
    on();
    mq.addEventListener?.("change", on);
    return () => mq.removeEventListener?.("change", on);
  }, [q]);
  return m;
}

/** A titled group of fields. */
export function Section({ title, aside, children, testId }: {
  title: string; aside?: ReactNode; children: ReactNode; testId?: string;
}) {
  return (
    <section className="space-y-3" data-testid={testId}>
      <div className="flex items-baseline justify-between gap-3 border-b border-line pb-1.5">
        <h3 className="text-sm font-semibold text-fg">{title}</h3>
        {aside && <div className="text-xs text-muted">{aside}</div>}
      </div>
      {children}
    </section>
  );
}

/** "?" next to a label: the explanation on hover, keyboard focus or tap. */
export function Hint({ children, label = "More information" }: { children: ReactNode; label?: string }) {
  return (
    <span className="group relative inline-flex align-middle">
      <button type="button" aria-label={label}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-line text-[10px] font-semibold leading-none text-muted hover:border-brand hover:text-brand focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40">
        ?
      </button>
      <span role="tooltip"
        className="pointer-events-none invisible absolute left-1/2 top-full z-30 mt-1.5 w-64 -translate-x-1/2 rounded-lg bg-slate-900 px-3 py-2 text-xs font-normal normal-case leading-relaxed tracking-normal text-slate-100 opacity-0 shadow-pop transition-opacity group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100">
        {children}
      </span>
    </span>
  );
}

export interface SegOption<T extends string> { value: T; label: ReactNode; sub?: ReactNode; disabled?: boolean }

/** One choice out of a few, as joined buttons (radio inputs underneath: arrow keys, screen readers, labels). */
export function Segmented<T extends string>({ name, value, options, onChange, ariaLabel, disabled, size = "md" }: {
  name: string; value: T; options: SegOption<T>[]; onChange: (v: T) => void; ariaLabel: string;
  disabled?: boolean; size?: "sm" | "md";
}) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} aria-disabled={disabled || undefined}
      className={`inline-flex max-w-full flex-wrap gap-1 rounded-lg bg-surface-2 p-1 ring-1 ring-inset ring-line ${disabled ? "opacity-60" : ""}`}>
      {options.map((o) => {
        const off = disabled || o.disabled;
        return (
          <label key={o.value} className={`relative ${off ? "cursor-not-allowed" : "cursor-pointer"}`}>
            <input type="radio" className="peer sr-only" name={name} checked={value === o.value} disabled={off}
              onChange={() => onChange(o.value)} />
            <span className={`flex flex-col rounded-md text-left transition-colors peer-focus-visible:ring-2 peer-focus-visible:ring-brand/50
              ${size === "sm" ? "px-2.5 py-1 text-xs" : "px-3 py-1.5 text-sm"}
              ${value === o.value ? "bg-surface font-medium text-fg shadow-sm ring-1 ring-line" : "text-muted hover:text-fg"}`}>
              <span>{o.label}</span>
              {o.sub && <span className="text-[11px] font-normal text-subtle">{o.sub}</span>}
            </span>
          </label>
        );
      })}
    </div>
  );
}

/** Small fact about a video: "00:01:00", "1280×720", "keyframe 10 s". */
export function Chip({ children, tone = "plain", title }: { children: ReactNode; tone?: "plain" | "ok" | "warn" | "bad"; title?: string }) {
  const c = {
    plain: "bg-surface-2 text-muted ring-line",
    ok: "bg-emerald-500/10 text-emerald-800 ring-emerald-500/30 dark:text-emerald-300",
    warn: "bg-amber-500/10 text-amber-800 ring-amber-500/30 dark:text-amber-300",
    bad: "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300",
  }[tone];
  return <span title={title} className={`inline-flex items-center whitespace-nowrap rounded px-1.5 py-px text-[11px] font-medium ring-1 ring-inset ${c}`}>{children}</span>;
}

export function videoChips(v: Video, blockedText?: string | null): ReactNode {
  const gop = v.keyframe_interval_s ?? 0;
  return (
    <>
      <Chip>{formatTime(v.duration_s, false)}</Chip>
      <Chip>{v.display_width ?? v.width}×{v.display_height ?? v.height}</Chip>
      {v.fps ? <Chip>{v.fps.toFixed(v.fps % 1 ? 2 : 0)} fps</Chip> : null}
      {v.video_codec && <Chip>{v.video_codec.toUpperCase()}</Chip>}
      {blockedText ? <Chip tone="bad">{blockedText}</Chip>
        : v.compat_mode === "transcode" ? <Chip tone="warn" title={v.compat_reasons.join(" ")}>needs transcode</Chip>
          : gop > 4 ? <Chip tone="warn" title="Keyframes far apart: slow start for new viewers">keyframe {gop.toFixed(0)} s</Chip>
            : v.b_frames ? <Chip tone="warn" title="B-frames: some edges show frames out of order">B-frames</Chip>
              : <Chip tone="ok" title="Streams as it is, with a quick start">camera-ready</Chip>}
    </>
  );
}

function Thumb({ v, className = "h-9 w-16" }: { v: Video; className?: string }) {
  return (
    <span className={`flex shrink-0 items-center justify-center overflow-hidden rounded bg-slate-800 ${className}`}>
      {v.thumbnail_url ? <img src={v.thumbnail_url} alt="" loading="lazy" className="h-full w-full object-cover" />
        : <FilmIcon className="h-4 w-4 text-slate-400" />}
    </span>
  );
}

/** Searchable video list: the chosen video shows its picture and facts; disabled videos say why. */
export function VideoPicker({ videos, value, onChange, blocked, id }: {
  videos: Video[]; value: number | null; onChange: (id: number) => void;
  blocked: (v: Video) => string | null; id: string;
}) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const box = useRef<HTMLDivElement>(null);
  const listId = useId();
  const current = videos.find((v) => v.id === value) ?? null;
  const shown = useMemo(() => {
    const t = q.trim().toLowerCase();
    return t ? videos.filter((v) => v.rel_path.toLowerCase().includes(t)) : videos;
  }, [videos, q]);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!box.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", away);
    setActive(Math.max(0, shown.findIndex((v) => v.id === value)));
    return () => document.removeEventListener("mousedown", away);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);
  useEffect(() => {
    if (open) document.getElementById(`${listId}-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [active, open, listId]);

  const pick = (v: Video | undefined) => {
    if (!v || blocked(v)) return;
    onChange(v.id);
    setOpen(false);
    setQ("");
  };

  return (
    <div className="relative" ref={box}>
      <button type="button" id={id} aria-haspopup="listbox" aria-expanded={open} onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-3 rounded-lg border border-line bg-surface p-2 text-left shadow-sm transition hover:border-brand/60 focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/30">
        {current ? (
          <>
            <Thumb v={current} className="h-12 w-20" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-fg">{current.rel_path}</span>
              <span className="mt-1 flex flex-wrap gap-1">{videoChips(current, blocked(current))}</span>
            </span>
          </>
        ) : <span className="flex-1 px-1 text-sm text-muted">Choose a video…</span>}
        <span className="shrink-0 px-1 text-xs font-medium text-brand">Change</span>
      </button>
      {open && (
        <div className="absolute inset-x-0 top-full z-30 mt-1 overflow-hidden rounded-lg border border-line bg-surface shadow-pop">
          <div className="flex items-center gap-2 border-b border-line px-3">
            <SearchIcon className="h-4 w-4 shrink-0 text-subtle" />
            <input autoFocus className="h-10 w-full bg-transparent text-sm text-fg placeholder:text-subtle focus:outline-none focus-visible:ring-0 focus-visible:ring-offset-0"
              placeholder={`Search ${videos.length} videos…`} value={q} aria-label="Search videos"
              role="combobox" aria-expanded aria-controls={listId} aria-activedescendant={shown[active] ? `${listId}-${active}` : undefined}
              onChange={(e) => { setQ(e.target.value); setActive(0); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(shown.length - 1, a + 1)); }
                else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
                else if (e.key === "Enter") { e.preventDefault(); pick(shown[active]); }
                else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setOpen(false); }
              }} />
          </div>
          <ul id={listId} role="listbox" aria-label="Videos" className="max-h-80 overflow-y-auto py-1">
            {shown.length === 0 && <li className="px-3 py-6 text-center text-sm text-muted">No video matches “{q}”.</li>}
            {shown.map((v, i) => {
              const why = blocked(v);
              return (
                <li key={v.id} id={`${listId}-${i}`} role="option" aria-selected={v.id === value} aria-disabled={!!why || undefined}
                  onMouseEnter={() => setActive(i)} onMouseDown={(e) => e.preventDefault()} onClick={() => pick(v)}
                  className={`flex items-center gap-3 px-3 py-2 ${why ? "cursor-not-allowed opacity-60" : "cursor-pointer"}
                    ${i === active ? "bg-surface-2" : ""} ${v.id === value ? "border-l-2 border-brand" : "border-l-2 border-transparent"}`}>
                  <Thumb v={v} />
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm text-fg">{v.rel_path}</span>
                    <span className="mt-0.5 flex flex-wrap gap-1">{videoChips(v, why)}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

/**
 * The part of the file that plays: a film strip with the chosen segment lit, start / end handles to drag
 * (arrow keys too), and the playhead whose frame is shown next to it.
 */
export function Timeline({ videoId, duration, start, end, playhead, onPlayhead, onStart, onEnd }: {
  videoId: number; duration: number; start: number | null; end: number | null; playhead: number;
  onPlayhead: (t: number) => void; onStart: (t: number) => void; onEnd: (t: number) => void;
}) {
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<"start" | "end" | "head" | null>(null);
  const n = 8;
  const thumbs = Array.from({ length: n }, (_, i) => (duration * (i + 0.5)) / n);
  const s = Math.min(Math.max(0, start ?? 0), duration);
  const e = Math.min(Math.max(s, end ?? duration), duration);
  const pct = (t: number) => `${(Math.min(Math.max(t, 0), duration) / duration) * 100}%`;
  const at = (clientX: number) => {
    const r = track.current?.getBoundingClientRect();
    if (!r || !(r.width > 0)) return null; // not laid out yet
    const t = ((clientX - r.left) / r.width) * duration;
    return Math.round(Math.min(Math.max(t, 0), duration));
  };
  const move = (clientX: number) => {
    const t = at(clientX);
    if (t === null) return;
    if (drag.current === "start") { const v = Math.min(t, e - 1); onStart(Math.max(0, v)); onPlayhead(Math.max(0, v)); }
    else if (drag.current === "end") { const v = Math.max(t, s + 1); onEnd(Math.min(duration, v)); onPlayhead(Math.min(duration, v) - 0.04); }
    else if (drag.current === "head") onPlayhead(Math.min(t, Math.max(0, duration - 0.04)));
  };
  const begin = (what: "start" | "end" | "head") => (ev: React.PointerEvent) => {
    ev.preventDefault();
    ev.stopPropagation();
    drag.current = what;
    if (what !== "head") (ev.currentTarget as HTMLElement).focus(); // then arrow keys fine-tune it
    track.current?.setPointerCapture?.(ev.pointerId);
    move(ev.clientX);
  };
  const keys = (what: "start" | "end") => (ev: React.KeyboardEvent) => {
    const step = ev.shiftKey ? 10 : 1;
    const d = ev.key === "ArrowLeft" || ev.key === "ArrowDown" ? -step : ev.key === "ArrowRight" || ev.key === "ArrowUp" ? step
      : ev.key === "Home" ? -duration : ev.key === "End" ? duration : 0;
    if (!d) return;
    ev.preventDefault();
    if (what === "start") { const v = Math.max(0, Math.min(s + d, e - 1)); onStart(v); onPlayhead(v); }
    else { const v = Math.min(duration, Math.max(e + d, s + 1)); onEnd(v); onPlayhead(Math.max(0, v - 0.04)); }
  };
  const handle = (what: "start" | "end", t: number) => (
    <div role="slider" tabIndex={0} aria-label={what === "start" ? "Start handle" : "End handle"}
      aria-valuemin={0} aria-valuemax={Math.round(duration)} aria-valuenow={Math.round(t)} aria-valuetext={formatTime(t, false)}
      onPointerDown={begin(what)} onKeyDown={keys(what)} title={`Drag to set the ${what} (arrow keys: 1 s, Shift: 10 s)`}
      className="group absolute inset-y-0 z-10 -ml-2 flex w-4 cursor-ew-resize touch-none items-center justify-center focus:outline-none"
      style={{ left: pct(t) }}>
      <span className="h-full w-1.5 rounded-full bg-brand shadow ring-2 ring-white group-focus-visible:ring-brand/50 dark:ring-slate-900" />
    </div>
  );
  return (
    <div className="select-none">
      <div ref={track} className="relative h-14 touch-none overflow-visible rounded-md bg-slate-800"
        onPointerDown={begin("head")}
        onPointerMove={(ev) => drag.current && move(ev.clientX)}
        onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}
        aria-label="Jump to a point in the video">
        <div className="absolute inset-0 flex overflow-hidden rounded-md">
          {thumbs.map((t) => (
            <img key={t} src={`/api/videos/${videoId}/frame?t=${t.toFixed(1)}`} alt="" loading="lazy" draggable={false}
              className="h-full min-w-0 flex-1 object-cover" />
          ))}
        </div>
        {/* outside the segment: dimmed */}
        <div className="pointer-events-none absolute inset-y-0 left-0 rounded-l-md bg-slate-950/70" style={{ width: pct(s) }} />
        <div className="pointer-events-none absolute inset-y-0 right-0 rounded-r-md bg-slate-950/70" style={{ left: pct(e) }} />
        <div className="pointer-events-none absolute inset-y-0 border-y-2 border-brand" style={{ left: pct(s), width: `calc(${pct(e)} - ${pct(s)})` }} />
        {/* playhead */}
        <div className="pointer-events-none absolute -inset-y-1 z-20 w-0.5 -translate-x-1/2 bg-white shadow-[0_0_0_1px_rgba(0,0,0,.5)]"
          style={{ left: pct(playhead) }} />
        {handle("start", s)}
        {handle("end", e)}
      </div>
      <div className="mt-1 flex justify-between font-mono text-[11px] text-subtle">
        <span>00:00:00</span><span>{formatTime(duration / 2, false)}</span><span>{formatTime(duration, false)}</span>
      </div>
    </div>
  );
}

/** A frame of the video at time t (debounced while scrubbing). */
export function FrameAt({ videoId, t, className = "" }: { videoId: number; t: number; className?: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    const h = setTimeout(() => { setSrc(`/api/videos/${videoId}/frame?t=${t.toFixed(2)}`); setLoaded(false); }, 200);
    return () => clearTimeout(h);
  }, [videoId, t]);
  return (
    <div className={`relative flex aspect-video items-center justify-center overflow-hidden rounded-lg bg-slate-900 ${className}`}>
      {src && <img src={src} alt={`Frame at ${formatTime(t, false)}`} onLoad={() => setLoaded(true)}
        className={`max-h-full max-w-full transition-opacity ${loaded ? "opacity-100" : "opacity-60"}`} />}
      <span className="absolute bottom-1.5 right-1.5 rounded bg-black/70 px-1.5 py-0.5 font-mono text-[11px] text-white">{formatTime(t, false)}</span>
    </div>
  );
}

/** Label: value row of the summary panel. */
export function SummaryRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5 text-sm">
      <dt className="shrink-0 text-muted">{label}</dt>
      <dd className="min-w-0 text-right font-medium text-fg">{children}</dd>
    </div>
  );
}
