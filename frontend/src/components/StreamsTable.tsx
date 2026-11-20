import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useLiveStream, useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import { formatBitrate, formatTime, since } from "../lib/format";
import type { BulkResult, Stream, StreamGroup, User } from "../types/api";
import { ConfirmDialog, CopyButton, copyText, MoreMenu, Pill, StatusBadge, useToast } from "./common";
import { GoButton, leadText } from "./LeadIn";
import { CheckIcon, ChevronIcon, ClockIcon, CopyIcon, EyeIcon, FilmIcon, GridIcon, LockIcon, MenuIcon, PlayIcon, RestartIcon, SearchIcon, StopIcon, TrashIcon } from "./icons";
import { codeTone } from "./Naming";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { StreamSetControls, useBulkRun } from "./BulkControls";
import { frameHealth, readersTitle, type FrameHealth } from "../lib/edge";

const HEALTH_TONE = { healthy: "muted", slow: "warn", frozen: "bad" } as const;

/** One small chip: the publisher's actual fps and whether frames arrive in real time. */
/** The fps chip of a card, fed by the live counters: only this chip redraws every 2 s, not the card. */
function LiveFrameChip({ s, expected, className }: { s: Stream; expected: number | null | undefined; className?: string }) {
  const live = useLiveStream(s.id) ?? s;
  return <FrameHealthChip h={frameHealth(live, expected)} className={className} />;
}

export function FrameHealthChip({ h, className }: { h: FrameHealth | null; className?: string }) {
  if (!h) return null;
  const text = h.state === "frozen" ? `frozen ${Math.round(h.ageS ?? 0)} s`
    : `${h.fps.toFixed(1)} fps${h.state === "slow" ? " · slow" : ""}`;
  return <span className={className} data-testid="frame-health" data-state={h.state}><Pill tone={HEALTH_TONE[h.state]} title={h.reason}>{text}</Pill></span>;
}

type Action = "start" | "stop" | "restart" | "delete";

/** Can this user change this stream? (protected streams: owner or admin only) */
export function canControl(s: Stream, user: User | null): boolean {
  if (!user?.can_write) return false;
  return !s.protected || user.is_admin || user.username === s.owner;
}

export function useStreamActions(onChanged: () => void) {
  const addr = useRtspAddress();
  const toast = useToast();
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const [confirm, setConfirm] = useState<Stream | null>(null);

  const run = async (s: Stream, action: Action, version: number | undefined = s.version): Promise<Stream | null> => {
    setBusy((b) => ({ ...b, [s.id]: true }));
    try {
      // If-Match: the version this user is looking at. If someone else changed
      // the stream meanwhile the server refuses instead of silently overriding.
      if (action === "delete") {
        await api.remove(s.id, version);
        toast("ok", `${s.name} deleted (the source video was kept).`);
        return null;
      }
      const updated = await api.action(s.id, action, version);
      if (action === "stop") {
        toast("info", `${s.name} stopped.`, { label: "Undo", run: () => { run(updated, "start", updated.version); } });
      } else {
        toast("ok", `${s.name}: ${action} requested.`);
      }
      return updated;
    } catch (e) {
      toast("bad", `${s.name}: ${(e as ApiError).message}`);
      return null;
    } finally {
      setBusy((b) => ({ ...b, [s.id]: false }));
      onChanged();
    }
  };

  const request = (s: Stream, action: Action) => {
    if (action === "delete") setConfirm(s);
    else run(s, action);
  };

  const dialog = confirm && (
    <ConfirmDialog
      title={`Delete ${confirm.name}?`} danger confirmLabel="Delete stream"
      message={<>
        The publisher stops and the stream configuration is removed. Cameras using{" "}
        <span className="font-mono text-fg">{addr.url(confirm.rtsp_url)}</span> go offline.
        {confirm.owner && confirm.owner !== "anonymous" && <> It was created by <b className="text-fg">{confirm.owner}</b>.</>} The source video file is <b className="text-fg">not</b> deleted.
      </>}
      onCancel={() => setConfirm(null)}
      onConfirm={() => { const c = confirm; setConfirm(null); run(c, "delete"); }}
    />
  );
  return { request, busy, dialog };
}

type Filter = "all" | "live" | "problems" | "stopped" | "mine";

const isLive = (s: Stream) => ["RUNNING", "STARTING", "RESTARTING"].includes(s.status);
/** "Queued #3 · starts in ~5 s" while a stream waits for its start slot (2 s spacing), else null. */
export function queueText(s: Stream): string | null {
  const q = s.runtime?.start_queue;
  if (!q) return null;
  const eta = q.eta_s != null ? ` · starts in ~${Math.max(1, Math.ceil(q.eta_s))} s` : "";
  return q.position === 1 ? `Next to start${eta}` : `Queued #${q.position}${eta}`;
}

