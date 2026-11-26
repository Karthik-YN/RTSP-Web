import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiError } from "../services/api";
import { useUploads } from "./UploadQueue";
import { formatBitrate, formatBytes, formatDuration, formatTime, since } from "../lib/format";
import {
  applyLibraryFilters, filtersActive, NO_FILTERS, SORT_LABEL, uploaderName, uploaders, videoProblems,
  type AnalyticsFilter, type LibraryFilters, type SortKey,
} from "../lib/libraryFilters";
import type { Conversion, ConvertFit, ConvertProfile, OutputConfig, PreviewConfig, User, Video } from "../types/api";
import { useRig } from "../hooks/useRigData";
import { isStreamLive, StreamSetControls } from "./BulkControls";
import { NameChips, RenameDialog } from "./Naming";
import { AnalyticsFilterSelect, AssignDialog } from "./LibraryAnalytics";
import { SHAPE_TEXT } from "./CreateStreamDialog";
import { ConfirmDialog, ErrorBox, Modal, MoreMenu, Pill, Skeleton, Spinner, useToast } from "./common";
import {
  AlertIcon, CameraIcon, CheckIcon, DownloadIcon, FilmIcon, FilterIcon, GridIcon, InfoIcon, MenuIcon, PlayIcon, RefreshIcon,
  SearchIcon, TagIcon, TrashIcon, UploadIcon,
} from "./icons";

/** Cards rendered per step; more appear as you scroll (keeps big libraries fast). */
export const PAGE = 48;
type View = "grid" | "list";
const VIEW_KEY = "rig02.library.view";

