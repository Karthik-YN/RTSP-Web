import { useState } from "react";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import { Skeleton } from "../components/common";
import { SearchIcon } from "../components/icons";

const LEVEL: Record<string, string> = {
  ERROR: "bg-red-500/10 text-red-700 dark:text-red-300", WARNING: "bg-amber-500/10 text-amber-800 dark:text-amber-300",
  INFO: "bg-slate-500/10 text-muted",
};

/** `embedded`: shown as a tab of the System page (no page title). */
export function EventsPage({ embedded = false }: { embedded?: boolean }) {
  // newest 100 to start (was 300 every 3 s); older ones on request, up to the server's 1000
  const [limit, setLimit] = useState(100);
  const { data, loading } = usePolling(() => api.events(limit), 5000, [limit]);
  const [q, setQ] = useState("");
  const [level, setLevel] = useState<"all" | "problems">("all");
  const rows = (data ?? []).filter((e) =>
    (level === "all" || e.level !== "INFO") &&
    (!q || `${e.kind} ${e.stream_name ?? ""} ${e.username ?? ""} ${e.message}`.toLowerCase().includes(q.toLowerCase())));
  return (
    <div className="space-y-5">
      {embedded ? <p className="text-sm text-muted">Who did what, failures and recoveries (newest first).</p> : (
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Events</h1>
          <p className="text-sm text-muted">Who did what, failures and recoveries (newest first).</p>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-subtle" />
          <input className="input pl-8" placeholder="Filter events…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Filter events" />
        </div>
        <div className="flex gap-1 rounded-lg bg-surface-2 p-1 ring-1 ring-inset ring-line">
          {(["all", "problems"] as const).map((l) => (
            <button key={l} onClick={() => setLevel(l)} className={`rounded-md px-2.5 py-1 text-xs font-medium ${level === l ? "bg-surface text-fg shadow-sm" : "text-muted"}`}>
              {l === "all" ? "All" : "Warnings & errors"}
            </button>
          ))}
        </div>
      </div>
      <div className="card overflow-hidden">
        {loading && !data ? <div className="space-y-2 p-4">{[0, 1, 2, 3, 4].map((i) => <Skeleton key={i} className="h-6" />)}</div> : (
          <ul className="divide-y divide-line">
            {rows.map((e) => (
              <li key={e.id} className="flex flex-wrap items-start gap-x-3 gap-y-1 px-4 py-2.5 text-sm">
                <span className="w-36 shrink-0 font-mono text-xs text-subtle">{new Date(e.ts).toLocaleString()}</span>
                <span className={`rounded px-1.5 py-0.5 font-mono text-[11px] ${LEVEL[e.level] ?? LEVEL.INFO}`}>{e.kind}</span>
                {e.stream_name && <span className="font-mono text-xs font-semibold">{e.stream_name}</span>}
                <span className="min-w-0 flex-1">{e.message}</span>
                {e.username && e.username !== "anonymous" && <span className="text-xs text-muted">by {e.username}</span>}
              </li>
            ))}
            {rows.length === 0 && <li className="p-8 text-center text-sm text-muted">No events match.</li>}
          </ul>
        )}
        {data && data.length >= limit && limit < 1000 && (
          <div className="border-t border-line p-2 text-center">
            <button className="btn-secondary btn-xs" onClick={() => setLimit((l) => Math.min(1000, l + 200))}>Show older events</button>
          </div>
        )}
      </div>
    </div>
  );
}