/** Normal on the way up: queued or starting (a start that hangs turns RESTARTING), lead-in slate. */
const ON_TRACK = new Set(["RUNNING", "STARTING", "LEAD_IN", "WAITING_FOR_VIEWER"]);

/** Same rule as the backend's analytics counts (api/analytics.py `_problem`). */
export const isProblem = (s: Stream) =>
  s.status === "ERROR" || (s.desired_state === "RUNNING" && !ON_TRACK.has(s.status));

const ACCENT: Record<string, string> = {
  RUNNING: "before:bg-emerald-500", STARTING: "before:bg-blue-500", RESTARTING: "before:bg-amber-500",
  STOPPING: "before:bg-slate-400", STOPPED: "before:bg-slate-300 dark:before:bg-slate-600",
  ERROR: "before:bg-red-500", COMPLETED: "before:bg-violet-500",
  LEAD_IN: "before:bg-sky-500", WAITING_FOR_VIEWER: "before:bg-indigo-500",
};

export function StreamsTable({ streams, user, onSelect, onChanged, mediamtxDown, groups = [], onGroupsChanged, onClone }: {
  streams: Stream[]; user: User | null; onSelect: (s: Stream) => void; onChanged: () => void; mediamtxDown: boolean;
  groups?: StreamGroup[]; onGroupsChanged?: () => void; onClone?: (s: Stream) => void;
}) {
  const addr = useRtspAddress();
  const { request, busy, dialog } = useStreamActions(onChanged);
  const toast = useToast();
  const [query, setQuery] = useState("");
  // ?filter=live|problems|stopped (links from the Overview's numbers) picks the starting tab
  const [params] = useSearchParams();
  const [filter, setFilter] = useState<Filter>(() => {
    const f = params.get("filter");
    return f === "live" || f === "problems" || f === "stopped" ? f : "all";
  });
  const [groupFilter, setGroupFilter] = useState<number | null>(null);
  const [analyticsFilter, setAnalyticsFilter] = useState<number | null>(null);
  const { analytics, videos } = useRig();
  const videoFps = new Map((videos ?? []).map((v) => [v.id, v.fps]));
  // the stored thumbnail of each stream's video: no frame grab from the live stream (keeps the page light)
  const videoThumb = new Map((videos ?? []).map((v) => [v.id, v.thumbnail_url]));
  // stream.slot_id -> its analytics, for labels and the analytics filter
  const slotAnalytics = new Map<number, { id: number; name: string; code?: string | null }>();
  const analyticsOrder = new Map<number, number>();
  for (const c of analytics ?? []) for (const a of c.analytics) analyticsOrder.set(a.id, analyticsOrder.size);
  for (const c of analytics ?? []) for (const a of c.analytics) for (const sid of a.slot_ids) slotAnalytics.set(sid, a);
  const analyticsOf = (s: Stream) => (s.slot_id != null ? slotAnalytics.get(s.slot_id) ?? null : null);
  const usedAnalytics = [...new Map(streams.map(analyticsOf).filter(Boolean).map((a) => [a!.id, a!])).values()]
    .sort((x, y) => x.name.localeCompare(y.name));
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [view, setViewState] = useState<"cards" | "table">(() => (readPref(VIEW_KEY) === "table" ? "table" : "cards"));
  const [grouped, setGroupedState] = useState<boolean>(() => readPref(GROUP_KEY) !== "off");
  const [folded, setFolded] = useState<string[]>(() => { try { return JSON.parse(readPref(FOLD_KEY) ?? "[]"); } catch { return []; } });
  const setView = (v: "cards" | "table") => { setViewState(v); writePref(VIEW_KEY, v); };
  const setGrouped = (g: boolean) => { setGroupedState(g); writePref(GROUP_KEY, g ? "on" : "off"); };
  const toggleFold = (k: string) => setFolded((f) => {
    const next = f.includes(k) ? f.filter((x) => x !== k) : [...f, k];
    writePref(FOLD_KEY, JSON.stringify(next));
    return next;
  });
  const selecting = !!user?.can_write; // a checkbox on every card for anyone who can act on streams
  const fast = useBulkRun(() => { onGroupsChanged?.(); }); // keep the selection: stop them now, start the same ones later
  const [working, setWorking] = useState(false);
  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const report = (label: string, r: BulkResult) => {
    const failed = r.results.filter((x) => !x.ok);
    if (failed.length) toast("bad", `${label}: ${r.ok} ok, ${failed.length} failed (${failed[0].error})`);
    else toast("ok", `${label}: ${r.ok} stream(s) done.`);
  };
  const bulk = async (action: "start" | "stop" | "restart" | "delete") => {
    if (action === "stop" || action === "restart") {
      fast.run(action, streams.filter((s) => selected.has(s.id)), `${action === "stop" ? "Stop" : "Restart"} the selected streams`);
      return;
    }
    setWorking(true);
    try {
      report(`${action[0].toUpperCase()}${action.slice(1)}`, await api.bulk([...selected], action));
      if (action === "delete") setSelected(new Set());
    }
    catch (e) { toast("bad", (e as ApiError).message); }
    finally { setWorking(false); onChanged(); onGroupsChanged?.(); }
  };
  const toGroup = async (value: string) => {
    if (!value) return;
    setWorking(true);
    try {
      if (value === "__new") {
        const name = window.prompt("Name for the new group (e.g. Store demo, 16 cameras):");
        if (!name) return;
        await api.createGroup(name, [...selected]);
        toast("ok", `Group '${name}' created with ${selected.size} stream(s).`);
      } else {
        await api.setGroup([...selected], value === "__none" ? null : Number(value));
        toast("ok", value === "__none" ? "Removed from group." : "Added to group.");
      }
      setSelected(new Set());
    } catch (e) { toast("bad", (e as ApiError).message); }
    finally { setWorking(false); onChanged(); onGroupsChanged?.(); }
  };
  const group = groups.find((g) => g.id === groupFilter) ?? null;
  const groupAct = async (action: "start" | "stop" | "restart") => {
    if (!group) return;
    setWorking(true);
    try { report(`${group.name}: ${action}`, await api.groupAction(group.id, action)); }
    catch (e) { toast("bad", (e as ApiError).message); }
    finally { setWorking(false); onChanged(); onGroupsChanged?.(); }
  };
  const copyGroupUrls = async () => {
    if (!group) return;
    try {
      const text = await api.groupUrls(group.id, addr.param);
      if (await copyText(text)) toast("ok", `Copied ${text.trim().split("\n").length} RTSP URLs.`);
    } catch (e) { toast("bad", (e as ApiError).message); }
  };
  const counts = useMemo(() => ({
    all: streams.length,
    live: streams.filter(isLive).length,
    problems: streams.filter(isProblem).length,
    stopped: streams.filter((s) => !isLive(s)).length,
    mine: streams.filter((s) => s.owner === user?.username).length,
  }), [streams, user]);
  const q = query.trim().toLowerCase();
  const shown = streams.filter((s) =>
    (groupFilter === null || s.group_id === groupFilter) &&
    (analyticsFilter === null || analyticsOf(s)?.id === analyticsFilter) &&
    (filter === "all" || (filter === "live" && isLive(s)) || (filter === "problems" && isProblem(s)) ||
      (filter === "stopped" && !isLive(s)) || (filter === "mine" && s.owner === user?.username)) &&
    (!q || s.name.toLowerCase().includes(q) || s.source_rel_path.toLowerCase().includes(q) ||
      (s.note ?? "").toLowerCase().includes(q) || (s.owner ?? "").toLowerCase().includes(q)))
    // natural order: cam-2 before cam-10
    .sort((x, y) => x.name.localeCompare(y.name, undefined, { numeric: true, sensitivity: "base" }));
  // sections: one per analytics (in the Overview's order), streams in no analytics last
  const sections: { key: string; title: string; code?: string | null; link?: string; items: Stream[] }[] = [];
  // until the analytics list has loaded, a flat list (not everything under "Not in any analytics")
  if (grouped && analytics) {
    const by = new Map<string, Stream[]>();
    for (const s of shown) {
      const a = analyticsOf(s);
      const k = a ? `a${a.id}` : "none";
      by.set(k, [...(by.get(k) ?? []), s]);
    }
    const keys = [...by.keys()].sort((x, y) => (x === "none" ? 1 : y === "none" ? -1
      : (analyticsOrder.get(Number(x.slice(1))) ?? 0) - (analyticsOrder.get(Number(y.slice(1))) ?? 0)));
    for (const k of keys) {
      const items = by.get(k)!;
      const a = k === "none" ? null : analyticsOf(items[0]);
      sections.push(a ? { key: k, title: a.name, code: a.code, link: `/analytics/${a.id}`, items }
        : { key: k, title: "Not in any analytics", items });
    }
  } else sections.push({ key: "all", title: "", items: shown });
  const cardCtx: CardCtx = { user, busy, request, onSelect, onClone, onChanged, selecting, selected, toggle,
    analyticsOf: grouped ? () => null : analyticsOf, url: (s) => addr.url(s.rtsp_url), mediamtxDown,
    thumbOf: (s) => (s.video_id != null ? videoThumb.get(s.video_id) ?? null : null), now: Date.now(),
    fpsOf: (s) => (s.video_id != null ? videoFps.get(s.video_id) ?? null : null) };
  const allShownSelected = shown.length > 0 && shown.every((s) => selected.has(s.id));
  const someShownSelected = shown.some((s) => selected.has(s.id));

  const chips: [Filter, string][] = [["all", "All"], ["live", "Live"], ["problems", "Needs attention"], ["stopped", "Stopped"]];
  if (user?.auth_enabled) chips.push(["mine", "Mine"]);
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-subtle" />
          <input className="input pl-8" placeholder="Search name, file, note…" value={query}
            onChange={(e) => setQuery(e.target.value)} aria-label="Search streams" />
        </div>
        <div className="flex flex-wrap gap-1 rounded-lg bg-surface-2 p-1 ring-1 ring-inset ring-line" role="tablist" aria-label="Filter streams">
          {chips.map(([f, label]) => {
            const active = filter === f;
            const alert = f === "problems" && counts.problems > 0;
            return (
              <button key={f} role="tab" aria-selected={active} onClick={() => setFilter(f)}
                className={`rounded-md px-2.5 py-1 text-xs font-medium transition ${active
                  ? "bg-surface text-fg shadow-sm" : alert ? "text-red-600 hover:text-red-700 dark:text-red-400" : "text-muted hover:text-fg"}`}>
                {label} <span className={`ml-0.5 ${alert ? "font-bold" : "text-subtle"}`}>{counts[f]}</span>
              </button>
            );
          })}
        </div>
        {usedAnalytics.length > 0 && (
          <select className="input h-8 w-auto py-1 text-xs" value={analyticsFilter ?? ""} aria-label="Filter by analytics"
            onChange={(e) => setAnalyticsFilter(e.target.value ? Number(e.target.value) : null)}>
            <option value="">All analytics</option>
            {usedAnalytics.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </select>
        )}
        <div className="ml-auto flex items-center gap-2">
          <label className="flex cursor-pointer items-center gap-1.5 text-xs font-medium text-muted">
            <input type="checkbox" className="h-3.5 w-3.5 accent-blue-600" checked={grouped} onChange={(e) => setGrouped(e.target.checked)} />
            Group by analytics
          </label>
          <div className="flex rounded-lg ring-1 ring-inset ring-line" role="group" aria-label="View">
            <button className={`rounded-l-lg px-2 py-1.5 ${view === "cards" ? "bg-brand-soft text-brand" : "text-muted hover:text-fg"}`}
              onClick={() => setView("cards")} aria-pressed={view === "cards"} aria-label="Card view" title="Cards"><GridIcon /></button>
            <button className={`rounded-r-lg px-2 py-1.5 ${view === "table" ? "bg-brand-soft text-brand" : "text-muted hover:text-fg"}`}
              onClick={() => setView("table")} aria-pressed={view === "table"} aria-label="Table view" title="Table: one line per stream"><MenuIcon /></button>
          </div>
        </div>
      </div>

      {user?.can_write && shown.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-xl border border-line bg-surface px-3 py-2" aria-label="Bulk actions">
          <label className="flex cursor-pointer items-center gap-2 text-sm font-medium">
            <input type="checkbox" className="h-4 w-4 accent-blue-600"
              checked={allShownSelected} ref={(el) => { if (el) el.indeterminate = someShownSelected && !allShownSelected; }}
              onChange={() => setSelected(allShownSelected ? new Set() : new Set(shown.map((s) => s.id)))}
              aria-label={allShownSelected ? "Deselect all" : `Select all ${shown.length} shown`} />
            {selected.size ? <span>{selected.size} selected</span> : <span>Select all <span className="text-muted">({shown.length})</span></span>}
          </label>
          {selected.size > 0 && (
            <button className="btn-ghost btn-xs" onClick={() => setSelected(new Set())}><CheckIcon className="h-3.5 w-3.5" /> Deselect all</button>
          )}
          <span className="hidden h-5 w-px bg-line sm:block" />
          <span className="text-xs font-semibold uppercase tracking-wide text-subtle" title="Act on every stream the filters above are showing">
            {shown.length === streams.length ? "All streams" : `All ${shown.length} shown`}
          </span>
          <StreamSetControls streams={shown} showRestart
            label={shown.length === streams.length ? "all streams" : `the ${shown.length} streams shown (${[q && `search "${q}"`, filter !== "all" && filter, group?.name, analyticsFilter !== null && usedAnalytics.find((a) => a.id === analyticsFilter)?.name].filter(Boolean).join(", ")})`} />
        </div>
      )}

      {groups.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-muted"><GridIcon className="h-3.5 w-3.5" /> Groups</span>
          <button onClick={() => setGroupFilter(null)} className={`rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ${groupFilter === null ? "bg-fg text-surface ring-fg" : "text-muted ring-line hover:text-fg"}`}>All</button>
          {groups.map((g) => (
            <button key={g.id} onClick={() => setGroupFilter(g.id === groupFilter ? null : g.id)}
              className={`rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ${groupFilter === g.id ? "bg-brand-strong text-white ring-brand-strong" : "text-muted ring-line hover:text-fg"}`}>
              {g.name} <span className="opacity-70">{g.running}/{g.total}</span>
            </button>
          ))}
          {group && user?.can_write && (
            <div className="flex flex-wrap gap-1">
              <button className="btn-secondary btn-xs" disabled={working} onClick={() => groupAct("start")}><PlayIcon className="h-3.5 w-3.5" /> Start all</button>
              <button className="btn-secondary btn-xs" disabled={working} onClick={() => groupAct("stop")}><StopIcon className="h-3.5 w-3.5" /> Stop all</button>
              <button className="btn-secondary btn-xs" disabled={working} onClick={() => groupAct("restart")}><RestartIcon className="h-3.5 w-3.5" /> Restart all</button>
              <button className="btn-secondary btn-xs" onClick={copyGroupUrls} title="Copy every RTSP URL of the group (one per line)"><CopyIcon className="h-3.5 w-3.5" /> Copy URLs</button>
              <button className="btn-ghost btn-xs text-red-600" disabled={working} onClick={async () => {
                if (!window.confirm(`Delete group '${group.name}'? Its streams keep running.`)) return;
                await api.deleteGroup(group.id).catch((e) => toast("bad", (e as ApiError).message));
                setGroupFilter(null); onGroupsChanged?.(); onChanged();
              }}>Delete group</button>
            </div>
          )}
        </div>
      )}

      {shown.length === 0 ? (
        <div className="card p-8 text-center text-sm text-muted">No streams match this filter.</div>
      ) : (
        <div className="space-y-5">
          {sections.map((sec) => {
            const body = view === "table"
              ? <StreamRowsTable streams={sec.items} c={cardCtx} />
              : (
                <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">
                  {sec.items.map((s) => <StreamCard key={s.id} s={s} c={cardCtx} />)}
                </div>
              );
            if (sec.key === "all") return <div key={sec.key}><h2 className="sr-only">All streams</h2>{body}</div>;
            const open = !!q || !folded.includes(sec.key);
            const liveN = sec.items.filter(isLive).length;
            const probN = sec.items.filter(isProblem).length;
            return (
              <section key={sec.key} aria-label={`Streams of ${sec.title}`} className="space-y-3">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-line pb-2">
                  <h2 className="text-[1em]"><button className="flex items-center gap-1.5 font-semibold" onClick={() => toggleFold(sec.key)} aria-expanded={open}>
                    <ChevronIcon className={`h-4 w-4 text-muted transition-transform ${open ? "rotate-90" : ""}`} />{sec.title}
                  </button></h2>
                  {sec.code && <span className={`rounded px-1.5 py-0.5 font-mono text-[11px] font-bold ring-1 ring-inset ${codeTone(sec.code)}`}>{sec.code}</span>}
                  <span className="text-sm text-muted"><b className={liveN ? "text-emerald-700 dark:text-emerald-400" : "text-fg"}>{liveN}</b> live of {sec.items.length}</span>
                  {probN > 0 && <span className="rounded-full bg-red-500 px-2 text-xs font-semibold text-white">{probN} need attention</span>}
                  {sec.link && <Link to={sec.link} className="text-xs text-brand hover:underline">Open analytics</Link>}
                  {user?.can_write && <div className="ml-auto"><StreamSetControls streams={sec.items} label={`the streams of ${sec.title}`} /></div>}
                </div>
                {open && body}
              </section>
            );
          })}
        </div>
      )}
      {selecting && selected.size > 0 && (
        <div className="sticky bottom-4 z-10 mx-auto flex w-fit flex-wrap items-center gap-2 rounded-xl border border-line bg-surface px-4 py-2.5 shadow-pop">
          <span className="text-sm font-semibold">{selected.size} selected</span>
          <button className="btn-secondary btn-xs" disabled={working || !!fast.working} onClick={() => bulk("start")}><PlayIcon className="h-3.5 w-3.5" /> Start</button>
          <button className="btn-secondary btn-xs" disabled={working || !!fast.working} onClick={() => bulk("stop")}><StopIcon className="h-3.5 w-3.5" /> Stop</button>
          <button className="btn-secondary btn-xs" disabled={working || !!fast.working} onClick={() => bulk("restart")}><RestartIcon className="h-3.5 w-3.5" /> Restart</button>
          <select className="input w-auto py-0.5 text-xs" value="" disabled={working} onChange={(e) => toGroup(e.target.value)} aria-label="Add to group">
            <option value="">Add to group…</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            <option value="__new">+ New group…</option>
            <option value="__none">Remove from group</option>
          </select>
          <button className="btn-ghost btn-xs text-red-600" disabled={working} onClick={() => {
            if (window.confirm(`Delete ${selected.size} stream(s)? Source videos are kept.`)) bulk("delete");
          }}><TrashIcon className="h-3.5 w-3.5" /> Delete</button>
          {!allShownSelected
            ? <button className="btn-ghost btn-xs" onClick={() => setSelected(new Set(shown.map((s) => s.id)))}>Select all {shown.length}</button>
            : null}
          <button className="btn-ghost btn-xs" onClick={() => setSelected(new Set())}>Deselect all</button>
        </div>
      )}
      {dialog}
      {fast.dialog}
    </div>
  );
}

