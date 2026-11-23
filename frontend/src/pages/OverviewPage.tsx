import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useRig } from "../hooks/useRigData";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { api, ApiError } from "../services/api";
import type { AnalyticsItem, Stream, Video } from "../types/api";
import { copyText, MoreMenu, Skeleton, useToast } from "../components/common";
import { isStreamLive, StreamSetControls } from "../components/BulkControls";
import { NewAnalyticsDialog } from "../components/AppShell";
import { codeTone } from "../components/Naming";
import { isProblem } from "../components/StreamsTable";
import { AlertIcon, CameraIcon, ChevronIcon, CopyIcon, DownloadIcon, EyeIcon, FilmIcon, PlusIcon, SearchIcon, ServerIcon } from "../components/icons";
import { buildEdgeCsv, downloadText, edgeCsvFilename } from "../lib/edge";

/** Download a CSV of these streams with what an edge needs (URL, resolution, fps, codec, keyframes). */
/** The ⋯ menu of an analytics' streams: copy their RTSP URLs, or download a CSV for setting up an edge. */
export function StreamsMenu({ streams, name, code, analyticsId }: {
  streams: Stream[]; name: string; code?: string | null; analyticsId?: number;
}) {
  const { videos } = useRig();
  const addr = useRtspAddress();
  const toast = useToast();
  if (!streams.length) return null;
  const copyUrls = async () => {
    try {
      const text = analyticsId != null
        ? await api.analyticsUrls(analyticsId, "txt", addr.param)
        : streams.map((s) => addr.url(s.rtsp_url)).join("\n") + "\n";
      if (await copyText(text)) toast("ok", `Copied ${text.trim().split("\n").filter(Boolean).length} RTSP URLs of ${name}.`);
    } catch (e) { toast("bad", (e as ApiError).message); }
  };
  const save = () => {
    try {
      downloadText(edgeCsvFilename(name, code), buildEdgeCsv(streams, videos, (s) => addr.url(s.rtsp_url)));
    } catch (e) { toast("bad", `Could not save the CSV: ${(e as Error).message}`); }
  };
  return (
    <MoreMenu label={`More for ${name}`} items={[
      { label: "Copy RTSP URLs", icon: <CopyIcon className="h-4 w-4" />, onClick: copyUrls,
        title: "Every RTSP URL of these streams, one per line" },
      { label: `Export ${name} for edge`, icon: <DownloadIcon className="h-4 w-4" />, onClick: save,
        title: "CSV for edge setup: URL, status, resolution, fps, codec, keyframe interval, video" },
    ]} />
  );
}

const FOLDED_KEY = "rig02.overview.folded";
const NONE = -1; // the "Not in any analytics" section

/**
 * The main page: every analytics, grouped by category, with its videos, live streams and fast start/stop.
 * Built only from what the dashboard already polls (analytics tree, streams, videos).
 */
