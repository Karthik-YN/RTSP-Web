import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { formatClock, formatDate, useRigClock } from "../lib/rigClock";

/**
 * The rig's clock, big, with milliseconds, in IST and UTC. For latency checks: take one photo (or screen capture)
 * showing this page next to a stream that has "Server clock in picture"; the difference is the delay.
 */
export function ClockPage() {
  const [clock, remeasure] = useRigClock();
  const [now, setNow] = useState(Date.now());
  const raf = useRef(0);
  useEffect(() => {
    const tick = () => { setNow(Date.now()); raf.current = requestAnimationFrame(tick); };
    raf.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf.current);
  }, []);
  const rig = now + (clock?.offsetMs ?? 0);
  const laptopOff = clock ? -clock.offsetMs : null; // this device minus the rig
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Rig clock</h1>
          <p className="max-w-3xl text-sm text-muted">The server's own clock (the one drawn on streams with <b>Server clock in picture</b>).
            Put this page next to such a stream and take one photo or screen capture: the difference between the two times
            is the delay from the rig to that screen.</p>
        </div>
        <Link to="/system" className="btn-secondary">Health and NTP check</Link>
      </div>
      <div className="card grid gap-4 p-6 md:grid-cols-2" data-testid="rig-clock">
        {(["IST", "UTC"] as const).map((tz) => (
          <div key={tz} className="rounded-xl bg-slate-950 p-6 text-center text-white">
            <div className="text-sm font-semibold tracking-wide text-slate-300">{tz === "IST" ? "IST · UTC+5:30" : "UTC"}</div>
            <div className="mt-2 font-mono text-5xl font-bold tabular-nums sm:text-6xl" aria-live="off">{formatClock(rig, tz)}</div>
            <div className="mt-1 font-mono text-sm text-slate-400">{formatDate(rig, tz)}</div>
          </div>
        ))}
      </div>
      <div className="card space-y-1 p-4 text-sm">
        {clock ? (
          <>
            <div>Measured against the rig {clock.source === "server" ? "with millisecond time stamps" : "from the HTTP Date header (whole seconds until the server update)"}:
              {" "}accurate to <b>±{Math.round(clock.errorMs)} ms</b>.</div>
            <div>This device's own clock is <b>{Math.abs(laptopOff!) < 1 ? "the same as" : `${Math.round(Math.abs(laptopOff!))} ms ${laptopOff! > 0 ? "ahead of" : "behind"}`}</b> the rig.
              {Math.abs(laptopOff!) > 100 && <> Sync it: <span className="font-mono">sudo timedatectl set-ntp true</span> (Linux) or "Sync now" in Windows date &amp; time settings.</>}</div>
            <button className="text-brand hover:underline" onClick={remeasure}>Measure again</button>
          </>
        ) : <div className="text-muted">Measuring the rig's clock…</div>}
      </div>
    </div>
  );
}