function IconButton({ label, onClick, disabled, tone, children }: {
  label: string; onClick: () => void; disabled?: boolean; tone?: "go" | "danger"; children: React.ReactNode;
}) {
  const color = tone === "go" ? "text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-400"
    : tone === "danger" ? "text-red-600 hover:bg-red-500/10 dark:text-red-400" : "text-muted hover:bg-surface-2 hover:text-fg";
  return (
    <button type="button" onClick={onClick} disabled={disabled} title={label} aria-label={label}
      className={`rounded-lg border border-line bg-surface p-1.5 transition disabled:opacity-40 ${color}`}>
      {children}
    </button>
  );
}

// ------------------------------------------------------------------ view preferences (per browser)
const VIEW_KEY = "rig02.streams.view";
const GROUP_KEY = "rig02.streams.grouped";
const FOLD_KEY = "rig02.streams.folded";
function readPref(k: string): string | null {
  try { return localStorage.getItem(k); } catch { return null; }
}
function writePref(k: string, v: string) {
  try { localStorage.setItem(k, v); } catch { /* private mode */ }
}

interface CardCtx {
  user: User | null;
  busy: Record<number, boolean>;
  request: (s: Stream, action: Action) => void;
  onSelect: (s: Stream) => void;
  onClone?: (s: Stream) => void;
  onChanged: () => void;
  selecting: boolean;
  selected: Set<number>;
  toggle: (id: number) => void;
  /** the stream's analytics, or null (also null when the page is grouped by analytics already) */
  analyticsOf: (s: Stream) => { id: number; name: string } | null;
  url: (s: Stream) => string;
  mediamtxDown: boolean;
  thumbOf: (s: Stream) => string | null;
  now: number;
  /** the source video's fps (expected output fps), null when unknown */
  fpsOf: (s: Stream) => number | null;
}