export function OverviewPage() {
  const { analytics, streams, videos, user, refreshAnalytics } = useRig();
  const toast = useToast();
  const [q, setQ] = useState("");
  const [cat, setCat] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const [folded, setFolded] = useState<number[]>(() => {
    try { return JSON.parse(localStorage.getItem(FOLDED_KEY) ?? `[${NONE}]`); } catch { return [NONE]; }
  });
  const searchRef = useRef<HTMLInputElement>(null);
  useEffect(() => { document.title = "Overview · RTSP Studio"; }, []);
  // "/" jumps to the search (unless typing in a field already)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
      if (t && (t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName))) return;
      e.preventDefault();
      searchRef.current?.focus();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const toggle = (id: number) => {
    const next = folded.includes(id) ? folded.filter((x) => x !== id) : [...folded, id];
    setFolded(next);
    try { localStorage.setItem(FOLDED_KEY, JSON.stringify(next)); } catch { /* private mode */ }
  };

  // who belongs where: stream -> analytics (by slot), video -> analytics (from the library)
  const index = useMemo(() => {
    const slotOwner = new Map<number, number>();
    for (const c of analytics ?? []) for (const a of c.analytics) for (const sid of a.slot_ids) slotOwner.set(sid, a.id);
    const streamsOf = new Map<number, Stream[]>();
    const loose: Stream[] = [];
    for (const s of streams ?? []) {
      const aid = s.slot_id != null ? slotOwner.get(s.slot_id) : undefined;
      if (aid === undefined) loose.push(s);
      else streamsOf.set(aid, [...(streamsOf.get(aid) ?? []), s]);
    }
    const videosOf = new Map<number, Video[]>();
    const looseVideos: Video[] = [];
    for (const v of videos ?? []) {
      if (!v.analytics?.length) looseVideos.push(v);
      for (const a of v.analytics ?? []) {
        const list = videosOf.get(a.id) ?? [];
        if (!list.includes(v)) videosOf.set(a.id, [...list, v]);
      }
    }
    return { streamsOf, videosOf, loose, looseVideos };
  }, [analytics, streams, videos]);

  const needle = q.trim().toLowerCase();
  const matches = (a: AnalyticsItem, catName: string) => !needle
    || [a.name, a.code ?? "", a.description ?? "", catName].some((x) => x.toLowerCase().includes(needle))
    || (index.videosOf.get(a.id) ?? []).some((v) => v.filename.toLowerCase().includes(needle))
    || (index.streamsOf.get(a.id) ?? []).some((s) => s.name.toLowerCase().includes(needle));
  const cats = (analytics ?? [])
    .filter((c) => cat === null || c.id === cat)
    .map((c) => ({ ...c, analytics: c.analytics.filter((a) => matches(a, c.name)) }))
    .filter((c) => !needle || c.analytics.length);


  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Overview</h1>
          <p className="text-sm text-muted">Every analytics with its videos and live streams. Open one to manage it.</p>
        </div>
        {user.can_write && (
          <button className="btn-secondary" onClick={() => setAdding(true)}><PlusIcon /> New analytics</button>
        )}
      </div>

      <OverviewStats />

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-sm">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-subtle" />
          <input ref={searchRef} className="input pl-8" placeholder="Find analytics, video or stream  ( / )" value={q}
            onChange={(e) => setQ(e.target.value)} aria-label="Find analytics, video or stream" />
        </div>
        <div className="flex flex-wrap gap-1 rounded-lg bg-surface-2 p-1 ring-1 ring-inset ring-line" role="tablist" aria-label="Category">
          {[{ id: null as number | null, name: "All" }, ...(analytics ?? []).map((c) => ({ id: c.id as number | null, name: c.name }))].map((c) => (
            <button key={c.name} role="tab" aria-selected={cat === c.id} onClick={() => setCat(c.id)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${cat === c.id ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg"}`}>
              {c.name}
            </button>
          ))}
        </div>
      </div>

      {!analytics || !videos || !streams ? (
        // everything a section needs must be there before drawing it, or tiles jump as data arrives
        <div className="space-y-3">{[0, 1].map((i) => <Skeleton key={i} className="h-48" />)}</div>
      ) : (
        <>
          {!cats.length && <div className="card p-8 text-center text-sm text-muted">No analytics match "{q}".</div>}
          {cats.map((c) => {
            const busy = c.analytics.filter((a) => (index.videosOf.get(a.id)?.length ?? 0) > 0 || (index.streamsOf.get(a.id)?.length ?? 0) > 0);
            const empty = c.analytics.filter((a) => !busy.includes(a));
            const catStreams = c.analytics.flatMap((a) => index.streamsOf.get(a.id) ?? []);
            return (
              <Section key={c.id} id={c.id} title={c.name} open={!!needle || !folded.includes(c.id)} onToggle={() => toggle(c.id)}
                streams={catStreams} label={`all streams of ${c.name}`}>
                {busy.length > 0 && (
                  <div className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3">
                    {busy.map((a) => (
                      <AnalyticsTile key={a.id} a={a} streams={index.streamsOf.get(a.id) ?? []} videos={index.videosOf.get(a.id) ?? []} />
                    ))}
                  </div>
                )}
                {empty.length > 0 && <EmptyLine items={empty} />}
              </Section>
            );
          })}
          {cat === null && !needle && (index.loose.length > 0 || index.looseVideos.length > 0) && (
            <Section id={NONE} title="Not in any analytics" open={!folded.includes(NONE)} onToggle={() => toggle(NONE)}
              streams={index.loose} label="the streams not in any analytics"
              note={`${index.looseVideos.length} videos, ${index.loose.length} streams`}>
              <Unassigned streams={index.loose} videos={index.looseVideos} />
            </Section>
          )}
        </>
      )}

      {adding && analytics && (
        <NewAnalyticsDialog categories={analytics} onClose={() => setAdding(false)}
          onCreated={() => { setAdding(false); refreshAnalytics(); toast("ok", "Analytics added."); }} />
      )}
    </div>
  );
}

