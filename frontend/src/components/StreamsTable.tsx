import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import { formatBitrate, formatTime, since } from "../lib/format";
import type { BulkResult, Stream, StreamGroup, User } from "../types/api";
import { Avatar, ConfirmDialog, CopyButton, copyText, Pill, StatusBadge, useToast } from "./common";
import { CheckIcon, ClockIcon, CopyIcon, EyeIcon, FilmIcon, GridIcon, LockIcon, LoopIcon, MonitorIcon, PlayIcon, RestartIcon, SearchIcon, StopIcon, TrashIcon } from "./icons";

type Action = "start" | "stop" | "restart" | "delete";

/** Can this user change this stream? (protected streams: owner or admin only) */
export function canControl(s: Stream, user: User | null): boolean {
  if (!user?.can_write) return false;
  return !s.protected || user.is_admin || user.username === s.owner;
}

export function useStreamActions(onChanged: () => void) {
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
        <span className="font-mono text-fg">{confirm.rtsp_url}</span> go offline.
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
export const isProblem = (s: Stream) => s.status === "ERROR" || (s.desired_state === "RUNNING" && s.status !== "RUNNING");

const ACCENT: Record<string, string> = {
  RUNNING: "before:bg-emerald-500", STARTING: "before:bg-blue-500", RESTARTING: "before:bg-amber-500",
  STOPPING: "before:bg-slate-400", STOPPED: "before:bg-slate-300 dark:before:bg-slate-600",
  ERROR: "before:bg-red-500", COMPLETED: "before:bg-violet-500",
};

export function StreamsTable({ streams, user, onSelect, onChanged, mediamtxDown, groups = [], onGroupsChanged, onClone }: {
  streams: Stream[]; user: User | null; onSelect: (s: Stream) => void; onChanged: () => void; mediamtxDown: boolean;
  groups?: StreamGroup[]; onGroupsChanged?: () => void; onClone?: (s: Stream) => void;
}) {
  const { request, busy, dialog } = useStreamActions(onChanged);
  const toast = useToast();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<Filter>("all");
  const [groupFilter, setGroupFilter] = useState<number | null>(null);
  const [analyticsFilter, setAnalyticsFilter] = useState<number | null>(null);
  const { analytics } = useRig();
  // stream.slot_id -> its analytics, for labels and the analytics filter
  const slotAnalytics = new Map<number, { id: number; name: string }>();
  for (const c of analytics ?? []) for (const a of c.analytics) for (const sid of a.slot_ids) slotAnalytics.set(sid, a);
  const analyticsOf = (s: Stream) => (s.slot_id != null ? slotAnalytics.get(s.slot_id) ?? null : null);
  const usedAnalytics = [...new Map(streams.map(analyticsOf).filter(Boolean).map((a) => [a!.id, a!])).values()]
    .sort((x, y) => x.name.localeCompare(y.name));
  const [selecting, setSelecting] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [working, setWorking] = useState(false);
  const toggle = (id: number) => setSelected((s) => { const n = new Set(s); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const report = (label: string, r: BulkResult) => {
    const failed = r.results.filter((x) => !x.ok);
    if (failed.length) toast("bad", `${label}: ${r.ok} ok, ${failed.length} failed (${failed[0].error})`);
    else toast("ok", `${label}: ${r.ok} stream(s) done.`);
  };
  const bulk = async (action: "start" | "stop" | "restart" | "delete") => {
    setWorking(true);
    try { report(`${action[0].toUpperCase()}${action.slice(1)}`, await api.bulk([...selected], action)); setSelected(new Set()); }
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
      const text = await api.groupUrls(group.id);
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
      (s.note ?? "").toLowerCase().includes(q) || (s.owner ?? "").toLowerCase().includes(q)));
  const now = Date.now();
  const bucket = Math.floor(now / 30000); // preview thumbnails refresh every 30 s

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
                {label} <span className={`ml-0.5 ${alert ? "font-bold" : "opacity-60"}`}>{counts[f]}</span>
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
        {user?.can_write && (
          <button className={`${selecting ? "btn-primary" : "btn-secondary"} ml-auto`} onClick={() => { setSelecting((x) => !x); setSelected(new Set()); }}>
            <CheckIcon /> {selecting ? "Done" : "Select"}
          </button>
        )}
      </div>

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
        <div className="grid gap-3 xl:grid-cols-2 2xl:grid-cols-3">
          {shown.map((s) => {
            const live = isLive(s);
            const can = canControl(s, user);
            const problem = isProblem(s) && (s.status === "ERROR" || s.status_detail);
            return (
              <article key={s.id} onClick={() => onSelect(s)} tabIndex={0}
                onKeyDown={(e) => { if (e.key === "Enter") onSelect(s); }}
                className={`card group relative cursor-pointer overflow-hidden p-4 pl-5 transition hover:-translate-y-px hover:shadow-pop before:absolute before:inset-y-0 before:left-0 before:w-1 ${ACCENT[s.status]}`}
                aria-label={`Stream ${s.name}, ${s.status}`}>
                <div className="flex items-start gap-3">
                  {selecting && (
                    <input type="checkbox" className="mt-1 h-4 w-4 accent-blue-600" checked={selected.has(s.id)}
                      onClick={(e) => e.stopPropagation()} onChange={() => toggle(s.id)} aria-label={`Select ${s.name}`} />
                  )}
                  {s.status === "RUNNING" && (
                    <img src={`/api/streams/${s.id}/preview?b=${bucket}`} alt="" loading="lazy"
                      className="hidden h-12 w-20 shrink-0 rounded-md bg-slate-800 object-cover sm:block"
                      onError={(e) => { (e.target as HTMLImageElement).style.visibility = "hidden"; }} />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate font-mono text-[15px] font-semibold">{s.name}</h3>
                      <StatusBadge status={s.status} title={s.status_detail} />
                      {s.output_mode === "transcode" && <Pill tone="warn">transcode</Pill>}
                      {analyticsOf(s) && (
                        <Link to={`/analytics/${analyticsOf(s)!.id}`} onClick={(e) => e.stopPropagation()}
                          className="rounded-md bg-brand-soft px-1.5 py-0.5 text-xs font-medium text-brand hover:underline"
                          title="Open this analytics">{analyticsOf(s)!.name}</Link>
                      )}
                      {s.protected && user?.auth_enabled && <Pill tone="info" title={`Protected by ${s.owner}: only they or an admin can change it`}><LockIcon className="h-3 w-3" />{s.owner}</Pill>}
                    </div>
                    {s.note && <p className="mt-1 line-clamp-1 text-xs text-muted" title={s.note}>“{s.note}”</p>}
                  </div>
                  {can ? (
                    <div className="flex shrink-0 gap-1 opacity-80 transition group-hover:opacity-100" onClick={(e) => e.stopPropagation()}>
                      {live
                        ? <IconButton label="Stop" disabled={busy[s.id]} onClick={() => request(s, "stop")}><StopIcon /></IconButton>
                        : <IconButton label="Start" tone="go" disabled={busy[s.id]} onClick={() => request(s, "start")}><PlayIcon /></IconButton>}
                      <IconButton label="Restart" disabled={busy[s.id]} onClick={() => request(s, "restart")}><RestartIcon /></IconButton>
                      {onClone && <IconButton label="Clone" onClick={() => onClone(s)}><CopyIcon /></IconButton>}
                      <IconButton label="Delete" tone="danger" disabled={busy[s.id]} onClick={() => request(s, "delete")}><TrashIcon /></IconButton>
                    </div>
                  ) : s.protected && user?.can_write ? (
                    <span className="shrink-0 text-subtle" title={`Protected by ${s.owner}`}><LockIcon /></span>
                  ) : null}
                </div>

                <div className="mt-3 flex items-center gap-2 rounded-lg bg-surface-2 px-2.5 py-1.5 ring-1 ring-inset ring-line"
                  onClick={(e) => e.stopPropagation()}>
                  <span className="min-w-0 flex-1 truncate font-mono text-xs text-muted" title={s.rtsp_url}>{s.rtsp_url}</span>
                  <CopyButton text={s.rtsp_url} label="Copy URL" />
                </div>

                <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted">
                  <span className="flex min-w-0 items-center gap-1" title={s.source_rel_path}>
                    <FilmIcon className="h-3.5 w-3.5 text-subtle" /><span className="max-w-[13rem] truncate">{s.source_rel_path}</span>
                  </span>
                  <span className="font-mono tabular-nums">
                    {formatTime(s.effective_start_s, false)} → {s.end_offset_s !== null ? formatTime(s.effective_end_s, false) : "end"}
                  </span>
                  <span className="flex items-center gap-1">
                    {s.playback_mode === "loop" ? <><LoopIcon className="h-3.5 w-3.5 text-subtle" />{s.runtime.loop_count} loops</> : "Play once"}
                  </span>
                  {s.status === "RUNNING" && (
                    <span className="flex items-center gap-1"><ClockIcon className="h-3.5 w-3.5 text-subtle" />{since(s.running_since, now)}</span>
                  )}
                  {!isLive(s) && (s.last_stop_at || s.last_start_at) && (
                    <span className="flex items-center gap-1" title="Start it again any time: the URL stays the same">
                      <ClockIcon className="h-3.5 w-3.5 text-subtle" />last ran {since(s.last_stop_at ?? s.last_start_at, now)} ago
                    </span>
                  )}
                  <span className="flex items-center gap-1"
                    title={`RTSP viewers now (e.g. the edge) · peak today ${s.runtime.viewer_peak_today}`}>
                    <EyeIcon className="h-3.5 w-3.5 text-subtle" />{mediamtxDown ? "?" : s.runtime.viewer_count}
                    {s.runtime.viewer_peak_today > s.runtime.viewer_count && <span className="text-subtle">(peak {s.runtime.viewer_peak_today})</span>}
                  </span>
                  {s.runtime.preview_watchers > 0 && (
                    <span className="flex items-center gap-1 text-subtle" title="Watching the preview in the dashboard (not counted as viewers)">
                      <MonitorIcon className="h-3.5 w-3.5" />{s.runtime.preview_watchers}
                    </span>
                  )}
                  {s.runtime.ingest_bps != null && s.status === "RUNNING" && (
                    <span className="tabular-nums" title="Publisher bitrate">{formatBitrate(s.runtime.ingest_bps)}</span>
                  )}
                  {s.owner && !s.protected && user?.auth_enabled && <span className="ml-auto flex items-center gap-1" title={`Created by ${s.owner}`}><Avatar name={s.owner} /></span>}
                </div>

                {problem && (
                  <p className={`mt-2 rounded-md px-2 py-1 text-xs ${s.status === "ERROR" ? "bg-red-500/10 text-red-700 dark:text-red-300" : "bg-amber-500/10 text-amber-800 dark:text-amber-300"}`}>
                    {s.status === "ERROR" ? (s.last_error ?? s.status_detail) : s.status_detail}
                  </p>
                )}
              </article>
            );
          })}
        </div>
      )}
      {selecting && selected.size > 0 && (
        <div className="sticky bottom-4 z-10 mx-auto flex w-fit flex-wrap items-center gap-2 rounded-xl border border-line bg-surface px-4 py-2.5 shadow-pop">
          <span className="text-sm font-semibold">{selected.size} selected</span>
          <button className="btn-secondary btn-xs" disabled={working} onClick={() => bulk("start")}><PlayIcon className="h-3.5 w-3.5" /> Start</button>
          <button className="btn-secondary btn-xs" disabled={working} onClick={() => bulk("stop")}><StopIcon className="h-3.5 w-3.5" /> Stop</button>
          <button className="btn-secondary btn-xs" disabled={working} onClick={() => bulk("restart")}><RestartIcon className="h-3.5 w-3.5" /> Restart</button>
          <select className="input w-auto py-0.5 text-xs" value="" disabled={working} onChange={(e) => toGroup(e.target.value)} aria-label="Add to group">
            <option value="">Add to group…</option>
            {groups.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
            <option value="__new">+ New group…</option>
            <option value="__none">Remove from group</option>
          </select>
          <button className="btn-ghost btn-xs text-red-600" disabled={working} onClick={() => {
            if (window.confirm(`Delete ${selected.size} stream(s)? Source videos are kept.`)) bulk("delete");
          }}><TrashIcon className="h-3.5 w-3.5" /> Delete</button>
          <button className="btn-ghost btn-xs" onClick={() => setSelected(new Set(shown.map((s) => s.id)))}>Select all {shown.length}</button>
        </div>
      )}
      {dialog}
    </div>
  );
}

function IconButton({ label, onClick, disabled, tone, children }: {
  label: string; onClick: () => void; disabled?: boolean; tone?: "go" | "danger"; children: React.ReactNode;
}) {
  const color = tone === "go" ? "text-emerald-600 hover:bg-emerald-500/10 dark:text-emerald-400"
    : tone === "danger" ? "text-red-600 hover:bg-red-500/10 dark:text-red-400" : "text-muted hover:bg-surface-2 hover:text-fg";
  return (
    <button type="button" onClick={onClick} disabled={disabled} title={label} aria-label={label}
      className={`rounded-lg border border-line bg-surface p-1.5 transition disabled:opacity-40 ${color}`}>
      {children}
    </button>
  );
}