/** "Up 3m 34s" for a running stream, "last ran 7h ago" for a stopped one. */
function upText(s: Stream, now: number): string | null {
  if (s.status === "RUNNING") return `up ${since(s.running_since, now)}`;
  if (!isLive(s) && (s.last_stop_at || s.last_start_at)) return `last ran ${since(s.last_stop_at ?? s.last_start_at, now)} ago`;
  return null;
}

/** Everything the compact views leave out, for the hover title. */
function detailsTitle(s: Stream): string {
  return [
    `Source: ${s.source_rel_path}`,
    `Range: ${formatTime(s.effective_start_s, false)} → ${s.end_offset_s !== null ? formatTime(s.effective_end_s, false) : "end"}`,
    s.playback_mode === "loop" ? `Loops: ${s.runtime.loop_count}` : "Plays once",
    s.runtime.ingest_bps != null && s.status === "RUNNING" ? `Bitrate: ${formatBitrate(s.runtime.ingest_bps)}` : null,
    `Viewers peak today: ${s.runtime.viewer_peak_today}`,
    s.runtime.preview_watchers ? `Watching the preview here: ${s.runtime.preview_watchers}` : null,
    s.owner ? `Created by ${s.owner}` : null,
  ].filter(Boolean).join("\n");
}

function Notices({ s, can, onChanged }: { s: Stream; can: boolean; onChanged: () => void }) {
  const problem = isProblem(s) && (s.status === "ERROR" || s.status_detail);
  return (
    <>
      {leadText(s) && (
        <div className="mt-2 flex items-center gap-2 rounded-md bg-sky-500/10 px-2 py-1 text-xs text-sky-800 dark:text-sky-300"
          onClick={(e) => e.stopPropagation()} title="The stream's slate is on its URL; the video follows the lead-in">
          <span className="flex-1">{leadText(s)}</span>
          {can && <GoButton s={s} onDone={onChanged} />}
        </div>
      )}
      {queueText(s) && (
        <p className="mt-2 rounded-md bg-blue-500/10 px-2 py-1 text-xs text-blue-700 dark:text-blue-300"
          title="Camera starts are kept 2 s apart so the edge never gets the same frame from two cameras at once">{queueText(s)}</p>
      )}
      {s.runtime?.spacing_warning && s.status !== "ERROR" && (
        <p className="mt-2 rounded-md bg-amber-500/10 px-2 py-1 text-xs text-amber-800 dark:text-amber-300">{s.runtime.spacing_warning}</p>
      )}
      {problem && (
        <p className={`mt-2 rounded-md px-2 py-1 text-xs ${s.status === "ERROR" ? "bg-red-500/10 text-red-700 dark:text-red-300" : "bg-amber-500/10 text-amber-800 dark:text-amber-300"}`}>
          {s.status === "ERROR" ? (s.last_error ?? s.status_detail) : s.status_detail}
        </p>
      )}
    </>
  );
}

