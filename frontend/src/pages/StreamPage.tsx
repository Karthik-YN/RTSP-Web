import { useEffect, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { usePolling } from "../hooks/usePolling";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import { formatBitrate, formatBytes, formatTime, since } from "../lib/format";
import type { StreamDetail } from "../types/api";
import { Avatar, CopyButton, ErrorBox, Pill, Skeleton, Spinner, StatusBadge, useToast, WarnBox } from "../components/common";
import { LivePlayer } from "../components/LivePlayer";
import { canControl, useStreamActions } from "../components/StreamsTable";
import { ChevronLeftIcon, ImageIcon, LockIcon, PlayIcon, RestartIcon, StopIcon, TrashIcon } from "../components/icons";

type Tab = "overview" | "logs" | "viewers" | "history";

export function StreamPage() {
  const { id } = useParams();
  const sid = Number(id);
  const rig = useRig();
  const navigate = useNavigate();
  const { data: s, error, refresh } = usePolling(() => api.stream(sid), 2000, [sid]);
  const { request, busy, dialog } = useStreamActions(() => { refresh(); rig.refreshStreams(); });
  const [tab, setTab] = useState<Tab>("overview");
  const [snap, setSnap] = useState<string | null>(null);
  const [snapLoading, setSnapLoading] = useState(false);

  useEffect(() => {
    if (error?.status === 404) {
      const t = setTimeout(() => navigate("/streams"), 2500);
      return () => clearTimeout(t);
    }
  }, [error, navigate]);

  if (error?.status === 404) return <ErrorBox>This stream no longer exists (deleted by another user?). Returning to the list…</ErrorBox>;
  if (!s) return <div className="space-y-3"><Skeleton className="h-10 w-64" /><Skeleton className="h-32" /><Skeleton className="h-64" /></div>;

  const can = canControl(s, rig.user);
  const live = ["RUNNING", "STARTING", "RESTARTING"].includes(s.status);
  const tabs: [Tab, string][] = [["overview", "Overview"], ["logs", "Logs"], ["viewers", `Viewers (${s.runtime.viewer_count})`], ["history", "History"]];

  return (
    <div className="space-y-4">
      <Link to="/streams" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg"><ChevronLeftIcon /> All streams</Link>

      <div className="card p-5">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="font-mono text-2xl font-bold">{s.name}</h1>
              <StatusBadge status={s.status} title={s.status_detail} />
              {s.output_mode === "transcode" && <Pill tone="warn">H.264 transcode</Pill>}
              {s.protected && <Pill tone="info"><LockIcon className="h-3 w-3" /> protected</Pill>}
            </div>
            <p className="mt-1 text-sm text-muted">
              {s.source_rel_path} · {formatTime(s.effective_start_s, false)} → {s.end_offset_s !== null ? formatTime(s.effective_end_s, false) : "end"} ·{" "}
              {s.playback_mode === "loop" ? `loop (${s.runtime.loop_count} done)` : "play once"}
              {s.status === "RUNNING" && <> · live for {since(s.running_since)}</>}
            </p>
          </div>
          {can && (
            <div className="flex flex-wrap gap-2">
              {live
                ? <button className="btn-secondary" disabled={busy[s.id]} onClick={() => request(s, "stop")}><StopIcon /> Stop</button>
                : <button className="btn-primary" disabled={busy[s.id]} onClick={() => request(s, "start")}><PlayIcon /> Start</button>}
              <button className="btn-secondary" disabled={busy[s.id]} onClick={() => request(s, "restart")}><RestartIcon /> Restart</button>
              <button className="btn-secondary text-red-600 dark:text-red-400" disabled={busy[s.id]} onClick={() => request(s, "delete")}><TrashIcon /> Delete</button>
            </div>
          )}
        </div>
        <div className="mt-4 flex items-center gap-2 rounded-lg bg-slate-950 px-3 py-2 font-mono text-sm text-emerald-300 ring-1 ring-inset ring-white/10">
          <span className="flex-1 break-all">{s.rtsp_url}</span><CopyButton text={s.rtsp_url} label="Copy URL" />
        </div>
        <div className="mt-3 space-y-2">
          {s.status_detail && s.status !== "RUNNING" && <WarnBox>{s.status_detail}</WarnBox>}
          {s.last_error && <ErrorBox><b>Last error:</b> {s.last_error}</ErrorBox>}
          {s.warnings.map((w, i) => <WarnBox key={i}>{w}</WarnBox>)}
          {s.protected && !can && rig.user.can_write && (
            <WarnBox>Protected by <b>{s.owner}</b>: only they or an admin can change this stream.{s.note && <> Note: “{s.note}”</>}</WarnBox>
          )}
        </div>
      </div>

      <div className="flex gap-1 border-b border-line" role="tablist">
        {tabs.map(([t, label]) => (
          <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-medium ${tab === t ? "border-brand text-brand" : "border-transparent text-muted hover:text-fg"}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === "overview" && (
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="space-y-4 lg:col-span-2">
            {s.status === "RUNNING" && (
              <Panel title="Live preview" right={<span className="text-xs text-subtle">a few seconds behind the RTSP stream</span>}>
                <LivePlayer streamId={s.id} />
              </Panel>
            )}
            <UptimeBar streamId={s.id} />
            <Panel title="Live status">
              <KV k="Desired / actual" v={<>{s.desired_state} / {s.status}</>} />
              <KV k="RTSP path ready" v={s.runtime.path_ready ? <Pill tone="ok">yes (confirmed by MediaMTX)</Pill> : <Pill tone="muted">no</Pill>} />
              <KV k="Running since" v={s.running_since ? `${new Date(s.running_since).toLocaleString()} (${since(s.running_since)})` : "–"} />
              <KV k="Playback position" v={s.runtime.playback_position_s !== null ? formatTime(s.runtime.playback_position_s) : "–"} />
              <KV k="Loops completed" v={s.playback_mode === "loop" ? <>{s.runtime.loop_count} <span className="text-xs text-subtle">(from published timestamps; period {formatTime(s.runtime.loop_period_s)})</span></> : "–"} />
              <KV k="Publisher" v={s.runtime.progress
                ? <>{s.runtime.progress.fps.toFixed(1)} fps · speed {s.runtime.progress.speed ?? "–"}x · dropped {s.runtime.progress.drop_frames}</> : "–"} />
              <KV k="Bitrate" v={s.runtime.ingest_bps != null
                ? <>{formatBitrate(s.runtime.ingest_bps)} in · {formatBitrate(s.runtime.egress_bps)} out to {s.runtime.viewer_count} reader(s)</> : "–"} />
              <KV k="Process" v={s.pid ? <>PID {s.pid}{s.runtime.process && <> · {s.runtime.process.cpu_percent.toFixed(0)}% CPU · {formatBytes(s.runtime.process.rss_bytes)}</>}</> : "–"} />
              <KV k="Automatic restarts" v={s.restart_count} />
            </Panel>
            <Panel title="Configuration">
              <KV k="Requested start / end" v={`${formatTime(s.start_offset_s)} / ${s.end_offset_s !== null ? formatTime(s.end_offset_s) : "end of file"}`} />
              <KV k="Effective (keyframes)" v={`${formatTime(s.effective_start_s)} → ${formatTime(s.effective_end_s)}`} />
              <KV k="Mode" v={`${s.playback_mode === "loop" ? "Loop" : "Play once"} · ${s.playback_speed}x · ${s.output_mode === "copy" ? "stream copy" : "H.264 transcode"}`} />
              <KV k="Last change" v={`${s.updated_by ?? "?"} · ${new Date(s.updated_at).toLocaleString()} (v${s.version})`} />
            </Panel>
            <Panel title="FFmpeg command">
              {s.ffmpeg_command_text ? (
                <div className="relative">
                  <pre className="max-h-44 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-slate-950 p-3 font-mono text-xs text-slate-100">{s.ffmpeg_command_text}</pre>
                  <div className="absolute right-2 top-2"><CopyButton text={s.ffmpeg_command_text} /></div>
                </div>
              ) : <p className="text-muted">Not started yet.</p>}
            </Panel>
          </div>
          <div className="space-y-4">
            <Panel title="RTSP check">
              <div className="flex aspect-video items-center justify-center overflow-hidden rounded-lg bg-slate-900">
                {snap ? <img src={snap} alt="RTSP snapshot" className="h-full w-full object-contain" onLoad={() => setSnapLoading(false)}
                  onError={() => { setSnap(null); setSnapLoading(false); }} />
                  : <ImageIcon className="h-8 w-8 text-slate-600" />}
              </div>
              <button className="btn-secondary mt-2 w-full" disabled={s.status !== "RUNNING" || snapLoading}
                onClick={() => { setSnapLoading(true); setSnap(`/api/streams/${s.id}/snapshot?t=${Date.now()}`); }}
                title="Reads one frame back over RTSP (end-to-end check)">
                {snapLoading ? <Spinner /> : <ImageIcon />} Take snapshot
              </button>
            </Panel>
            <MetaPanel s={s} onSaved={refresh} />
            {s.video && (
              <Panel title="Source video">
                <KV k="File" v={`${s.video.rel_path} (${formatBytes(s.video.size_bytes)})`} />
                <KV k="Duration" v={formatTime(s.video.duration_s)} />
                <KV k="Video" v={`${s.video.video_codec} ${s.video.video_profile ?? ""} · ${s.video.width}×${s.video.height}`} />
                <KV k="Frame rate" v={`${s.video.fps?.toFixed(3)} fps${s.video.is_vfr ? " (VFR)" : ""}`} />
                <KV k="Pixel format" v={`${s.video.pix_fmt} · ${s.video.bit_depth ?? "?"}-bit`} />
                <KV k="Audio" v={s.video.audio_codec ? `${s.video.audio_codec} (dropped)` : "none"} />
                <KV k="Bitrate / GOP" v={`${formatBitrate(s.video.bitrate_bps)} · keyframe every ${s.video.keyframe_interval_s?.toFixed(2) ?? "?"} s`} />
              </Panel>
            )}
          </div>
        </div>
      )}

      {tab === "logs" && <LogsPanel s={s} />}

      {tab === "viewers" && (
        <Panel title="Connected readers">
          {s.runtime.readers.length === 0 ? <p className="text-muted">No readers connected. Point the edge at the URL above.</p> : (
            <table className="w-full text-sm">
              <thead><tr className="text-left text-xs text-muted"><th className="py-1">Address</th><th>Transport</th><th>Since</th><th className="text-right">Sent</th></tr></thead>
              <tbody className="divide-y divide-line">
                {s.runtime.readers.map((r) => (
                  <tr key={r.id ?? r.remote_addr}>
                    <td className="py-1.5 font-mono text-xs">{r.remote_addr ?? r.type}</td><td className="text-xs">{r.transport ?? "–"}</td>
                    <td className="text-xs">{r.since ? `${new Date(r.since).toLocaleTimeString()} (${since(r.since)})` : "–"}</td>
                    <td className="text-right text-xs">{formatBytes(r.bytes_sent)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Panel>
      )}

      {tab === "history" && (
        <Panel title="Recent events">
          <ul className="divide-y divide-line text-sm">
            {s.events.map((e, i) => (
              <li key={i} className="flex gap-3 py-1.5">
                <span className="w-20 shrink-0 font-mono text-xs text-subtle">{new Date(e.ts).toLocaleTimeString()}</span>
                <span className={`w-28 shrink-0 font-mono text-xs ${e.level === "ERROR" ? "text-red-600" : e.level === "WARNING" ? "text-amber-600" : "text-muted"}`}>{e.kind.replace("stream.", "")}</span>
                <span className="flex-1">{e.user && <b>{e.user} </b>}{e.message}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      {dialog}
    </div>
  );
}

function LogsPanel({ s }: { s: StreamDetail }) {
  const [lines, setLines] = useState(20);
  const { data } = usePolling(() => api.logs(s.id, lines), 2000, [lines]);
  const tail: string[] = data?.lines ?? s.log_tail;
  return (
    <Panel title={`Last ${lines} log lines`} right={
      <select className="input w-auto py-1 text-xs" value={lines} onChange={(e) => setLines(Number(e.target.value))} aria-label="Lines">
        {[20, 50, 100, 200].map((n) => <option key={n} value={n}>{n} lines</option>)}
      </select>}>
      <pre className="max-h-[32rem] overflow-auto rounded-lg bg-slate-950 p-3 font-mono text-[11px] leading-relaxed text-slate-200">
        {tail.length ? tail.map((ln, i) => <div key={i} className={ln.includes("[error]") || ln.includes("[fatal]") ? "text-red-400" : ln.includes("[rtspm]") ? "text-sky-300" : ln.includes("[warning]") ? "text-amber-300" : ""}>{ln}</div>) : "(no log output yet)"}
      </pre>
      {s.runtime.suppressed_timestamp_warnings > 3 && (
        <p className="mt-2 text-xs text-subtle">{s.runtime.suppressed_timestamp_warnings} expected timestamp fix-up warnings at loop points were collapsed.</p>
      )}
    </Panel>
  );
}

function MetaPanel({ s, onSaved }: { s: StreamDetail; onSaved: () => void }) {
  const { user } = useRig();
  const toast = useToast();
  const [note, setNote] = useState(s.note ?? "");
  const [saving, setSaving] = useState(false);
  useEffect(() => setNote(s.note ?? ""), [s.note]);
  const isOwner = user.is_admin || user.username === s.owner;
  const can = canControl(s, user);
  const save = async (body: { note?: string; protected?: boolean }) => {
    setSaving(true);
    try {
      await api.update(s.id, s.version, body);
      toast("ok", "Saved.");
      onSaved();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <Panel title="Ownership & note">
      <div className="flex items-center gap-2 text-sm">
        <Avatar name={s.owner} size="md" />
        <div><div className="font-medium">{s.owner ?? "unknown"}</div><div className="text-xs text-muted">created {new Date(s.created_at).toLocaleDateString()}</div></div>
      </div>
      <label className="label mt-3" htmlFor="note">Note for colleagues</label>
      <textarea id="note" className="input min-h-[4rem]" placeholder="e.g. Client demo until 17:00, please don't stop"
        value={note} onChange={(e) => setNote(e.target.value)} disabled={!can} maxLength={500} />
      {can && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button className="btn-secondary btn-xs" disabled={saving || note === (s.note ?? "")} onClick={() => save({ note })}>Save note</button>
          {isOwner && (
            <label className="ml-auto flex cursor-pointer items-center gap-2 text-xs">
              <input type="checkbox" checked={s.protected} disabled={saving} onChange={(e) => save({ protected: e.target.checked })} />
              <LockIcon className="h-3.5 w-3.5" /> Protect (only me or an admin)
            </label>
          )}
        </div>
      )}
    </Panel>
  );
}

function Panel({ title, children, right }: { title: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h2>{right}
      </div>
      <div className="space-y-1.5 text-sm">{children}</div>
    </section>
  );
}

function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="grid grid-cols-[10rem_1fr] gap-2">
      <span className="text-muted">{k}</span><span className="min-w-0 break-words">{v}</span>
    </div>
  );
}

const SEG_COLOR: Record<string, string> = {
  RUNNING: "bg-emerald-500", STARTING: "bg-blue-400", RESTARTING: "bg-amber-500", STOPPING: "bg-slate-400",
  STOPPED: "bg-slate-300 dark:bg-slate-600", ERROR: "bg-red-500", COMPLETED: "bg-violet-500",
};

/** Last 24 h of status changes as a colored bar (hover a segment for details). */
function UptimeBar({ streamId }: { streamId: number }) {
  const { data } = usePolling(() => api.history(streamId, 24), 30000, [streamId]);
  if (!data) return <Skeleton className="h-16" />;
  const total = data.segments.reduce((a, x) => a + x.seconds, 0) || 1;
  const since = new Date(data.since);
  return (
    <section className="card p-4">
      <div className="mb-2 flex items-baseline justify-between">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">Availability (24 h)</h2>
        <span className="text-sm"><b className="tabular-nums">{data.running_percent.toFixed(1)}%</b> <span className="text-muted">live · {data.changes} status changes</span></span>
      </div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-surface-2">
        {data.segments.map((x, i) => (
          <div key={i} className={`${SEG_COLOR[x.status] ?? "bg-slate-400"} h-full`} style={{ width: `${(x.seconds / total) * 100}%` }}
            title={`${x.status}: ${new Date(x.start).toLocaleTimeString()} – ${new Date(x.end).toLocaleTimeString()} (${Math.round(x.seconds)} s)`} />
        ))}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-subtle">
        <span>{since.toLocaleString()}</span><span>now</span>
      </div>
    </section>
  );
}
