import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../services/api";

/**
 * Poll an async loader every `intervalMs`. Keeps the last good data when a
 * request fails (so the page stays usable during outages) and exposes the
 * error separately. Polling pauses while the tab is hidden.
 */
export function usePolling<T>(loader: () => Promise<T>, intervalMs: number, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(true);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const seq = useRef(0);
  // Same payload as last time: keep the old object, so nothing re-renders
  // (the dashboard polls every 2 s; most polls change nothing).
  const last = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    const my = ++seq.current;
    try {
      const d = await loaderRef.current();
      if (my === seq.current) {
        const json = JSON.stringify(d);
        if (json !== last.current) {
          last.current = json;
          setData(d);
        }
        setError(null);
      }
    } catch (e) {
      if (my === seq.current) setError(e instanceof ApiError ? e : new ApiError(0, String(e)));
    } finally {
      if (my === seq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    refresh();
    const start = () => {
      if (!timer) timer = setInterval(() => { if (!document.hidden) refresh(); }, intervalMs);
    };
    start();
    const onVis = () => { if (!document.hidden) refresh(); };
    document.addEventListener("visibilitychange", onVis);
    return () => {
      if (timer) clearInterval(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, refresh, ...deps]);

  return { data, error, loading, refresh };
}