/** Start/Stop (the one main action) and a ⋯ menu with the rest. */
function Actions({ s, c }: { s: Stream; c: CardCtx }) {
  const can = canControl(s, c.user);
  if (!can) {
    return s.protected && c.user?.can_write
      ? <span className="shrink-0 text-subtle" title={`Protected by ${s.owner}`}><LockIcon /></span> : null;
  }
  const b = c.busy[s.id];
  return (
    <div className="flex shrink-0 items-center gap-1" onClick={(e) => e.stopPropagation()}>
      {isLive(s)
        ? <IconButton label="Stop" disabled={b} onClick={() => c.request(s, "stop")}><StopIcon /></IconButton>
        : <IconButton label="Start" tone="go" disabled={b} onClick={() => c.request(s, "start")}><PlayIcon /></IconButton>}
      <MoreMenu label={`More actions for ${s.name}`} items={[
        { label: "Restart", icon: <RestartIcon />, onClick: () => c.request(s, "restart"), disabled: b },
        ...(c.onClone ? [{ label: "Clone…", icon: <CopyIcon />, onClick: () => c.onClone!(s) }] : []),
        { label: "Delete…", icon: <TrashIcon />, onClick: () => c.request(s, "delete"), danger: true, disabled: b },
      ]} />
    </div>
  );
}

