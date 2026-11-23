import { useEffect, useState } from "react";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import type { Capacity, CheckStatus, EncoderChoice, Encoders, HealthReport, LimitInfo } from "../types/api";
import { ErrorBoundary, ErrorBox, Skeleton, Spinner, useToast } from "../components/common";
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
    `RTSP Studio (rig02) health test (${r.mode}) on ${r.server}`,
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

/** Settings that used to sit under the health report: encoding, capacity, backup, H.265. */
export function SettingsPanel() {
  const { user } = useRig();
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <ErrorBoundary label="video encoding"><EncoderCard canWrite={user.can_write} /></ErrorBoundary>
      <ErrorBoundary label="capacity limits"><CapacityCard canWrite={user.can_write} /></ErrorBoundary>
      <ErrorBoundary label="database backup"><BackupCard canWrite={user.can_write} /></ErrorBoundary>
      <ErrorBoundary label="H.265 setting"><CodecsCard isAdmin={user.is_admin} /></ErrorBoundary>
    </div>
  );
}

/** The health report and test. `embedded`: shown as a tab of the System page (no page title, no settings). */
export function HealthPage({ embedded = false }: { embedded?: boolean }) {
  const toast = useToast();
  const [report, setReport] = useState<HealthReport | null>(null);
  const [auto, setAuto] = useState<{ quick: HealthReport | null; interval: number } | null>(null);
  const [running, setRunning] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    api.lastHealth().then((r) => {
      setReport(r.full);
      setAuto({ quick: r.quick, interval: r.auto_interval_s });
    }).catch(() => {}).finally(() => setLoaded(true));
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
        {embedded ? <p className="text-sm text-muted">Checks the server, MediaMTX, disk, backups and every running stream.</p> : (
          <div>
            <h1 className="text-2xl font-bold tracking-tight">Health</h1>
            <p className="text-sm text-muted">Checks the server, MediaMTX, disk, backups and every running stream.</p>
          </div>
        )}
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

      {!loaded ? (
        // holds the report's place, so the cards below don't jump down when it arrives
        <div className="card space-y-3 p-4" aria-busy="true">{Array.from({ length: 12 }, (_, i) => <Skeleton key={i} className="h-12" />)}</div>
      ) : report ? <ReportCard r={report} /> : (
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

      {!embedded && <SettingsPanel />}
    </div>
  );
}

function CheckRow({ c }: { c: HealthReport["checks"][number] }) {
  return (
    <li className="flex items-start gap-3 px-4 py-2.5 text-sm">
      <Status s={c.status} />
      <div className="min-w-0 flex-1">
        <div className="font-medium">{c.name}</div>
        <div className="text-muted">{c.detail}</div>
      </div>
      <span className="shrink-0 font-mono text-[11px] text-subtle">{c.ms} ms</span>
    </li>
  );
}

/** A report: problems first and always visible; the passing per-stream reads fold into one line. */
function ReportCard({ r }: { r: HealthReport }) {
  const isStream = (c: HealthReport["checks"][number]) => c.name.startsWith("Stream ");
  const streamOk = r.checks.filter((c) => isStream(c) && (c.status === "PASS" || c.status === "SKIP"));
  const rest = r.checks.filter((c) => !streamOk.includes(c));
  const order = { FAIL: 0, WARN: 1, PASS: 2, SKIP: 3 } as Record<string, number>;
  rest.sort((a, b) => (order[a.status] ?? 9) - (order[b.status] ?? 9));
  const [open, setOpen] = useState(false);
  const ageH = (Date.now() - new Date(r.started_at).getTime()) / 3.6e6;
  return (
    <div className="card overflow-hidden" data-testid="health-report">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-4 py-3">
        <span className={`rounded-lg px-2.5 py-1 text-sm font-bold ring-1 ring-inset ${TONE[r.overall]}`}>{r.overall}</span>
        <span className="text-sm text-muted">
          {r.summary.PASS} pass · {r.summary.WARN} warn · {r.summary.FAIL} fail · {r.summary.SKIP} skipped
        </span>
        <span className={`ml-auto text-xs ${ageH > 1 ? "font-medium text-amber-700 dark:text-amber-300" : "text-subtle"}`}
          title={ageH > 1 ? "This report is old: streams may have changed since. Run the test again." : undefined}>
          {r.mode === "full" ? "Full test" : "Quick check"} · {new Date(r.started_at).toLocaleString()}
          {ageH > 1 && ` (${Math.round(ageH)} h ago)`} · {r.duration_s} s
        </span>
      </div>
      <ul className="divide-y divide-line">
        {rest.map((c) => <CheckRow key={c.id} c={c} />)}
        {streamOk.length > 0 && (
          <li className="text-sm">
            <button className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-surface-2" onClick={() => setOpen((o) => !o)}
              aria-expanded={open}>
              <Status s="PASS" />
              <span className="flex-1 font-medium">Streams read over RTSP: {streamOk.length} ok</span>
              <span className="text-xs text-brand">{open ? "Hide" : "Show each"}</span>
            </button>
            {open && <ul className="divide-y divide-line border-t border-line bg-surface-2/40">{streamOk.map((c) => <CheckRow key={c.id} c={c} />)}</ul>}
          </li>
        )}
      </ul>
    </div>
  );
}

const CHOICE_LABEL: Record<EncoderChoice, string> = {
  auto: "Auto: best that works here",
  cpu: "CPU",
  nvidia: "NVIDIA GPU",
  intel: "Intel iGPU (Quick Sync)",
  vaapi: "GPU via VAAPI",
};

/** Which hardware encodes transcoded streams on THIS machine, and the choice. */
export function EncoderCard({ canWrite }: { canWrite: boolean }) {
  const toast = useToast();
  const [enc, setEnc] = useState<Encoders | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    api.encoders()
      .then((e) => (Array.isArray(e?.encoders) ? setEnc(e) : setErr("This server doesn't report its encoders.")))
      .catch((e) => setErr((e as ApiError).message));
  }, []);

  const run = async (f: () => Promise<Encoders>, msg?: string) => {
    setBusy(true);
    setErr(null);
    try {
      setEnc(await f());
      if (msg) toast("ok", msg);
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const gpus = enc?.encoders.filter((e) => e.id !== "cpu" && e.available) ?? [];
  return (
    <div className="card space-y-3 p-4 lg:col-span-2" data-testid="encoder-card">
      <div>
        <h2 className="font-semibold">Video encoding</h2>
        <p className="text-xs text-muted">
          Used only by streams that are <b>transcoded</b> (resized or converted live); copy streams don't encode.
          Each option was tested on this machine with a short encode.
        </p>
      </div>
      {err && <ErrorBox>{err}</ErrorBox>}
      {!enc ? (
        // same height as the loaded card, so the cards below don't jump
        <div className="space-y-2" aria-busy="true">
          <Skeleton className="h-5 w-1/2" />
          <div className="grid gap-2 sm:grid-cols-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-16" />)}</div>
          <Skeleton className="h-9 w-2/3" />
          <Skeleton className="h-4 w-full" />
        </div>
      ) : <>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span>In use now:</span>
          <b data-testid="encoder-active">{enc.active_label}</b>
          {enc.active === "cpu" && gpus.length === 0 && (
            <span className="text-xs text-muted">No usable GPU on this machine, so the CPU encodes.</span>
          )}
        </div>
        {enc.note && <div className="rounded-md bg-amber-500/10 px-3 py-2 text-sm text-amber-800 dark:text-amber-300">{enc.note}</div>}
        <ul className="grid gap-2 sm:grid-cols-2" aria-label="Encoders on this machine">
          {enc.encoders.map((e) => (
            <li key={e.id} data-testid={`encoder-${e.id}`}
              className={`rounded-lg border px-3 py-2 text-sm ${e.id === enc.active ? "border-brand ring-1 ring-brand" : "border-line"}`}>
              <div className="flex items-center gap-2">
                <span className={`inline-flex rounded-md px-1.5 py-0.5 text-[11px] font-bold ring-1 ring-inset ${e.available ? TONE.PASS : TONE.SKIP}`}>
                  {e.available ? "WORKS" : "NO"}
                </span>
                <span className="font-medium">{e.label}</span>
                {e.id === enc.active && <span className="ml-auto text-xs font-semibold text-brand">in use</span>}
              </div>
              <div className="mt-1 text-xs text-muted">{e.available ? e.about : e.reason}</div>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label htmlFor="encoder-choice">Use</label>
          <select id="encoder-choice" className="input h-8 w-auto" value={enc.choice} disabled={!canWrite || busy}
            onChange={(e) => run(() => api.setEncoder(e.target.value as EncoderChoice),
              "Saved. New and restarted transcoded streams use it; running ones keep theirs until restarted.")}>
            {enc.choices.map((c) => {
              const info = enc.encoders.find((x) => x.id === c);
              return <option key={c} value={c}>{CHOICE_LABEL[c]}{info && !info.available ? " (not available here)" : ""}</option>;
            })}
          </select>
          {canWrite && enc.overridden && (
            <button type="button" className="text-xs text-brand hover:underline" disabled={busy}
              onClick={() => run(() => api.setEncoder(null), "Back to the default.")}>reset</button>
          )}
          {canWrite && (
            <button type="button" className="btn-secondary h-8" disabled={busy}
              onClick={() => run(api.detectEncoders, "Checked again.")}>{busy ? <Spinner /> : <RefreshIcon />} Check again</button>
          )}
        </div>
        <p className="text-xs text-subtle">
          A GPU must be passed into the container first: <span className="font-mono">docker-compose.gpu-intel.yml</span> (Intel/AMD
          graphics) or <span className="font-mono">docker-compose.gpu-nvidia.yml</span>, then "Check again".
        </p>
      </>}
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
      {!cap ? <div className="space-y-3" aria-busy="true"><Skeleton className="h-10" /><Skeleton className="h-10" /></div> : (Object.keys(LABELS) as (keyof Capacity)[]).map((k) => {
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


/** H.265 to the edges as-is (no transcoding) or converted to H.264. Hidden on servers without the setting. */
function CodecsCard({ isAdmin }: { isAdmin: boolean }) {
  const toast = useToast();
  const [state, setState] = useState<{ hevc_passthrough: boolean } | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { api.codecs().then(setState).catch(() => setState(null)); }, []);
  if (!state) return null;
  const toggle = async () => {
    setBusy(true);
    try {
      const r = await api.setCodecs(!state.hevc_passthrough);
      setState(r);
      toast("ok", r.hevc_passthrough
        ? "H.265 videos now go to edges as they are. Applies to streams started or restarted from now on."
        : "H.265 videos are converted to H.264 again. Applies to streams started or restarted from now on.");
    } catch (e) { toast("bad", (e as ApiError).message); } finally { setBusy(false); }
  };
  return (
    <div className="card space-y-3 p-4" data-testid="codecs-card">
      <div className="flex items-center gap-2">
        <ServerIcon />
        <h2 className="font-semibold">H.265 to edges</h2>
        <span className={`ml-auto rounded-md px-1.5 py-0.5 text-[11px] font-bold ring-1 ring-inset ${state.hevc_passthrough ? TONE.PASS : TONE.SKIP}`}>
          {state.hevc_passthrough ? "AS-IS" : "CONVERTED"}
        </span>
      </div>
      <p className="text-sm text-muted">
        {state.hevc_passthrough
          ? "H.265 videos are sent to edges unchanged: no CPU, like an H.265 camera. The dashboard preview gets its own small H.264 copy while someone watches."
          : "H.265 videos are converted to H.264 live (about one CPU core per stream). Turn this on only if your edges decode H.265."}
      </p>
      {isAdmin && (
        <button className="btn-secondary" onClick={toggle} disabled={busy}>
          {busy ? <Spinner /> : null} {state.hevc_passthrough ? "Convert H.265 to H.264 again" : "Send H.265 as-is"}
        </button>
      )}
    </div>
  );
}