export function VideoLibrary({ videos, loading, user, onChanged, onCreateStream }: {
  videos: Video[]; loading: boolean; user: User | null; onChanged: () => void;
  onCreateStream: (videoId: number) => void;
}) {
  const toast = useToast();
  const rig = useRig();
  const { output: out, preview } = rig.config;
  const canWrite = !!user?.can_write;
  const [refreshing, setRefreshing] = useState(false);
  const [fitting, setFitting] = useState<Video | null>(null);
  const [assigning, setAssigning] = useState<Video[] | null>(null);
  const onAssign = useCallback((v: Video) => setAssigning([v]), []);

  // filters: the analytics one lives in the URL (?analytics=), the rest in the page
  const [params, setParams] = useSearchParams();
  const [local, setLocal] = useState<Omit<LibraryFilters, "analytics">>(NO_FILTERS);
  const filters: LibraryFilters = { ...local, analytics: (params.get("analytics") ?? "") as AnalyticsFilter };
  const setFilter = (patch: Partial<LibraryFilters>) => {
    const { analytics, ...rest } = patch;
    if (Object.keys(rest).length) setLocal((f) => ({ ...f, ...rest }));
    if (analytics !== undefined) setParams(analytics ? { analytics } : {}, { replace: true });
  };
  const clear = () => { setLocal((f) => ({ ...NO_FILTERS, sort: f.sort })); setParams({}, { replace: true }); };

  const [view, setView] = useState<View>(() => {
    try {
      const saved = localStorage.getItem(VIEW_KEY);
      if (saved === "list" || saved === "grid") return saved;
    } catch { /* private mode */ }
    // phones: the compact list (a card grid is ~18,000 px tall there); a saved choice always wins
    return typeof window !== "undefined" && window.matchMedia?.("(max-width: 639px)").matches ? "list" : "grid";
  });
  const changeView = (v: View) => { setView(v); try { localStorage.setItem(VIEW_KEY, v); } catch { /* private mode */ } };

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await api.refreshVideos();
      toast("info", `Library rescanned: ${r.added} new, ${r.updated} changed, ${r.missing} missing` +
        (r.settling.length ? `, ${r.settling.length} still being copied` : ""));
      onChanged();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setRefreshing(false);
    }
  };

  const conv = useConversions(onChanged);

  const [deleting, setDeleting] = useState<Video | null>(null);
  const doDelete = async (v: Video) => {
    try {
      toast("ok", (await api.deleteVideo(v.id)).message);
      onChanged();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };
  const usableAll = useMemo(() => videos.filter((v) => v.status === "READY" && v.compat_mode !== "reject"), [videos]);
  const unusable = useMemo(() => videos.filter((v) => !(v.status === "READY" && v.compat_mode !== "reject")), [videos]);
  const people = useMemo(() => uploaders(usableAll), [usableAll]);
  const attentionCount = useMemo(() => usableAll.filter((v) => videoProblems(v).length > 0).length, [usableAll]);
  const anyNotWide = useMemo(() => usableAll.some((v) => v.is_16_9 === false), [usableAll]);
  const nonStandard = useMemo(() => videos.filter((v) => v.name_parts === null).length, [videos]);
  const fkey = JSON.stringify(filters);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const shown = useMemo(() => applyLibraryFilters(usableAll, filters, out), [usableAll, fkey, out]);

  // paged rendering: PAGE more whenever the end of the list scrolls into view
  const [limit, setLimit] = useState(PAGE);
  useEffect(() => setLimit(PAGE), [fkey]);
  const sentinel = useRef<HTMLDivElement>(null);
  const more = shown.length > limit;
  useEffect(() => {
    const el = sentinel.current;
    if (!el || !more || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver((e) => { if (e[0]?.isIntersecting) setLimit((l) => l + PAGE); }, { rootMargin: "600px" });
    io.observe(el);
    return () => io.disconnect();
  }, [more, limit]);
  const page = shown.slice(0, limit);

  // multi-select (for "Add to analytics")
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const toggle = useCallback((v: Video) => setSelected((s) => {
    const n = new Set(s);
    if (n.has(v.id)) n.delete(v.id); else n.add(v.id);
    return n;
  }), []);
  const stopSelecting = () => { setSelecting(false); setSelected(new Set()); };

  // the same content twice (e.g. copied into the server folder under two names): flag the later copies
  const sameAs = useMemo(() => {
    const first = new Map<string, Video>();
    const out = new Map<number, Video>();
    for (const v of [...usableAll].sort((a, b) => a.id - b.id)) {
      const h = v.quick_hash ?? /[?&]h=([0-9a-f]{8})/.exec(v.thumbnail_url ?? "")?.[1];
      if (!h) continue;
      const key = `${v.size_bytes}:${h.slice(0, 8)}`;
      const f = first.get(key);
      if (f) out.set(v.id, f); else first.set(key, v);
    }
    return out;
  }, [usableAll]);

  const common = {
    canWrite, isAdmin: !!user?.is_admin, onCreateStream, onDelete: setDeleting, onConvert: conv.start,
    onCancel: conv.cancel, out, onFit: setFitting, preview, onAssign,
    onToggle: selecting ? toggle : undefined,
  };

  return (
    <div className="space-y-3">
      <div className="z-10 -mx-1 space-y-2 bg-bg/90 px-1 py-2 backdrop-blur sm:sticky sm:top-[var(--header-h,3.25rem)]" data-testid="library-toolbar">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full sm:w-auto sm:min-w-[12rem] sm:max-w-xs sm:flex-1">
            <SearchIcon className="pointer-events-none absolute left-2.5 top-2.5 h-4 w-4 text-subtle" />
            <input className="input pl-8" placeholder="Search names or uploaders…" value={filters.q}
              onChange={(e) => setFilter({ q: e.target.value })} aria-label="Search videos" />
          </div>
          <select className="input w-auto max-sm:flex-1" value={filters.sort} aria-label="Sort videos"
            onChange={(e) => setFilter({ sort: e.target.value as SortKey })}>
            {(Object.keys(SORT_LABEL) as SortKey[]).map((k) => <option key={k} value={k}>{SORT_LABEL[k]}</option>)}
          </select>
          <FiltersButton active={[filters.analytics, filters.shape, filters.uploader, filters.attention, filters.naming].filter(Boolean).length}>
            <AnalyticsFilterSelect value={filters.analytics} onChange={(a) => setFilter({ analytics: a })} tree={rig.analytics} />
            {(anyNotWide || filters.shape) && <select className="input" value={filters.shape} aria-label="Filter by shape"
              onChange={(e) => setFilter({ shape: e.target.value as LibraryFilters["shape"] })}>
              <option value="">Any shape</option>
              <option value="16:9">16:9 only</option>
              <option value="other">Not 16:9</option>
            </select>}
            {people.length > 1 && (
              <select className="input" value={filters.uploader} aria-label="Filter by uploader"
                onChange={(e) => setFilter({ uploader: e.target.value })}>
                <option value="">Anyone</option>
                {people.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            )}
            {(attentionCount > 0 || filters.attention) && (
              <button className={filters.attention ? "btn-primary" : "btn-secondary"} aria-pressed={filters.attention}
                onClick={() => setFilter({ attention: !filters.attention })} title="Videos that don't behave like a real camera: live transcoding, long keyframe gaps, B-frames">
                <AlertIcon /> Needs attention ({attentionCount})
              </button>
            )}
            {(nonStandard > 0 || filters.naming) && (
              <button className={filters.naming ? "btn-primary" : "btn-secondary"} aria-pressed={!!filters.naming}
                onClick={() => setFilter({ naming: !filters.naming })} title="Videos not named WHAT_WHERE_NN: rename them from their card">
                Aa Non-standard names ({nonStandard})
              </button>
            )}
          </FiltersButton>
          <div className="ml-auto flex flex-wrap items-center justify-end gap-2">
            <div className="flex rounded-lg ring-1 ring-inset ring-line" role="group" aria-label="View">
              <button className={`rounded-l-lg p-2 ${view === "grid" ? "bg-surface-2 text-fg" : "text-muted"}`}
                onClick={() => changeView("grid")} aria-pressed={view === "grid"} aria-label="Grid view" title="Grid"><GridIcon /></button>
              <button className={`rounded-r-lg p-2 ${view === "list" ? "bg-surface-2 text-fg" : "text-muted"}`}
                onClick={() => changeView("list")} aria-pressed={view === "list"} aria-label="List view" title="List"><MenuIcon /></button>
            </div>
            {canWrite && (
              <button className={selecting ? "btn-primary" : "btn-secondary"} onClick={() => (selecting ? stopSelecting() : setSelecting(true))}>
                <CheckIcon /> {selecting ? "Done" : "Select"}
              </button>
            )}
            <button className="btn-secondary px-2" onClick={refresh} disabled={refreshing} aria-label="Refresh"
              title="Rescan the server folder (files copied there also appear by themselves within 30 s)">
              {refreshing ? <Spinner /> : <RefreshIcon />}
            </button>
            {canWrite && <UploadButton onDone={onChanged} />}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted" data-testid="library-count">
          <span>{filtersActive(filters) ? `${shown.length} of ${usableAll.length} videos` : `${usableAll.length} video${usableAll.length === 1 ? "" : "s"}`}</span>
          {filters.analytics && <FilterChip onClear={() => setFilter({ analytics: "" })}>
            {filters.analytics === "none" ? "In no analytics" : `Analytics: ${rig.analytics?.flatMap((c) => c.analytics).find((a) => String(a.id) === String(filters.analytics))?.name ?? filters.analytics}`}</FilterChip>}
          {filters.shape && <FilterChip onClear={() => setFilter({ shape: "" })}>{filters.shape === "16:9" ? "16:9 only" : "Not 16:9"}</FilterChip>}
          {filters.uploader && <FilterChip onClear={() => setFilter({ uploader: "" })}>By {filters.uploader}</FilterChip>}
          {filters.attention && <FilterChip onClear={() => setFilter({ attention: false })}>Needs attention</FilterChip>}
          {filters.naming && <FilterChip onClear={() => setFilter({ naming: false })}>Non-standard names</FilterChip>}
          {filtersActive(filters) && <button className="text-brand hover:underline" onClick={clear}>Clear filters</button>}
        </div>
      </div>

      {loading && videos.length === 0 && (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3" aria-busy="true">
          {Array.from({ length: 8 }, (_, i) => <Skeleton key={i} className="h-72 rounded-xl" />)}
        </div>
      )}
      {!loading && usableAll.length === 0 && unusable.length === 0 && (
        <div className="card flex flex-col items-center gap-2 p-10 text-center text-muted">
          <FilmIcon className="h-10 w-10 text-subtle" />
          The library is empty.<br />Upload a video (or drop files anywhere on the page), or copy files into the server folder and press Refresh.
        </div>
      )}
      {usableAll.length > 0 && shown.length === 0 && (
        <div className="card flex flex-col items-center gap-2 p-8 text-center text-sm text-muted" data-testid="no-match">
          No videos match these filters.
          <button className="btn-secondary btn-xs" onClick={clear}>Clear filters</button>
        </div>
      )}

      {view === "grid" ? (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(14rem,1fr))] gap-3">
          {page.map((v) => <VideoCard key={v.id} v={v} job={conv.activeFor(v.id)} selected={selected.has(v.id)} sameAs={sameAs.get(v.id)} {...common} />)}
        </div>
      ) : page.length > 0 && (
        <div className="card divide-y divide-line overflow-hidden" role="list" aria-label="Videos">
          {page.map((v) => <VideoRow key={v.id} v={v} job={conv.activeFor(v.id)} selected={selected.has(v.id)} sameAs={sameAs.get(v.id)} {...common} />)}
        </div>
      )}
      {more && (
        <div ref={sentinel} className="flex justify-center py-2">
          <button className="btn-secondary" onClick={() => setLimit((l) => l + PAGE)}>
            Show more ({shown.length - limit} left)
          </button>
        </div>
      )}

      {selecting && (
        <div className="sticky bottom-3 z-20 mx-auto flex w-fit max-w-full flex-wrap items-center gap-2 rounded-xl border border-line bg-surface px-3 py-2 shadow-pop"
          data-testid="selection-bar">
          <span className="text-sm font-medium">{selected.size} selected</span>
          <button className="btn-ghost btn-xs" onClick={() => setSelected(new Set(shown.map((v) => v.id)))}>Select all {shown.length}</button>
          <button className="btn-ghost btn-xs" disabled={!selected.size} onClick={() => setSelected(new Set())}>Clear</button>
          <button className="btn-primary btn-xs" disabled={!selected.size || !rig.analytics}
            onClick={() => setAssigning(usableAll.filter((v) => selected.has(v.id)))}>
            <TagIcon className="h-3.5 w-3.5" /> Add to analytics
          </button>
        </div>
      )}

      {assigning && rig.analytics && <AssignDialog videos={assigning} tree={rig.analytics} onClose={() => setAssigning(null)}
        onDone={(added, n) => {
          setAssigning(null);
          toast("ok", n ? `${n} link${n === 1 ? "" : "s"} added: ${added.join(", ")}.` : "Already in those analytics.");
          if (selecting) stopSelecting();
          onChanged();
          rig.refreshAnalytics();
        }} />}

      {fitting && <FitDialog v={fitting} out={out} onClose={() => setFitting(null)}
        onStart={async (fit) => { const err = await conv.start(fitting, { profile: "16x9", fit }); if (!err) setFitting(null); return err; }} />}

      {deleting && (
        <ConfirmDialog title={`Delete ${deleting.filename}?`} danger confirmLabel="Move to trash"
          message={<>The file is moved to the library's <span className="font-mono">.trash</span> folder on the server and purged after 7 days
            (an admin can restore it from there). Videos used by a stream cannot be deleted.</>}
          onCancel={() => setDeleting(null)} onConfirm={() => { const v = deleting; setDeleting(null); doDelete(v); }} />
      )}

      {unusable.length > 0 && (
        <details className="card p-3" open={usableAll.length === 0}>
          <summary className="cursor-pointer text-sm font-medium text-fg">
            {unusable.length} file{unusable.length > 1 ? "s" : ""} cannot be streamed
          </summary>
          <ul className="mt-2 space-y-1 text-sm">
            {unusable.map((v) => (
              <li key={v.id} className="flex flex-wrap items-center gap-2">
                <span className="break-all font-medium">{v.rel_path}</span>
                {user?.is_admin && v.status !== "MISSING" && (
                  <button className="btn-ghost btn-xs text-red-600" onClick={() => setDeleting(v)}>Delete</button>
                )}
                <span className="text-red-700 dark:text-red-300">{v.error ?? v.compat_reasons.join(" ")}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** "Filters" with a count; the filter controls open below it (kept out of the toolbar until needed). */
function FiltersButton({ active, children }: { active: number; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const away = () => setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    window.addEventListener("resize", away);
    return () => {
      document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc);
      window.removeEventListener("resize", away);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button className={active ? "btn-primary" : "btn-secondary"} onClick={() => setOpen((o) => !o)} aria-expanded={open} aria-haspopup="true">
        <FilterIcon /> Filters{active ? ` (${active})` : ""}
      </button>
      {open && (
        <div className="absolute left-0 top-full z-30 mt-1 flex w-72 flex-col gap-2 rounded-xl border border-line bg-surface p-3 shadow-pop" role="group" aria-label="Filters">
          {children}
        </div>
      )}
    </div>
  );
}

function FilterChip({ children, onClear }: { children: React.ReactNode; onClear: () => void }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-medium text-brand">
      {children}
      <button className="rounded-full px-0.5 hover:bg-brand/10" onClick={onClear} aria-label={`Remove filter: ${String(children)}`}>×</button>
    </span>
  );
}

/** The video file on the server: plays in the browser, or downloads with its own name. */
export function videoFileUrl(id: number, download = false): string {
  return `/api/videos/${id}/file${download ? "?download=true" : ""}`;
}

/**
 * "Convert to H.264" jobs: start, cancel, and follow progress (polled every 2 s
 * while one is queued or running). Finished jobs refresh the library.
 */
export function useConversions(onChanged: () => void) {
  const toast = useToast();
  const [jobs, setJobs] = useState<Conversion[]>([]);
  const seen = useRef<Record<string, Conversion["state"]>>({});
  const load = useCallback(async () => {
    try {
      const list = await api.conversions();
      let changed = false;
      for (const j of list) {
        const before = seen.current[j.id];
        if (before && before !== j.state && (before === "queued" || before === "running")) {
          if (j.state === "done") { toast("ok", `Done: ${j.target} is in the library`); changed = true; }
          if (j.state === "failed") toast("bad", j.error ?? `Converting ${j.source} failed`);
        }
        seen.current[j.id] = j.state;
      }
      setJobs(list);
      if (changed) onChanged();
    } catch { /* keep the last list */ }
  }, [onChanged, toast]);
  const busy = jobs.some((j) => j.state === "queued" || j.state === "running");
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [busy, load]);
  /** Starts a conversion; resolves to the error message, or null when it started. */
  const start = useCallback(async (v: Video, opts?: { profile: ConvertProfile; fit?: ConvertFit }): Promise<string | null> => {
    try {
      const j = await api.convertVideo(v.id, opts);
      seen.current[j.id] = j.state;
      setJobs((cur) => [j, ...cur]);
      toast("info", `${WHAT[opts?.profile ?? "h264"]} of ${v.filename} in the background; the original is kept.`);
      return null;
    } catch (e) {
      const msg = (e as ApiError).message;
      toast("bad", msg);
      return msg;
    }
  }, [toast]);
  const cancel = useCallback(async (j: Conversion) => {
    try { await api.cancelConversion(j.id); await load(); } catch (e) { toast("bad", (e as ApiError).message); }
  }, [load, toast]);
  return {
    jobs,
    activeFor: (videoId: number) => jobs.find((j) => j.video_id === videoId && (j.state === "queued" || j.state === "running")),
    start,
    cancel,
  };
}

const WHAT: Record<ConvertProfile, string> = {
  h264: "Converting to H.264", "720p": "Making a 720p copy", "16x9": "Making a 16:9 copy", clip: "Making a clip",
};

function formatEta(s: number | null): string {
  if (s === null) return "";
  return s < 60 ? ` · ~${s} s left` : ` · ~${Math.round(s / 60)} min left`;
}

export function ConversionProgress({ job, onCancel }: { job: Conversion; onCancel?: (j: Conversion) => void }) {
  const pct = Math.round(job.progress * 100);
  return (
    <div className="space-y-1" data-testid="conversion-progress">
      <div className="flex items-center justify-between text-xs">
        <span>{job.state === "queued" ? "Waiting to convert…" : `${WHAT[job.profile ?? "h264"]}: ${pct}%${formatEta(job.eta_s)}`}</span>
        {onCancel && <button className="btn-ghost btn-xs" onClick={() => onCancel(job)}>Cancel</button>}
      </div>
      <div className="h-1.5 overflow-hidden rounded bg-surface-2">
        <div className="h-full bg-brand transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/** Short clip of a long video (made and cached by the server). */
export function previewUrl(id: number, start = 0): string {
  return `/api/videos/${id}/preview${start > 0 ? `?start=${start}` : ""}`;
}

/** Where a preview can start: the beginning, then a quarter, half and three quarters in (whole minutes). */
export function previewStarts(duration: number, clip: number): number[] {
  const last = Math.max(0, duration - clip);
  const out = [0, 0.25, 0.5, 0.75].map((f) => Math.min(last, Math.floor((duration * f) / 60) * 60));
  return [...new Set(out)];
}

/**
 * Plays the file in the browser. Long videos play a short preview clip instead of the
 * whole file; formats browsers can't play get the H.264 copy offer; Download is always there.
 */
export function VideoPlayer({ v, onClose, onConvert, job, preview }: {
  v: Video; onClose: () => void; onConvert?: () => Promise<string | null> | void; job?: Conversion;
  preview?: PreviewConfig;
}) {
  if (preview && (v.duration_s ?? 0) > preview.threshold_s) {
    return <PreviewPlayer v={v} clip={preview.seconds} onClose={onClose} />;
  }
  return <FilePlayer v={v} onClose={onClose} onConvert={onConvert} job={job} />;
}

function FilePlayer({ v, onClose, onConvert, job }: {
  v: Video; onClose: () => void; onConvert?: () => Promise<string | null> | void; job?: Conversion;
}) {
  const [failed, setFailed] = useState(false);
  const [convertErr, setConvertErr] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const convert = async () => {
    if (!onConvert) return;
    setStarting(true);
    setConvertErr((await onConvert()) ?? null);
    setStarting(false);
  };
  const unplayable = v.browser_playable === false || failed;
  return (
    <Modal title={v.filename} wide onClose={onClose}>
      {unplayable ? (
        <div className="space-y-3 text-sm" data-testid="not-playable">
          <p>{v.browser_note ?? `This browser can't play this file (${v.video_codec}).`}</p>
          <p className="text-muted">
            It still streams over RTSP. To watch it here, convert it once to H.264: the copy plays in every browser,
            streams without live transcoding and starts within 2 s. The original is kept.
          </p>
          {job ? <ConversionProgress job={job} />
            : onConvert && (
              <button className="btn-primary" onClick={convert} disabled={starting}>
                {starting ? <Spinner /> : <RefreshIcon />} Convert to H.264
              </button>
            )}
          {convertErr && !job && <ErrorBox>{convertErr}</ErrorBox>}
        </div>
      ) : (
        <video data-testid="video-player" className="max-h-[70vh] w-full rounded bg-black" src={videoFileUrl(v.id)}
          controls autoPlay playsInline preload="metadata" onError={() => setFailed(true)} />
      )}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)} · uploaded by {uploaderName(v)}</span>
        <a className="btn-secondary" href={videoFileUrl(v.id, true)} download={v.filename}><DownloadIcon /> Download</a>
      </div>
      <VideoAnalyticsLine v={v} className="mt-2" />
    </Modal>
  );
}

/** Memoised: the library re-renders on every poll, a card only when its own video or job changes. */
interface ItemProps {
  v: Video; canWrite: boolean; isAdmin: boolean; onCreateStream: (id: number) => void; onDelete: (v: Video) => void;
  job?: Conversion; onConvert: (v: Video, opts?: { profile: ConvertProfile; fit?: ConvertFit }) => Promise<string | null>;
  onCancel: (j: Conversion) => void; out?: OutputConfig; onFit: (v: Video) => void; preview?: PreviewConfig;
  onAssign?: (v: Video) => void;
  /** Set while selecting: the item shows a checkbox. */
  onToggle?: (v: Video) => void;
  selected?: boolean;
  /** Another library video with exactly the same content (by fingerprint, not name). */
  sameAs?: Video;
}

/** What a library item offers, shared by the card and the list row. */
function itemFlags(v: Video, out: OutputConfig | undefined, canWrite: boolean) {
  const notWide = v.is_16_9 === false;
  const blocked = notWide && !!out?.require_16_9;
  const isStd = !!out && v.display_width === out.width && v.display_height === out.height;
  const offer720 = canWrite && !!out && v.is_16_9 === true && !isStd && !v.copy_720p_id && !/ \(720p\)\.mp4$/.test(v.rel_path);
  const reasons = v.convert_reasons ?? [];
  return { notWide, blocked, isStd, offer720, reasons };
}

/** Only what needs a look: "not 16:9", and one amber chip when the video is not camera-like
 *  (live transcoding, long keyframe gap, B-frames). Normal states show nothing. */
function Flags({ v, out, onOpen, sameAs }: { v: Video; out?: OutputConfig; onOpen: () => void; sameAs?: Video }) {
  const { notWide } = itemFlags(v, out, false);
  const problems = videoProblems(v);
  if (!notWide && !problems.length && !sameAs) return null;
  const label = problems.length === 1 ? problems[0].short : `${problems.length} issues`;
  return (
    <div className="flex flex-wrap gap-1">
      {sameAs && <span data-testid="same-as"><Pill tone="warn" title={`Byte-for-byte the same video as '${sameAs.filename}' (found by content, not name). An admin can delete one of them.`}>
        Same video as {sameAs.filename}</Pill></span>}
      {notWide && <Pill tone="muted" title="Kept in the library; streams must be 16:9. Make a 16:9 copy to stream it.">
        Not 16:9 · {SHAPE_TEXT[v.shape ?? "other"]}</Pill>}
      {problems.length > 0 && (
        <button onClick={onOpen} data-testid="problem-chip"
          title={[...problems.map((x) => x.text), "Fix: ⋯ → Make a copy (keyframe every 2 s, plays without transcoding)."].join("\n")}
          aria-label={`${label}: ${v.filename}`}
          className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-amber-500/10 px-2 py-0.5 text-[11px] font-medium text-amber-700 ring-1 ring-inset ring-amber-500/30 hover:bg-amber-500/20 dark:text-amber-300">
          <AlertIcon className="h-3 w-3" /> {label}
        </button>
      )}
    </div>
  );
}

function MainAction({ v, canWrite, blocked, job, onCreateStream, onFit, onPlay, compact = false }: {
  v: Video; canWrite: boolean; blocked: boolean; job?: Conversion; onCreateStream: (id: number) => void;
  onFit: (v: Video) => void; onPlay: () => void; compact?: boolean;
}) {
  const cls = compact ? "btn-primary btn-xs whitespace-nowrap" : "btn-primary min-w-0 flex-1 whitespace-nowrap px-2";
  const label = (t: string) => compact ? <span className="hidden sm:inline">{t}</span> : t;
  if (canWrite && blocked && v.copy_16x9_id) {
    return <button className={cls} onClick={() => onCreateStream(v.copy_16x9_id!)} title="Streams the 16:9 copy of this video"
      aria-label={`Stream the 16:9 copy of ${v.filename}`}><CameraIcon /> {label("New stream (copy)")}</button>;
  }
  if (canWrite && blocked) {
    return <button className={cls} onClick={() => onFit(v)} disabled={!!job} aria-label={`Make a 16:9 copy of ${v.filename}`}
      title="Streams must be 16:9: make a 16:9 copy (the original is kept)"><RefreshIcon /> {label("16:9 copy")}</button>;
  }
  if (canWrite) {
    return <button className={cls} onClick={() => onCreateStream(v.id)} aria-label={`New stream from ${v.filename}`} title="Publish this video as an RTSP camera">
      <CameraIcon /> {label("New stream")}</button>;
  }
  return <button className={compact ? "btn-secondary btn-xs" : "btn-secondary min-w-0 flex-1 whitespace-nowrap px-2"} onClick={onPlay}><PlayIcon /> {label("Play")}</button>;
}

/** Everything besides the main button: one ⋯ menu instead of a row of icons. */
function ItemMenu({ v, canWrite, isAdmin, out, job, onAssign, onDelete, onConvert, onPlay, onDetails, onRename }: {
  v: Video; canWrite: boolean; isAdmin: boolean; out?: OutputConfig; job?: Conversion;
  onAssign?: (v: Video) => void; onDelete: (v: Video) => void;
  onConvert: (v: Video, opts?: { profile: ConvertProfile; fit?: ConvertFit }) => Promise<string | null>;
  onPlay: () => void; onDetails: () => void; onRename?: () => void;
}) {
  const { notWide, offer720 } = itemFlags(v, out, canWrite);
  const isCopy = / \((h264|720p|720p 16x9|camera|720p camera)\)\.mp4$/.test(v.rel_path);
  // Only servers that report B-frames (b_frames present) make camera-style copies without them.
  const camCopies = v.b_frames !== undefined;
  const items = [
    { label: "Play in browser", icon: <PlayIcon className="h-4 w-4" />, onClick: onPlay },
    { label: "Details", icon: <InfoIcon className="h-4 w-4" />, onClick: onDetails },
    ...(canWrite && onAssign ? [{ label: "Add to analytics", icon: <TagIcon className="h-4 w-4" />, onClick: () => onAssign(v) }] : []),
    ...(canWrite && onRename ? [{ label: "Rename", icon: <span className="w-4 text-center font-mono text-[10px] font-bold">Aa</span>, onClick: onRename }] : []),
    ...(canWrite && camCopies && !notWide && !isCopy ? [{
      label: "Make a copy (same size)", icon: <RefreshIcon className="h-4 w-4" />, disabled: !!job,
      title: "One-off H.264 copy at the same size, keyframe every 2 s: streams it without live transcoding.",
      onClick: () => onConvert(v, { profile: "h264" }),
    }] : []),
    ...(offer720 && camCopies ? [{
      label: "Make a 720p copy", icon: <RefreshIcon className="h-4 w-4" />, disabled: !!job,
      title: `One-off copy at ${out!.width}×${out!.height}, keyframe every 2 s.`,
      onClick: () => onConvert(v, { profile: "720p" }),
    }] : []),
    { label: "Download", icon: <DownloadIcon className="h-4 w-4" />, onClick: () => downloadFile(videoFileUrl(v.id, true), v.filename) },
    ...(canWrite && isAdmin ? [{
      label: "Delete", icon: <TrashIcon className="h-4 w-4" />, danger: true, disabled: v.streams_using > 0,
      title: v.streams_using > 0 ? "Used by a stream" : "Delete video (admin)", onClick: () => onDelete(v),
    }] : []),
  ];
  return <MoreMenu label={`More actions for ${v.filename}`} items={items} />;
}

function downloadFile(href: string, name: string) {
  const a = document.createElement("a");
  a.href = href; a.download = name; document.body.appendChild(a); a.click(); a.remove();
}

function SelectBox({ v, selected, onToggle, className = "" }: { v: Video; selected?: boolean; onToggle: (v: Video) => void; className?: string }) {
  return (
    <input type="checkbox" className={`h-5 w-5 shrink-0 cursor-pointer accent-blue-600 ${className}`} checked={!!selected}
      onChange={() => onToggle(v)} onClick={(e) => e.stopPropagation()} aria-label={`Select ${v.filename}`} />
  );
}

const VideoCard = memo(function VideoCard(p: ItemProps) {
  const { v, canWrite, isAdmin, onCreateStream, onDelete, job, onConvert: convertVideo, onCancel, out, onFit, preview, onAssign,
    onToggle, selected } = p;
  const onConvert = () => convertVideo(v);
  const { blocked, reasons } = itemFlags(v, out, canWrite);
  const [playing, setPlaying] = useState(false);
  const [details, setDetails] = useState(false);
  const [renaming, setRenaming] = useState(false);
  return (
    <div className={`card flex flex-col overflow-hidden ${selected ? "ring-2 ring-brand" : ""}`}
      style={{ contentVisibility: "auto", containIntrinsicSize: "auto 22rem" }}>
      {/* Fixed 16:9 box: portrait and 4:3 thumbnails are letterboxed inside it, so every card has the same height. */}
      <div className="group relative aspect-video shrink-0 overflow-hidden bg-slate-900" data-testid="thumb-box">
        {v.thumbnail_url
          ? <img src={v.thumbnail_url} alt="" loading="lazy" decoding="async"
              className="absolute inset-0 h-full w-full object-contain" />
          : <div className="absolute inset-0 flex items-center justify-center"><FilmIcon className="h-8 w-8 text-muted" /></div>}
        <button className="absolute inset-0 flex items-center justify-center bg-black/0 transition hover:bg-black/30"
          onClick={() => (onToggle ? onToggle(v) : setPlaying(true))} aria-label={onToggle ? `Select ${v.filename}` : `Play ${v.filename}`}
          title={onToggle ? "Select" : "Play in the browser"} tabIndex={onToggle ? -1 : 0}>
          {!onToggle && <span className="rounded-full bg-black/60 p-3 text-white opacity-80 group-hover:opacity-100"><PlayIcon className="h-6 w-6" /></span>}
        </button>
        <span className="pointer-events-none absolute bottom-1.5 left-1.5 rounded bg-black/75 px-1.5 py-0.5 text-[11px] text-white" data-testid="res-chip">
          {v.display_width && v.display_height ? `${v.display_width}×${v.display_height}` : `${v.width}×${v.height}`}
        </span>
        <span className="pointer-events-none absolute bottom-1.5 right-1.5 rounded bg-black/75 px-1.5 py-0.5 font-mono text-[11px] text-white">
          {formatTime(v.duration_s, false)}
        </span>
        {v.streams_using > 0 && (
          <span className="pointer-events-none absolute right-1.5 top-1.5 rounded bg-green-600/90 px-1.5 py-0.5 text-[11px] font-medium text-white">
            {v.streams_using} stream{v.streams_using > 1 ? "s" : ""}
          </span>
        )}
        {onToggle && <SelectBox v={v} selected={selected} onToggle={onToggle} className="absolute left-2 top-2" />}
      </div>
      <div className="flex flex-1 flex-col gap-1.5 p-3">
        <div className="flex items-start gap-1">
          <div className="line-clamp-2 min-w-0 flex-1 break-words font-mono text-sm font-medium leading-snug" title={v.rel_path}>{v.rel_path}</div>
          <button className="-mr-1 -mt-0.5 rounded p-1 text-muted hover:bg-surface-2 hover:text-fg"
            onClick={() => setDetails(true)} aria-label={`Details of ${v.filename}`} title="Details and uploader">
            <InfoIcon />
          </button>
        </div>
        <NameChips v={v} onRename={canWrite ? () => setRenaming(true) : undefined} />
        <div className="truncate text-xs text-muted" title={[v.fps ? `${v.fps} fps` : null, v.video_codec, formatBytes(v.size_bytes)].filter(Boolean).join(" · ")}>
          {v.fps ? `${v.fps.toFixed(3).replace(/\.?0+$/, "")} fps` : "–"} · {v.video_codec}{v.bit_depth && v.bit_depth !== 8 ? ` ${v.bit_depth}-bit` : ""} · {formatBytes(v.size_bytes)}
        </div>
        <UploaderLine v={v} />
        {!!v.analytics?.length && <AnalyticsChips v={v} />}
        <Flags v={v} out={out} onOpen={() => setDetails(true)} sameAs={p.sameAs} />
        {job && <ConversionProgress job={job} onCancel={canWrite ? onCancel : undefined} />}
        {v.streams_using > 0 && canWrite && <VideoStreamsBar videoId={v.id} />}
        <div className="mt-auto flex gap-1 pt-1">
          <MainAction v={v} canWrite={canWrite} blocked={blocked} job={job} onCreateStream={onCreateStream} onFit={onFit}
            onPlay={() => setPlaying(true)} />
          <ItemMenu v={v} canWrite={canWrite} isAdmin={isAdmin} out={out} job={job} onAssign={onAssign} onDelete={onDelete}
            onConvert={convertVideo} onPlay={() => setPlaying(true)} onDetails={() => setDetails(true)}
            onRename={canWrite && v.name_parts !== undefined ? () => setRenaming(true) : undefined} />
        </div>
      </div>
      {renaming && <RenameDialog v={v} onClose={() => setRenaming(false)} />}
      {details && <VideoDetails v={v} onClose={() => setDetails(false)} />}
      {playing && <VideoPlayer v={v} onClose={() => setPlaying(false)} job={job} preview={preview}
        onConvert={canWrite && reasons.length > 0 ? onConvert : undefined} />}
    </div>
  );
});

/** Live/total streams made from one video, with fast Start all / Stop all buttons. */
function VideoStreamsBar({ videoId }: { videoId: number }) {
  const { streams } = useRig();
  const mine = (streams ?? []).filter((s) => s.video_id === videoId);
  if (!mine.length) return null;
  const live = mine.filter(isStreamLive).length;
  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg bg-surface-2 px-2 py-1.5 ring-1 ring-inset ring-line">
      <span className="flex-1 text-xs text-muted" title={mine.map((s) => s.name).join(", ")}>
        <b className={live ? "text-emerald-700 dark:text-emerald-400" : "text-fg"}>{live}</b>/{mine.length} live
      </span>
      <StreamSetControls streams={mine} label={`the streams of this video (${mine.map((s) => s.name).join(", ")})`} />
    </div>
  );
}

/** One line per video: compact, for scanning long libraries. */
const VideoRow = memo(function VideoRow(p: ItemProps) {
  const { v, canWrite, isAdmin, onCreateStream, onDelete, job, onConvert: convertVideo, onCancel, out, onFit, preview, onAssign,
    onToggle, selected } = p;
  const { blocked, reasons } = itemFlags(v, out, canWrite);
  const [playing, setPlaying] = useState(false);
  const [details, setDetails] = useState(false);
  const [renaming, setRenaming] = useState(false);
  return (
    <div role="listitem" className={`flex flex-wrap items-center gap-x-3 gap-y-2 px-3 py-2 sm:flex-nowrap ${selected ? "bg-brand-soft" : ""}`}
      style={{ contentVisibility: "auto", containIntrinsicSize: "auto 4.5rem" }} data-testid="video-row">
      {onToggle && <SelectBox v={v} selected={selected} onToggle={onToggle} />}
      <button className="relative aspect-video w-24 shrink-0 overflow-hidden rounded-md bg-slate-900"
        onClick={() => setPlaying(true)} aria-label={`Play ${v.filename}`} title="Play in the browser">
        {v.thumbnail_url && <img src={v.thumbnail_url} alt="" loading="lazy" decoding="async" className="absolute inset-0 h-full w-full object-contain" />}
        <span className="absolute bottom-0.5 right-0.5 rounded bg-black/75 px-1 font-mono text-[10px] text-white">{formatTime(v.duration_s, false)}</span>
      </button>
      <div className="min-w-0 flex-1 basis-40">
        <div className="flex items-center gap-1">
          <span className="truncate font-mono text-sm font-medium" title={v.rel_path}>{v.rel_path}</span>
          <button className="shrink-0 rounded p-0.5 text-muted hover:text-fg" onClick={() => setDetails(true)}
            aria-label={`Details of ${v.filename}`} title="Details and uploader"><InfoIcon className="h-3.5 w-3.5" /></button>
          <span className="hidden shrink-0 md:inline"><NameChips v={v} onRename={canWrite ? () => setRenaming(true) : undefined} /></span>
        </div>
        <div className="flex flex-wrap gap-x-2 text-xs text-muted">
          <span>{v.display_width ?? v.width}×{v.display_height ?? v.height}</span>
          <span>{v.video_codec}</span>
          <span>{formatBytes(v.size_bytes)}</span>
          <span className="truncate" data-testid="row-uploader">by {uploaderName(v)}</span>
          {v.streams_using > 0 && <span className="text-emerald-700 dark:text-emerald-400">{v.streams_using} stream{v.streams_using > 1 ? "s" : ""}</span>}
        </div>
      </div>
      <div className="hidden w-52 shrink-0 flex-col gap-1 xl:flex">
        {!!v.analytics?.length && <AnalyticsChips v={v} />}
        <Flags v={v} out={out} onOpen={() => setDetails(true)} sameAs={p.sameAs} />
      </div>
      {job && <div className="w-full sm:w-48"><ConversionProgress job={job} onCancel={canWrite ? onCancel : undefined} /></div>}
      <div className="ml-auto flex shrink-0 items-center gap-1">
        <MainAction v={v} canWrite={canWrite} blocked={blocked} job={job} onCreateStream={onCreateStream} onFit={onFit}
          onPlay={() => setPlaying(true)} compact />
        <ItemMenu v={v} canWrite={canWrite} isAdmin={isAdmin} out={out} job={job} onAssign={onAssign} onDelete={onDelete}
          onConvert={convertVideo} onPlay={() => setPlaying(true)} onDetails={() => setDetails(true)}
          onRename={canWrite && v.name_parts !== undefined ? () => setRenaming(true) : undefined} />
      </div>
      {renaming && <RenameDialog v={v} onClose={() => setRenaming(false)} />}
      {details && <VideoDetails v={v} onClose={() => setDetails(false)} />}
      {playing && <VideoPlayer v={v} onClose={() => setPlaying(false)} job={job} preview={preview}
        onConvert={canWrite && reasons.length > 0 ? () => convertVideo(v) : undefined} />}
    </div>
  );
});

/** Analytics this video is in, each linking to its page. At most 3 are shown; "+N" shows the rest. */
export function AnalyticsChips({ v, all = false }: { v: Video; all?: boolean }) {
  const [open, setOpen] = useState(all);
  // one chip per analytics, even if the video sits in it more than once
  const list = (v.analytics ?? []).filter((a, i, xs) => xs.findIndex((b) => b.id === a.id) === i);
  const shown = open ? list : list.slice(0, 3);
  return (
    <div className="flex flex-wrap gap-1" data-testid="analytics-chips">
      {shown.map((a) => (
        <Link key={a.id} to={`/analytics/${a.id}`} title={`Open ${a.name}`}
          className="rounded-full bg-brand-soft px-2 py-0.5 text-[11px] font-medium text-brand hover:underline">{a.name}</Link>
      ))}
      {list.length > shown.length && (
        <button type="button" className="rounded-full bg-surface-2 px-2 py-0.5 text-[11px] text-muted hover:text-fg"
          onClick={(e) => { e.stopPropagation(); setOpen(true); }}
          title={list.slice(3).map((a) => a.name).join(", ")} aria-label={`Show ${list.length - shown.length} more analytics`}>
          +{list.length - shown.length}
        </button>
      )}
    </div>
  );
}

/** "Analytics: PPE · FireSmoke", or a hint when the video is not in any analytics yet. */
export function VideoAnalyticsLine({ v, className = "" }: { v: Video; className?: string }) {
  return (
    <div className={`flex flex-wrap items-center gap-2 text-xs ${className}`} data-testid="video-analytics">
      <span className="text-muted">Analytics:</span>
      {v.analytics?.length ? <AnalyticsChips v={v} all />
        : <span className="italic text-subtle">not in any analytics yet (use the tag button to add it)</span>}
    </div>
  );
}

/** "Uploaded by admin · 2h ago" or "Uploaded by alice": administrators (and anything added before
 * login existed) show as "admin", everyone else by user name. */
export function UploaderLine({ v, now }: { v: Video; now?: number }) {
  const who = uploaderName(v);
  const when = v.uploaded_at ? `${since(v.uploaded_at, now)} ago` : null;
  const full = [who, v.uploaded_at && new Date(v.uploaded_at).toLocaleString(), v.uploaded_from, v.uploaded_device]
    .filter(Boolean).join(" · ");
  return (
    <div className="truncate text-xs text-muted" data-testid="uploader" title={full}>
      Uploaded by <span className="font-medium text-current">{who}</span>{when && <> · {when}</>}
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-2 border-b border-slate-100 py-1.5 text-sm last:border-0 dark:border-slate-800">
      <dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{children ?? "–"}</dd>
    </div>
  );
}

/** FFmpeg reports a family ("mov,mp4,m4a,…"): show the member matching the file's extension. */
export function containerName(v: Video): string | null {
  if (!v.container) return null;
  const names = v.container.split(",");
  const ext = v.rel_path.split(".").pop()?.toLowerCase() ?? "";
  return names.includes(ext) ? ext : names[0];
}

/** Everything known about one library video: who uploaded it, from where, and its technical details. */
export function VideoDetails({ v, onClose }: { v: Video; onClose: () => void }) {
  const fps = v.fps ? `${v.fps.toFixed(3).replace(/\.?0+$/, "")}${v.is_vfr ? " (variable)" : ""}` : null;
  const disp = v.display_width && v.display_height && (v.display_width !== v.width || v.display_height !== v.height)
    ? ` (shown as ${v.display_width}×${v.display_height})` : "";
  return (
    <Modal title={v.filename} onClose={onClose}>
      <dl data-testid="video-details">
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Upload</h3>
        <Row k="Uploaded by">{uploaderName(v)}</Row>
        {v.uploaded_by ? (
          <>
            {v.uploaded_by !== uploaderName(v) && <Row k="Account">{v.uploaded_by === "anonymous" ? "login was off" : v.uploaded_by}</Row>}
            <Row k="When">{v.uploaded_at ? `${new Date(v.uploaded_at).toLocaleString()} (${since(v.uploaded_at)} ago)` : null}</Row>
            <Row k="From address">{v.uploaded_from}</Row>
            <Row k="Device">{v.uploaded_device}</Row>
          </>
        ) : <Row k="How">Copied onto the server (not uploaded through this app)</Row>}
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Analytics</h3>
        <div className="py-1.5">
          {v.analytics?.length ? <AnalyticsChips v={v} all />
            : <span className="text-sm italic text-subtle">Not in any analytics yet.</span>}
        </div>
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">File</h3>
        <Row k="Path"><span className="font-mono text-xs">{v.rel_path}</span></Row>
        {v.original_name && <Row k="Uploaded as">{v.original_name}</Row>}
        {!!v.previous_names?.length && <Row k="Earlier names">{v.previous_names.join(" → ")}</Row>}
        <Row k="Size">{formatBytes(v.size_bytes)}</Row>
        <Row k="Duration">{v.duration_s != null ? `${formatTime(v.duration_s, false)} (${formatDuration(v.duration_s)})` : null}</Row>
        <Row k="Container">{containerName(v)}</Row>
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Video</h3>
        <Row k="Resolution">{v.width && v.height ? `${v.width}×${v.height}${disp}` : null}</Row>
        <Row k="Shape">{v.shape ? (v.is_16_9 ? "16:9" : `Not 16:9 · ${SHAPE_TEXT[v.shape]}`) : null}</Row>
        <Row k="Codec">{[v.video_codec, v.video_profile, v.pix_fmt, v.bit_depth ? `${v.bit_depth}-bit` : null].filter(Boolean).join(" · ") || null}</Row>
        <Row k="Frame rate">{fps}</Row>
        <Row k="Bitrate">{v.bitrate_bps ? formatBitrate(v.bitrate_bps) : null}</Row>
        <Row k="Keyframes">{v.keyframe_interval_s ? `every ${v.keyframe_interval_s.toFixed(1)} s` : null}</Row>
        <Row k="Audio">{v.audio_codec ? `${v.audio_codec} (dropped when streaming)` : "none"}</Row>
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Streaming</h3>
        <Row k="Mode">{v.compat_mode === "copy" ? "Copy (no transcoding)" : v.compat_mode === "transcode" ? "Live transcode" : v.compat_mode}</Row>
        <Row k="In use by">{`${v.streams_using} stream${v.streams_using === 1 ? "" : "s"}`}</Row>
        <Row k="Copies">{[v.copy_720p_id && "720p copy ready", v.copy_16x9_id && "16:9 copy ready"].filter(Boolean).join(", ") || "none"}</Row>
        {[...v.compat_reasons, ...v.compat_warnings].length > 0 && (
          <Row k="Notes"><ul className="list-disc pl-4">{[...v.compat_reasons, ...v.compat_warnings].map((n) => <li key={n}>{n}</li>)}</ul></Row>
        )}
      </dl>
    </Modal>
  );
}

const FITS: { fit: ConvertFit; title: string; text: string }[] = [
  { fit: "blur", title: "Blurred bars (recommended)", text: "The whole picture, with a blurred, zoomed copy of it filling the sides. Nothing is cut off; looks natural for portrait clips." },
  { fit: "pad", title: "Black bars", text: "The whole picture with plain black bars. Nothing is cut off." },
  { fit: "crop", title: "Crop to fill", text: "Fills the frame and cuts off the top and bottom (or the sides). Objects near the edges can be lost." },
];

/** Choose how a portrait / 4:3 video is fitted into the standard 16:9 frame, then start the copy. */
export function FitDialog({ v, out, onClose, onStart }: {
  v: Video; out?: OutputConfig; onClose: () => void; onStart: (fit: ConvertFit) => Promise<string | null>;
}) {
  const [fit, setFit] = useState<ConvertFit>("blur");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const size = out ? `${out.width}×${out.height}` : "16:9";
  return (
    <Modal title={`Make a 16:9 copy of ${v.filename}`} onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p className="text-muted">
          This video is {v.display_width}×{v.display_height} ({SHAPE_TEXT[v.shape ?? "other"]}). Streams must be 16:9, so a copy
          at {size} is made next to it; the original stays in the library for playing and downloading.
        </p>
        <div className="space-y-2" role="radiogroup" aria-label="How to fit the picture">
          {FITS.map((f) => (
            <label key={f.fit} className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${fit === f.fit ? "border-brand bg-brand/5" : "border-line"}`}>
              <input type="radio" name="fit" className="mt-1" checked={fit === f.fit} onChange={() => setFit(f.fit)} />
              <span><span className="font-medium">{f.title}</span><br /><span className="text-muted">{f.text}</span></span>
            </label>
          ))}
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy}
            onClick={async () => { setBusy(true); setErr(await onStart(fit)); setBusy(false); }}>
            {busy ? <Spinner /> : <RefreshIcon />} Make 16:9 copy
          </button>
        </div>
      </div>
    </Modal>
  );
}

function PreviewPlayer({ v, clip, onClose }: { v: Video; clip: number; onClose: () => void }) {
  const duration = v.duration_s ?? 0;
  const starts = previewStarts(duration, clip);
  const [start, setStart] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const pick = (s: number) => { if (s !== start) { setStart(s); setLoading(true); setFailed(false); } };
  return (
    <Modal title={v.filename} wide onClose={onClose}>
      <p className="mb-2 text-sm text-muted" data-testid="preview-label">
        Preview: {formatDuration(clip)} from {formatTime(start, false)} of a {formatDuration(duration)} video.
        Download it for the full length.
      </p>
      <div className="relative">
        <video key={start} data-testid="video-player" className="max-h-[70vh] w-full rounded bg-black" src={previewUrl(v.id, start)}
          controls autoPlay playsInline preload="auto" onLoadedData={() => setLoading(false)}
          onError={() => { setLoading(false); setFailed(true); }} />
        {loading && !failed && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 rounded bg-black/60 text-sm text-white">
            <Spinner /> Preparing the preview (first time only, a few seconds)…
          </div>
        )}
      </div>
      {failed && <div className="mt-2"><ErrorBox>The preview could not be made. Download the video to watch it.</ErrorBox></div>}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Start the preview at</span>
        {starts.map((s) => (
          <button key={s} className={s === start ? "btn-primary btn-xs" : "btn-secondary btn-xs"} onClick={() => pick(s)}
            aria-pressed={s === start}>{formatTime(s, false)}</button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)} · uploaded by {uploaderName(v)}</span>
        <a className="btn-secondary" href={videoFileUrl(v.id, true)} download={v.filename}><DownloadIcon /> Download full video</a>
      </div>
      <VideoAnalyticsLine v={v} className="mt-2" />
    </Modal>
  );
}

function UploadButton({ onDone: _onDone }: { onDone: () => void }) {
  // Uploads run in the app-wide queue (bottom-right panel), so they keep going while you
  // move between pages; the library refreshes when each file lands.
  const { enqueue } = useUploads();
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input ref={input} type="file" multiple className="hidden" accept="video/*,.mkv,.ts,.mts,.m2ts"
        onChange={(e) => { enqueue(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <button className="btn-primary" onClick={() => input.current?.click()} title="You can also drop videos anywhere on the page">
        <UploadIcon /> Upload<span className="hidden sm:inline"> videos</span>
      </button>
    </>
  );
}