function StreamCard({ s, c }: { s: Stream; c: CardCtx }) {
  const can = canControl(s, c.user);
  const a = c.analyticsOf(s);
  const up = upText(s, c.now);
  return (
    <article onClick={() => c.onSelect(s)} tabIndex={0} onKeyDown={(e) => { if (e.key === "Enter") c.onSelect(s); }}
      className={`card group relative cursor-pointer overflow-hidden p-4 pl-5 transition hover:-translate-y-px hover:shadow-pop before:absolute before:inset-y-0 before:left-0 before:w-1 ${ACCENT[s.status]} ${c.selected.has(s.id) ? "ring-2 ring-brand" : ""}`}
      aria-label={`Stream ${s.name}, ${s.status}`} title={detailsTitle(s)}>
      <div className="flex items-start gap-3">
        {c.selecting && (
          <input type="checkbox" className="mt-1 h-4 w-4 accent-blue-600" checked={c.selected.has(s.id)}
            onClick={(e) => e.stopPropagation()} onChange={() => c.toggle(s.id)} aria-label={`Select ${s.name}`} />
        )}
        <span className="hidden h-11 w-[4.5rem] shrink-0 overflow-hidden rounded-md bg-slate-800 sm:block" data-testid="card-thumb">
          {c.thumbOf(s) && (
            <img src={c.thumbOf(s)!} alt="" loading="lazy" decoding="async"
              className={`h-full w-full object-cover ${s.status === "RUNNING" ? "" : "opacity-40 grayscale"}`}
              onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
          )}
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="truncate font-mono text-[15px] font-semibold">{s.name}</h3>
            <StatusBadge status={s.status} title={s.status_detail} />
            <LiveFrameChip s={s} expected={c.fpsOf(s)} />
            {s.clock ? <Pill tone="info" title={`The rig's clock (${s.clock_tz}) is drawn on every frame (re-encoded live)`}>clock {s.clock_tz}</Pill>
              : s.output_mode === "transcode" && <Pill tone="warn" title="Re-encoded live: costs CPU">transcode</Pill>}
            {a && (
              <Link to={`/analytics/${a.id}`} onClick={(e) => e.stopPropagation()}
                className="rounded-md bg-brand-soft px-1.5 py-0.5 text-xs font-medium text-brand hover:underline">{a.name}</Link>
            )}
            {s.protected && c.user?.auth_enabled && <Pill tone="info" title={`Protected by ${s.owner}`}><LockIcon className="h-3 w-3" />{s.owner}</Pill>}
          </div>
          {s.note && <p className="mt-0.5 line-clamp-1 text-xs text-muted" title={s.note}>“{s.note}”</p>}
        </div>
        <Actions s={s} c={c} />
      </div>

      <div className="mt-3 flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 ring-1 ring-inset ring-line" onClick={(e) => e.stopPropagation()}>
        <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted" title={c.url(s)}>{c.url(s)}</span>
        <CopyButton text={c.url(s)} label="Copy URL" />
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted">
        <span className="flex min-w-0 items-center gap-1"><FilmIcon className="h-3.5 w-3.5 text-subtle" /><span className="max-w-[14rem] truncate">{s.source_rel_path}</span></span>
        <span className="flex items-center gap-1" title={readersTitle(s)} data-testid="viewers">
          <EyeIcon className="h-3.5 w-3.5 text-subtle" />{c.mediamtxDown ? "?" : s.runtime.viewer_count}
        </span>
        {up && <span className="flex items-center gap-1"><ClockIcon className="h-3.5 w-3.5 text-subtle" />{up}</span>}
      </div>
      <Notices s={s} can={can} onChanged={c.onChanged} />
    </article>
  );
}

