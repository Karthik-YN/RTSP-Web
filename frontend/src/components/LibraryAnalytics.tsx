// Library <-> analytics: add videos to analytics, review name-based suggestions, filter by analytics.
import { useMemo, useState } from "react";
import { api, ApiError } from "../services/api";
import type { AnalyticsCategory, AnalyticsSuggestion, Video } from "../types/api";
import { ErrorBox, Modal, Spinner } from "./common";

import type { AnalyticsFilter } from "../lib/libraryFilters";
export type { AnalyticsFilter };

export function filterByAnalytics(videos: Video[], f: AnalyticsFilter): Video[] {
  if (!f) return videos;
  if (f === "none") return videos.filter((v) => !v.analytics?.length);
  const id = Number(f);
  return videos.filter((v) => v.analytics?.some((a) => a.id === id));
}

export function AnalyticsFilterSelect({ value, onChange, tree }: {
  value: AnalyticsFilter; onChange: (f: AnalyticsFilter) => void; tree: AnalyticsCategory[] | null;
}) {
  return (
    <select className="input w-auto max-w-[14rem] max-sm:max-w-none max-sm:flex-1" value={value} aria-label="Filter by analytics"
      onChange={(e) => onChange(e.target.value as AnalyticsFilter)}>
      <option value="">All analytics</option>
      <option value="none">Not in any analytics</option>
      {(tree ?? []).map((c) => (
        <optgroup key={c.id} label={c.name}>
          {c.analytics.map((a) => <option key={a.id} value={String(a.id)}>{a.name}</option>)}
        </optgroup>
      ))}
    </select>
  );
}

/** Pick analytics for one or more videos. Analytics every chosen video is already in are ticked and fixed. */
export function AssignDialog({ videos, tree, onClose, onDone }: {
  videos: Video[]; tree: AnalyticsCategory[]; onClose: () => void;
  /** names of the analytics that got new links, and how many links were added */
  onDone: (added: string[], links: number) => void;
}) {
  const have = useMemo(() => {
    const counts = new Map<number, number>();
    for (const v of videos) for (const a of v.analytics ?? []) counts.set(a.id, (counts.get(a.id) ?? 0) + 1);
    return {
      all: new Set([...counts].filter(([, n]) => n === videos.length).map(([id]) => id)),
      some: new Set([...counts].filter(([, n]) => n < videos.length).map(([id]) => id)),
    };
  }, [videos]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const toggle = (id: number) => setPicked((p) => {
    const n = new Set(p);
    if (n.has(id)) n.delete(id); else n.add(id);
    return n;
  });
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.assignVideos(videos.map((v) => v.id), [...picked]);
      onDone([...new Set(r.created.map((c) => c.analytics_name))], r.created.length);
    } catch (e) {
      setErr((e as ApiError).message);
      setBusy(false);
    }
  };
  const one = videos.length === 1;
  return (
    <Modal title={one ? `Add ${videos[0].filename} to analytics` : `Add ${videos.length} videos to analytics`} onClose={onClose}>
      <p className="mb-3 text-sm text-muted">
        Each analytics gets a video placeholder per video, ready to run from its page.
        {!one && " Videos already in an analytics are skipped."}
      </p>
      <div className="max-h-[55vh] space-y-3 overflow-y-auto pr-1" data-testid="assign-list">
        {tree.map((c) => (
          <fieldset key={c.id}>
            <legend className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{c.name}</legend>
            <div className="grid grid-cols-1 gap-x-3 sm:grid-cols-2">
              {c.analytics.map((a) => {
                const all = have.all.has(a.id);
                return (
                  <label key={a.id} className={`flex items-center gap-2 rounded px-1 py-1.5 text-sm ${all ? "text-muted" : "hover:bg-surface-2"}`}>
                    <input type="checkbox" className="h-4 w-4" checked={all || picked.has(a.id)} disabled={all} onChange={() => toggle(a.id)} />
                    <span className="truncate">{a.name}</span>
                    {all && <span className="text-xs">(already in)</span>}
                    {have.some.has(a.id) && <span className="text-xs text-muted">(some already in)</span>}
                  </label>
                );
              })}
            </div>
          </fieldset>
        ))}
      </div>
      {err && <div className="mt-3"><ErrorBox>{err}</ErrorBox></div>}
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>Cancel</button>
        <button className="btn-primary" disabled={!picked.size || busy} onClick={save}>
          {busy && <Spinner />} Add to {picked.size || ""} analytics
        </button>
      </div>
    </Modal>
  );
}

/** Review suggestions (all ticked); only the ticked ones are linked, and only on a click. */
export function SuggestionsDialog({ items, onClose, onDone }: {
  items: AnalyticsSuggestion[]; onClose: () => void; onDone: (n: number) => void;
}) {
  const key = (s: AnalyticsSuggestion) => `${s.analytics_id}:${s.video_id}`;
  const [picked, setPicked] = useState<Set<string>>(() => new Set(items.map(key)));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const byAnalytics = useMemo(() => {
    const m = new Map<string, AnalyticsSuggestion[]>();
    for (const s of items) m.set(s.analytics_name, [...(m.get(s.analytics_name) ?? []), s]);
    return [...m.entries()];
  }, [items]);
  const toggle = (k: string) => setPicked((p) => {
    const n = new Set(p);
    if (n.has(k)) n.delete(k); else n.add(k);
    return n;
  });
  const save = async () => {
    setBusy(true);
    setErr(null);
    try {
      // one call per analytics: the API links every listed video to every listed analytics
      let n = 0;
      for (const [, list] of byAnalytics) {
        const chosen = list.filter((s) => picked.has(key(s)));
        if (chosen.length) n += (await api.assignVideos(chosen.map((s) => s.video_id), [chosen[0].analytics_id])).created.length;
      }
      onDone(n);
    } catch (e) {
      setErr((e as ApiError).message);
      setBusy(false);
    }
  };
  return (
    <Modal title="Suggested analytics" onClose={onClose}>
      <p className="mb-3 text-sm text-muted">Matched from words in the file names. Untick anything that doesn't belong.</p>
      <div className="max-h-[55vh] space-y-3 overflow-y-auto pr-1" data-testid="suggestions">
        {byAnalytics.map(([name, list]) => (
          <div key={name}>
            <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">{name}</div>
            {list.map((s) => (
              <label key={key(s)} className="flex items-center gap-2 rounded px-1 py-1 text-sm hover:bg-surface-2">
                <input type="checkbox" checked={picked.has(key(s))} onChange={() => toggle(key(s))} />
                <span className="min-w-0 flex-1 truncate">{s.rel_path}</span>
                <span className="text-xs text-muted">"{s.word}"</span>
              </label>
            ))}
          </div>
        ))}
      </div>
      {err && <div className="mt-3"><ErrorBox>{err}</ErrorBox></div>}
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onClose}>Not now</button>
        <button className="btn-primary" disabled={!picked.size || busy} onClick={save}>
          {busy && <Spinner />} Add {picked.size} selected
        </button>
      </div>
    </Modal>
  );
}
