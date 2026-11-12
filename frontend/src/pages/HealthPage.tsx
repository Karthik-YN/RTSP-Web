import { useEffect, useState } from "react";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import type { Capacity, CheckStatus, HealthReport, LimitInfo } from "../types/api";
import { ErrorBox, Spinner, useToast } from "../components/common";
import { DownloadIcon, PlayIcon, RefreshIcon, ServerIcon } from "../components/icons";

const TONE: Record<CheckStatus | "PASS" | "WARN" | "FAIL", string> = {
  PASS: "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300",
  WARN: "bg-amber-500/10 text-amber-800 ring-amber-500/30 dark:text-amber-300",
  FAIL: "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300",
  SKIP: "bg-slate-500/10 text-muted ring-slate-500/20",
};

function Status({ s }: { s: CheckStatus }) {
  return <span className={`inline-flex w-12 justify-center rounded-md px-1.5 py-0.5 text-[11px] font-bold ring-1 ring-inset ${TONE[s]}`}>{s}</span>;
}

export function reportText(r: HealthReport): string {
  const lines = [
    `RIG-02 health test (${r.mode}) on ${r.server}`,
    `Started ${new Date(r.started_at).toLocaleString()}, took ${r.duration_s} s`,
    `Result: ${r.overall}  (${r.summary.PASS} pass, ${r.summary.WARN} warn, ${r.summary.FAIL} fail, ${r.summary.SKIP} skipped)`,
    "",
    ...r.checks.map((c) => `${c.status.padEnd(4)}  ${c.name}: ${c.detail}`),
    "",
  ];
  return lines.join("\n");
}

