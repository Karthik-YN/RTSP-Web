import { createContext, useContext, useEffect, useMemo, useRef, useSyncExternalStore, type ReactNode } from "react";
import { api, ApiError } from "../services/api";
import type { AnalyticsCategory, AppConfig, MediaMTXStatus, Stream, SystemInfo, User, Video } from "../types/api";
import { usePolling } from "./usePolling";
import { RtspAddressProvider } from "./useRtspAddress";

interface RigData {
  user: User;
  config: AppConfig;
  /** The stream list. Its identity only changes when something people see changes (status, names, viewers…),
   *  not on every frame/byte counter tick, so lists don't redraw every 2 s. */
  streams: Stream[] | null;
  /** The newest stream list, counters included (fps, bitrate, loops). Only for small live chips. */
  liveStreams: Stream[] | null;
  system: SystemInfo | null;
  mediamtx: MediaMTXStatus | null;
  videos: Video[] | null;
  analytics: AnalyticsCategory[] | null;
  streamsLoading: boolean;
  videosLoading: boolean;
  appError: ApiError | null;
  refreshStreams: () => void;
  refreshVideos: () => void;
  refreshAnalytics: () => void;
  refreshAll: () => void;
}
type Key = keyof RigData;

/** Runtime fields that change on every poll while a stream runs; left out when deciding "did the list change". */
const VOLATILE = new Set(["progress", "ingest_bps", "egress_bps", "bytes_received", "bytes_sent", "process",
  "playback_position_s", "loop_count", "loop_period_s", "viewer_peak_today", "suppressed_timestamp_warnings"]);
export function streamsShape(list: Stream[] | null): string {
  return JSON.stringify(list, (k, v) => (VOLATILE.has(k) ? undefined : v));
}

/**
 * A tiny store: a component that calls useRig() redraws only when a field it actually read changes
 * (a CPU update redraws the top bar, not the stream cards).
 */
class RigStore {
  state = {} as RigData;
  versions = {} as Record<Key, number>;
  listeners = new Set<() => void>();
  set(next: RigData) {
    let changed = false;
    for (const k of Object.keys(next) as Key[]) {
      if (this.state[k] !== next[k]) { this.versions[k] = (this.versions[k] ?? 0) + 1; changed = true; }
    }
    this.state = next;
    if (changed) this.listeners.forEach((f) => f());
  }
  subscribe = (f: () => void) => { this.listeners.add(f); return () => { this.listeners.delete(f); }; };
}

const Ctx = createContext<RigStore | null>(null);

/** Polls the live server state once for the whole app (pauses in hidden tabs). */
export function RigDataProvider({ user, config, children }: { user: User; config: AppConfig; children: ReactNode }) {
  const streams = usePolling(api.streams, 2000);
  const system = usePolling(api.system, 3000);
  const mediamtx = usePolling(api.mediamtx, 5000);
  const videos = usePolling(() => api.videos(), 15000);
  const analytics = usePolling(api.analyticsTree, 5000);
  const store = useMemo(() => new RigStore(), []);

  // keep the previous list object while only counters moved
  const stable = useRef<{ shape: string; list: Stream[] | null }>({ shape: "", list: null });
  const shape = streamsShape(streams.data);
  if (shape !== stable.current.shape) stable.current = { shape, list: streams.data };

  const refreshers = useRef({ streams: streams.refresh, system: system.refresh, mediamtx: mediamtx.refresh,
    videos: videos.refresh, analytics: analytics.refresh });
  refreshers.current = { streams: streams.refresh, system: system.refresh, mediamtx: mediamtx.refresh,
    videos: videos.refresh, analytics: analytics.refresh };
  // stable callbacks: components holding them never redraw because of them
  const fns = useMemo(() => ({
    refreshStreams: () => refreshers.current.streams(),
    refreshVideos: () => refreshers.current.videos(),
    refreshAnalytics: () => refreshers.current.analytics(),
    refreshAll: () => { const r = refreshers.current; r.streams(); r.system(); r.mediamtx(); r.analytics(); },
  }), []);

  // not reachable (status 0) or failing (5xx): the dashboard is showing old data — say so, and since when
  const failing = streams.error && (streams.error.status === 0 || streams.error.status >= 500) ? streams.error : null;
  const since = useRef<number | null>(null);
  if (failing && since.current == null) since.current = Date.now();
  if (!failing) since.current = null;
  const err = failing ? Object.assign(failing, { since: since.current }) : null;
  const prevErr = store.state.appError;
  const next: RigData = {
    user, config,
    streams: stable.current.list, liveStreams: streams.data, system: system.data, mediamtx: mediamtx.data,
    videos: videos.data, analytics: analytics.data,
    streamsLoading: streams.loading, videosLoading: videos.loading,
    appError: err && prevErr && prevErr.message === err.message ? prevErr : err,
    ...fns,
  };
  if (!store.state.user) store.set(next); // first render: children read it right away
  useEffect(() => { store.set(next); });
  return <Ctx.Provider value={store}><RtspAddressProvider config={config}>{children}</RtspAddressProvider></Ctx.Provider>;
}

/**
 * The shared rig state, same shape as always. The component is subscribed only to the fields it reads,
 * so `const { user } = useRig()` never redraws for stream or CPU updates.
 */
export function useRig(): RigData {
  const store = useContext(Ctx);
  if (!store) throw new Error("useRig outside RigDataProvider");
  const used = useRef(new Set<Key>());
  const snapshot = () => {
    let s = "";
    used.current.forEach((k) => { s += `${k}:${store.versions[k] ?? 0};`; });
    return s;
  };
  useSyncExternalStore(store.subscribe, snapshot, snapshot);
  const state = store.state;
  return useMemo(() => new Proxy(state, {
    get(target, k) { used.current.add(k as Key); return target[k as Key]; },
  }), [state]);
}

/** The rig's server stats, or null outside the provider (dialogs rendered on their own, tests). */
export function useSystemMaybe(): SystemInfo | null {
  const store = useContext(Ctx);
  const subscribe = useMemo(() => (f: () => void) => (store ? store.subscribe(f) : () => {}), [store]);
  return useSyncExternalStore(subscribe, () => (store ? store.state.system : null), () => null);
}

/** One stream with its newest counters (fps, bitrate, loops), for small live chips. */
export function useLiveStream(id: number): Stream | null {
  const { liveStreams } = useRig();
  return liveStreams?.find((s) => s.id === id) ?? null;
}