/** One line per stream: fits ~50 streams on a laptop screen. */
function StreamRowsTable({ streams, c }: { streams: Stream[]; c: CardCtx }) {
  const showAnalytics = streams.some((s) => c.analyticsOf(s));
  return (
    <div className="card overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b border-line text-left text-xs uppercase tracking-wide text-subtle">
          <tr>
            {c.selecting && <th className="w-8 px-3 py-2"><span className="sr-only">Select</span></th>}
            <th className="px-3 py-2 font-semibold">Stream</th>
            <th className="px-3 py-2 font-semibold">Status</th>
            {showAnalytics && <th className="hidden px-3 py-2 font-semibold lg:table-cell">Analytics</th>}
            <th className="hidden px-3 py-2 font-semibold md:table-cell">Video</th>
            <th className="px-3 py-2 text-right font-semibold" title="RTSP readers now">Viewers</th>
            <th className="hidden px-3 py-2 font-semibold sm:table-cell">Time</th>
            <th className="px-3 py-2 font-semibold"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {streams.map((s) => {
            const a = c.analyticsOf(s);
            const problem = isProblem(s) && (s.status === "ERROR" || s.status_detail);
            return (
              <tr key={s.id} className={`cursor-pointer hover:bg-surface-2 ${c.selected.has(s.id) ? "bg-brand-soft" : ""}`}
                onClick={() => c.onSelect(s)} title={detailsTitle(s)} aria-label={`Stream ${s.name}, ${s.status}`}>
                {c.selecting && (
                  <td className="px-3 py-2" onClick={(e) => e.stopPropagation()}>
                    <input type="checkbox" className="h-4 w-4 accent-blue-600" checked={c.selected.has(s.id)}
                      onChange={() => c.toggle(s.id)} aria-label={`Select ${s.name}`} />
                  </td>
                )}
                <td className="max-w-[16rem] px-3 py-2">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-mono font-semibold">{s.name}</span>
                    <span onClick={(e) => e.stopPropagation()}><CopyButton text={c.url(s)} label="URL" /></span>
                  </div>
                  {problem && <div className="truncate text-xs text-red-600 dark:text-red-400">{s.status === "ERROR" ? (s.last_error ?? s.status_detail) : s.status_detail}</div>}
                  {!problem && (queueText(s) || leadText(s)) && <div className="truncate text-xs text-blue-700 dark:text-blue-300">{queueText(s) ?? leadText(s)}</div>}
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <StatusBadge status={s.status} title={s.status_detail} />
                  <LiveFrameChip s={s} expected={c.fpsOf(s)} className="ml-1" />
                  {s.clock ? <span className="ml-1"><Pill tone="info">clock {s.clock_tz}</Pill></span>
                    : s.output_mode === "transcode" && <span className="ml-1"><Pill tone="warn">transcode</Pill></span>}
                </td>
                {showAnalytics && (
                  <td className="hidden px-3 py-2 lg:table-cell">
                    {a && <Link to={`/analytics/${a.id}`} onClick={(e) => e.stopPropagation()} className="text-xs font-medium text-brand hover:underline">{a.name}</Link>}
                  </td>
                )}
                <td className="hidden max-w-[16rem] truncate px-3 py-2 text-xs text-muted md:table-cell">{s.source_rel_path}</td>
                <td className="px-3 py-2 text-right tabular-nums" title={readersTitle(s)} data-testid="viewers">{c.mediamtxDown ? "?" : s.runtime.viewer_count}</td>
                <td className="hidden whitespace-nowrap px-3 py-2 text-xs text-muted sm:table-cell">{upText(s, c.now)}</td>
                <td className="px-3 py-2"><div className="flex justify-end"><Actions s={s} c={c} /></div></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** A small ⋯ dropdown for the actions people need less often. */
