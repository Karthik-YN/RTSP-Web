/**
 * The rig's clock in the browser: the server's time, not this computer's. Measured like NTP: ask the server for its
 * time, assume it answered half-way through the round trip, keep the measurement with the shortest round trip.
 * Older servers without /api/system/time: the HTTP Date header (whole seconds only).
 */
import { useEffect, useState } from "react";

export interface RigClock {
  /** server time = Date.now() + offsetMs */
  offsetMs: number;
  /** ± uncertainty of offsetMs (half the best round trip; ~500 ms when only the Date header is available) */
  errorMs: number;
  source: "server" | "date-header";
}

let cached: RigClock | null = null;
let inflight: Promise<RigClock | null> | null = null;

async function measure(): Promise<RigClock | null> {
  let best: RigClock | null = null;
  for (let i = 0; i < 5; i++) {
    const t0 = Date.now();
    let r: Response;
    try { r = await fetch("/api/system/time", { cache: "no-store" }); } catch { break; }
    const t1 = Date.now();
    if (r.ok) {
      const { epoch_ms } = await r.json() as { epoch_ms: number };
      const m: RigClock = { offsetMs: epoch_ms - (t0 + t1) / 2, errorMs: (t1 - t0) / 2, source: "server" };
      if (!best || m.errorMs < best.errorMs) best = m;
    } else {
      const d = r.headers.get("date");
      if (d) best = { offsetMs: Date.parse(d) + 500 - (t0 + t1) / 2, errorMs: 500 + (t1 - t0) / 2, source: "date-header" };
      break;
    }
  }
  return best;
}

export function measureRigClock(force = false): Promise<RigClock | null> {
  if (cached && !force) return Promise.resolve(cached);
  inflight ??= measure().then((c) => { cached = c ?? cached; return cached; }).finally(() => { inflight = null; });
  return inflight;
}

/** The rig clock, re-measured every 5 minutes (and when the tab comes back); the second value re-measures now. */
export function useRigClock(): [RigClock | null, () => void] {
  const [c, setC] = useState<RigClock | null>(cached);
  const [n, setN] = useState(0);
  useEffect(() => {
    let alive = true;
    const run = (force: boolean) => measureRigClock(force).then((x) => { if (alive && x) setC(x); });
    run(n > 0);
    const t = setInterval(() => run(true), 300000);
    const vis = () => { if (!document.hidden) run(true); };
    document.addEventListener("visibilitychange", vis);
    return () => { alive = false; clearInterval(t); document.removeEventListener("visibilitychange", vis); };
  }, [n]);
  return [c, () => setN((x) => x + 1)];
}

const pad = (n: number, w = 2) => String(n).padStart(w, "0");
/** "18:54:00.288" in IST (UTC+5:30) or UTC, from an epoch in ms. */
export function formatClock(epochMs: number, tz: "IST" | "UTC", ms = true): string {
  const d = new Date(epochMs + (tz === "IST" ? 19800000 : 0));
  const s = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
  return ms ? `${s}.${pad(d.getUTCMilliseconds(), 3)}` : s;
}
export function formatDate(epochMs: number, tz: "IST" | "UTC"): string {
  const d = new Date(epochMs + (tz === "IST" ? 19800000 : 0));
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}