function download(r: HealthReport) {
  const stamp = r.started_at.slice(0, 19).replace(/[:T]/g, "-");
  const blob = new Blob([reportText(r)], { type: "text/plain" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `rig02-health-${stamp}.txt`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function HealthPage() {
  const { user } = useRig();
  const toast = useToast();
  const [report, setReport] = useState<HealthReport | null>(null);
  const [auto, setAuto] = useState<{ quick: HealthReport | null; interval: number } | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.lastHealth().then((r) => {
      setReport(r.full);
      setAuto({ quick: r.quick, interval: r.auto_interval_s });
    }).catch(() => {});
  }, []);

  const run = async () => {
    setRunning(true);
    setErr(null);
    try {
      const r = await api.healthTest();
      setReport(r);
      toast(r.overall === "FAIL" ? "bad" : "ok", `Health test: ${r.overall}`);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Health</h1>
          <p className="text-sm text-muted">Checks the server, MediaMTX, disk, backups and every running stream.</p>
        </div>
        <div className="flex gap-2">
          {report && (
            <button className="btn-secondary" onClick={() => download(report)}><DownloadIcon /> Report</button>
          )}
          <button className="btn-primary" onClick={run} disabled={running}>
            {running ? <Spinner /> : <PlayIcon />} {running ? "Running…" : "Run health test"}
          </button>
        </div>
      </div>

      {err && <ErrorBox>{err}</ErrorBox>}

      {report ? <ReportCard r={report} /> : (
        <div className="card p-8 text-center text-sm text-muted">
          No full test yet. <b>Run health test</b> checks everything, including reading 2 s from each running stream.
        </div>
      )}

      {auto && (
        <div className="text-xs text-muted">
          {auto.interval > 0
            ? <>Automatic quick check every {Math.round(auto.interval / 60)} min (no stream reads): {auto.quick
              ? <>last {new Date(auto.quick.started_at).toLocaleTimeString()}, <b>{auto.quick.overall}</b></> : "not run yet"}.
              Changes are logged in Events and sent to the alert webhook.</>
            : "Automatic checks are off (RTSPM_HEALTH_AUTO_INTERVAL_S=0)."}
        </div>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <CapacityCard canWrite={user.can_write} />
        <BackupCard canWrite={user.can_write} />
      </div>
    </div>
  );
}

function ReportCard({ r }: { r: HealthReport }) {
  return (
    <div className="card overflow-hidden" data-testid="health-report">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <span className={`rounded-lg px-2.5 py-1 text-sm font-bold ring-1 ring-inset ${TONE[r.overall]}`}>{r.overall}</span>
        <span className="text-sm text-muted">
          {r.summary.PASS} pass · {r.summary.WARN} warn · {r.summary.FAIL} fail · {r.summary.SKIP} skipped
        </span>
        <span className="ml-auto text-xs text-subtle">
          {r.mode === "full" ? "Full test" : "Quick check"} · {new Date(r.started_at).toLocaleString()} · {r.duration_s} s
        </span>
      </div>
      <ul className="divide-y divide-line">
        {r.checks.map((c) => (
          <li key={c.id} className="flex items-start gap-3 px-4 py-2.5 text-sm">
            <Status s={c.status} />
            <div className="min-w-0 flex-1">
              <div className="font-medium">{c.name}</div>
              <div className="text-muted">{c.detail}</div>
            </div>
            <span className="shrink-0 font-mono text-[11px] text-subtle">{c.ms} ms</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

const LABELS: Record<keyof Capacity, string> = {
  max_active_streams: "Streams at once",
  max_transcode_streams: "Transcoded streams at once",
};

function CapacityCard({ canWrite }: { canWrite: boolean }) {
  const toast = useToast();
  const [cap, setCap] = useState<Capacity | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const load = () => api.capacity().then((c) => {
    setCap(c);
    setDraft({ max_active_streams: String(c.max_active_streams.value), max_transcode_streams: String(c.max_transcode_streams.value) });
  }).catch((e) => setErr((e as ApiError).message));
  useEffect(() => { load(); }, []);

  const save = async (reset?: keyof Capacity) => {
    setBusy(true);
    setErr(null);
    try {
      const body = reset ? { reset: [reset] } : {
        max_active_streams: Number(draft.max_active_streams), max_transcode_streams: Number(draft.max_transcode_streams),
      };
      const c = await api.setCapacity(body);
      setCap(c);
      setDraft({ max_active_streams: String(c.max_active_streams.value), max_transcode_streams: String(c.max_transcode_streams.value) });
      toast("ok", "Capacity limits saved.");
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  const changed = cap && (Number(draft.max_active_streams) !== cap.max_active_streams.value
    || Number(draft.max_transcode_streams) !== cap.max_transcode_streams.value);
  return (
    <div className="card space-y-3 p-4">
      <div>
        <h2 className="font-semibold">Capacity limits</h2>
        <p className="text-xs text-muted">Set these from a load test on this server (<span className="font-mono">scripts/load_test.sh</span>): about 80% of the level where streams fall behind.</p>
      </div>
      {err && <ErrorBox>{err}</ErrorBox>}
      {!cap ? <Spinner /> : (Object.keys(LABELS) as (keyof Capacity)[]).map((k) => {
        const info: LimitInfo = cap[k];
        return (
          <label key={k} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="w-48">{LABELS[k]}</span>
            <input type="number" className="input h-8 w-24" min={info.min} max={info.max} disabled={!canWrite}
              value={draft[k] ?? ""} onChange={(e) => setDraft({ ...draft, [k]: e.target.value })} aria-label={LABELS[k]} />
            <span className="text-xs text-subtle">
              {info.overridden ? <>set by {info.updated_by ?? "?"} · default {info.default} {canWrite &&
                <button type="button" className="text-brand hover:underline" onClick={() => save(k)}>reset</button>}</> : "default (from settings)"}
            </span>
          </label>
        );
      })}
      {canWrite && <button className="btn-secondary" disabled={!changed || busy} onClick={() => save()}>{busy ? <Spinner /> : null} Save limits</button>}
    </div>
  );
}

function BackupCard({ canWrite }: { canWrite: boolean }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const now = async () => {
    setBusy(true);
    try {
      toast("ok", (await api.backupNow()).message);
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card space-y-3 p-4">
      <div className="flex items-center gap-2">
        <ServerIcon />
        <h2 className="font-semibold">Database backup</h2>
      </div>
      <p className="text-sm text-muted">
        The stream list, analytics and history are copied once a day (14 kept) next to the data folder. Videos are not
        copied: keep the video folder on a disk you back up, or copy the Docker volume (see DEPLOYMENT.md).
      </p>
      {canWrite && <button className="btn-secondary" onClick={now} disabled={busy}>{busy ? <Spinner /> : <RefreshIcon />} Back up now</button>}
    </div>
  );
}