function Stat({ icon, tone, label, value, sub, to }: {
  icon: ReactNode; tone: string; label: string; value: string | null; sub: string; to?: string;
}) {
  const body = (
    <>
      <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${tone}`}>{icon}</span>
      <div className="min-w-0">
        <div className="text-xs font-medium text-muted">{label}</div>
        {value === null ? <Skeleton className="mt-1 h-6 w-12" /> : <div className="text-2xl font-bold tabular-nums leading-tight">{value}</div>}
        <div className="truncate text-xs text-subtle">{sub}</div>
      </div>
    </>
  );
  return to
    ? <Link to={to} className="card flex items-center gap-3 p-4 transition hover:shadow-pop">{body}</Link>
    : <div className="card flex items-center gap-3 p-4">{body}</div>;
}

function Section({ id, title, open, onToggle, streams, label, note, children }: {
  id: number; title: string; open: boolean; onToggle: () => void; streams: Stream[]; label: string; note?: string; children: ReactNode;
}) {
  const live = streams.filter(isStreamLive).length;
  const problems = streams.filter(isProblem).length;
  return (
    <section aria-labelledby={`sec-${id}`} className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line pb-2">
        <button id={`sec-${id}`} className="flex items-center gap-1.5 text-lg font-semibold" onClick={onToggle} aria-expanded={open}>
          <ChevronIcon className={`h-4 w-4 text-muted transition-transform ${open ? "rotate-90" : ""}`} />{title}
        </button>
        <span className="text-sm text-muted">
          {note ?? (streams.length ? <><b className={live ? "text-emerald-700 dark:text-emerald-400" : "text-fg"}>{live}</b> live of {streams.length} streams</> : "no streams yet")}
        </span>
        {problems > 0 && <span className="rounded-full bg-red-500 px-2 text-xs font-semibold text-white" title="Streams that need attention">{problems} need attention</span>}
        <div className="ml-auto"><StreamSetControls streams={streams} label={label} /></div>
      </div>
      {open && children}
    </section>
  );
}

/** The 4 numbers at the top; each opens the matching view. Its own component: CPU/capacity updates redraw only it. */
function OverviewStats() {
  const { streams, system } = useRig();
  const all = streams ?? [];
  const live = all.filter((s) => s.status === "RUNNING").length;
  const problems = all.filter(isProblem).length;
  const cap = system?.streams;
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Summary">
      <Stat icon={<CameraIcon />} tone="text-emerald-700 bg-emerald-500/10 dark:text-emerald-400" label="Live"
        value={streams ? `${live}` : null} sub={`of ${all.length} streams`} to="/streams?filter=live" />
      <Stat icon={<AlertIcon />} tone={problems ? "text-red-600 bg-red-500/10 dark:text-red-400" : "text-muted bg-slate-500/10"}
        label="Need attention" value={streams ? `${problems}` : null} sub={problems ? "open them" : "all good"} to="/streams?filter=problems" />
      <Stat icon={<EyeIcon />} tone="text-blue-600 bg-blue-500/10 dark:text-blue-400" label="Viewers"
        value={system ? `${system.viewers ?? "?"}` : null} sub="edges and testers reading" to="/streams?filter=live" />
      <Stat icon={<ServerIcon />} tone="text-violet-600 bg-violet-500/10 dark:text-violet-400" label="Capacity"
        value={cap ? `${cap.desired_running}/${cap.max_active}` : null} sub={cap ? `${cap.transcoding}/${cap.max_transcode} transcoding` : ""} to="/system?tab=settings" />
    </div>
  );
}

/** One analytics: name and code, counts, up to 4 thumbnails, and the actions people use most. */
export function AnalyticsTile({ a, streams, videos }: { a: AnalyticsItem; streams: Stream[]; videos: Video[] }) {
  const live = streams.filter(isStreamLive).length;
  const viewers = streams.reduce((n, s) => n + (s.runtime?.viewer_count ?? 0), 0);
  const problems = streams.filter(isProblem).length;
  const thumbs = videos.slice(0, 4);
  return (
    <article className={`card flex flex-col gap-3 p-4 ${problems ? "ring-1 ring-red-500/40" : ""}`} aria-label={`Analytics ${a.name}`}>
      <div className="flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <Link to={`/analytics/${a.id}`} className="truncate font-semibold hover:text-brand hover:underline">{a.name}</Link>
            {a.code && <span className={`rounded px-1.5 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset ${codeTone(a.code)}`}>{a.code}</span>}
            {problems > 0 && <span className="h-2 w-2 rounded-full bg-red-500" title={`${problems} stream(s) need attention`} />}
          </div>
          {a.description && <p className="mt-0.5 line-clamp-1 text-xs text-muted" title={a.description}>{a.description}</p>}
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        {streams.length
          ? <span><span className={`mr-1 inline-block h-2 w-2 rounded-full ${live ? "bg-emerald-500" : "bg-slate-400"}`} />
              <b className="tabular-nums">{live}/{streams.length}</b> live</span>
          : <span className="text-muted">no streams</span>}
        {viewers > 0 && <span className="flex items-center gap-1 text-muted" title="Readers now (edges, testers)"><EyeIcon className="h-3.5 w-3.5" />{viewers}</span>}
        <span className="flex items-center gap-1 text-muted"><FilmIcon className="h-3.5 w-3.5" />{videos.length} video{videos.length === 1 ? "" : "s"}</span>
      </div>
      {thumbs.length > 0 && (
        <Link to={`/analytics/${a.id}`} className="grid grid-cols-4 gap-1.5" aria-label={`Open ${a.name}`} tabIndex={-1}>
          {thumbs.map((v, i) => (
            <span key={v.id} className="relative aspect-video overflow-hidden rounded-md bg-slate-800" title={v.filename}>
              {v.thumbnail_url && <img src={v.thumbnail_url} alt="" loading="lazy" className="h-full w-full object-cover" />}
              {i === 3 && videos.length > 4 && (
                <span className="absolute inset-0 flex items-center justify-center bg-black/55 text-sm font-semibold text-white">+{videos.length - 3}</span>
              )}
            </span>
          ))}
        </Link>
      )}
      <div className="mt-auto flex flex-wrap items-center gap-1.5">
        <Link to={`/analytics/${a.id}`} className="btn-primary btn-xs">Open</Link>
        <StreamSetControls streams={streams} label={`the streams of ${a.name}`} />
        <span className="ml-auto"><StreamsMenu streams={streams} name={a.name} code={a.code} analyticsId={a.id} /></span>
      </div>
    </article>
  );
}

function EmptyLine({ items }: { items: AnalyticsItem[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="text-sm text-muted">
      <button className="hover:text-fg" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        + {items.length} empty analytics{open ? "" : `: ${items.map((a) => a.name).join(", ")}`}
      </button>
      {open && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {items.map((a) => (
            <Link key={a.id} to={`/analytics/${a.id}`} className="rounded-md bg-surface-2 px-2 py-1 text-xs ring-1 ring-inset ring-line hover:text-brand">
              {a.name}{a.code ? <span className="ml-1 font-mono text-subtle">{a.code}</span> : null}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function Unassigned({ streams, videos }: { streams: Stream[]; videos: Video[] }) {
  return (
    <div className="card space-y-3 p-4 text-sm">
      <p className="text-muted">These were made before analytics existed, or never added to one. Add videos to an analytics from the library.</p>
      <div className="flex flex-wrap gap-2">
        <Link className="btn-secondary btn-xs" to="/library?analytics=none"><FilmIcon className="h-3.5 w-3.5" /> {videos.length} videos in the library</Link>
        <Link className="btn-secondary btn-xs" to="/streams"><CameraIcon className="h-3.5 w-3.5" /> Streams page</Link>
        <StreamsMenu streams={streams} name="Not in any analytics" />
      </div>
      {streams.length > 0 && (
        <ul className="flex flex-wrap gap-1.5" aria-label="Streams not in any analytics">
          {streams.map((s) => (
            <li key={s.id}>
              <Link to={`/streams/${s.id}`} className="flex items-center gap-1.5 rounded-md bg-surface-2 px-2 py-1 font-mono text-xs ring-1 ring-inset ring-line hover:text-brand"
                title={`${s.status} · ${s.source_rel_path}`}>
                <span className={`h-1.5 w-1.5 rounded-full ${isProblem(s) ? "bg-red-500" : isStreamLive(s) ? "bg-emerald-500" : "bg-slate-400"}`} />{s.name}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
