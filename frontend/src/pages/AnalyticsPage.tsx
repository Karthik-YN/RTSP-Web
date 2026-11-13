import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useParams } from "react-router-dom";
import { usePolling } from "../hooks/usePolling";
import { useRig } from "../hooks/useRigData";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { formatBytes, formatDuration, formatTime, parseTime } from "../lib/format";
import { api, ApiError } from "../services/api";
import type { AnalyticsDetail, AnalyticsSlot, RunResult, Stream, Video } from "../types/api";
import {
  ConfirmDialog, CopyButton, copyText, ErrorBox, Modal, Pill, Skeleton, Spinner, StatusBadge, useToast, WarnBox,
} from "../components/common";
import { isVideoFile, useUploads } from "../components/UploadQueue";
import { queueText } from "../components/StreamsTable";
import { GoButton, leadInBody, LeadInFields, leadText, type LeadInValue } from "../components/LeadIn";
import { FastStartNotice } from "../components/CreateStreamDialog";
import {
  CameraIcon, DownloadIcon, EditIcon, EyeIcon, FilmIcon, MonitorIcon, PlayIcon, PlusIcon, SearchIcon, StopIcon,
  TrashIcon, UploadIcon,
} from "../components/icons";

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50) || "camera";

/** One analytics (e.g. People › PPE): its video placeholders and the RTSP streams running them. */
export function AnalyticsPage() {
  const addr = useRtspAddress();
  const id = Number(useParams().id);
  const { user, refreshAnalytics, refreshStreams } = useRig();
  const toast = useToast();
  const { data, error, loading, refresh } = usePolling(() => api.analytics(id), 2000, [id]);
  const [adding, setAdding] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const changed = () => { refresh(); refreshAnalytics(); refreshStreams(); };

  if (loading && !data) return <PageSkeleton />;
  if (error && !data) return <ErrorBox>{error.message}</ErrorBox>;
  if (!data) return null;

  const bulk = async (action: "start" | "stop") => {
    setBusy(action);
    try {
      const r = await api.analyticsAction(id, action);
      toast(r.failed ? "bad" : "ok", `${action === "start" ? "Started" : "Stopped"} ${r.ok} stream(s)` +
        (r.failed ? `, ${r.failed} failed` : ""));
      changed();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setBusy(null);
    }
  };
  const copyAll = async () => {
    try {
      const txt = await api.analyticsUrls(id, "txt", addr.param);
      if (!txt.trim()) { toast("info", "No streams yet."); return; }
      if (await copyText(txt)) toast("ok", `Copied ${txt.trim().split("\n").length} RTSP URL(s).`);
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };

  return (
    <div className="space-y-5">
      <Header data={data} canWrite={user.can_write} onSaved={changed} />
      <div className="flex flex-wrap items-center gap-2">
        <Stat label="Videos" value={data.slots.length} />
        <Stat label="Streams" value={data.streams} />
        <Stat label="Live" value={data.running} tone={data.running ? "ok" : undefined} />
        <Stat label="Viewers" value={data.viewers} />
        {data.problems > 0 && <Stat label="Need attention" value={data.problems} tone="bad" />}
        <div className="ml-auto flex flex-wrap gap-2">
          {user.can_write && data.streams > 0 && (
            <>
              <button className="btn-secondary" disabled={!!busy} onClick={() => bulk("start")}>
                {busy === "start" ? <Spinner /> : <PlayIcon />} Start all
              </button>
              <button className="btn-secondary" disabled={!!busy} onClick={() => setConfirmStop(true)}>
                {busy === "stop" ? <Spinner /> : <StopIcon />} Stop all
              </button>
            </>
          )}
          {data.streams > 0 && (
            <>
              <button className="btn-secondary" onClick={copyAll}>Copy all URLs</button>
              <a className="btn-secondary" href={`/api/analytics/${id}/urls?format=csv${addr.param ? `&host=${encodeURIComponent(addr.param)}` : ""}`} download={`${slugify(data.name)}-rtsp.csv`}>
                <DownloadIcon /> CSV
              </a>
            </>
          )}
        </div>
      </div>

      {data.slots.length === 0 ? (
        <div className="card flex flex-col items-center gap-3 px-6 py-12 text-center">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-soft text-brand"><FilmIcon className="h-6 w-6" /></span>
          <div>
            <div className="font-semibold">No videos for {data.name} yet</div>
            <p className="mt-1 max-w-md text-sm text-muted">
              Add a video placeholder for each test video this analytics needs: give it a title and a description
              (scene, time of day, what should be detected), then upload or pick the video and run RTSP streams from it.
            </p>
          </div>
          {user.can_write && <button className="btn-primary" onClick={() => setAdding(true)}><PlusIcon /> Add video placeholder</button>}
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">
          {data.slots.map((sl) => (
            <SlotCard key={sl.id} slot={sl} analyticsName={data.name} canWrite={user.can_write} onChanged={changed} />
          ))}
          {user.can_write && (
            <button className="card flex min-h-[12rem] flex-col items-center justify-center gap-2 border-dashed text-muted transition-colors hover:border-brand hover:text-brand"
              onClick={() => setAdding(true)}>
              <PlusIcon className="h-6 w-6" /> <span className="text-sm font-medium">Add video placeholder</span>
            </button>
          )}
        </div>
      )}

      {adding && (
        <SlotDialog title={`New video placeholder in ${data.name}`} onClose={() => setAdding(false)}
          onSave={async (title, description) => {
            await api.createSlot(id, { title, description });
            setAdding(false);
            changed();
          }} />
      )}
      {confirmStop && (
        <ConfirmDialog title={`Stop all streams of ${data.name}?`} confirmLabel="Stop all" danger
          message={<>All {data.streams} stream(s) stop. Edges reading them lose their feed until you start them again
            (the URLs stay the same).</>}
          onCancel={() => setConfirmStop(false)} onConfirm={() => { setConfirmStop(false); bulk("stop"); }} />
      )}
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-5 w-96" />
      <div className="grid gap-4 md:grid-cols-2 2xl:grid-cols-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-64" />)}</div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "ok" | "bad" }) {
  const color = tone === "ok" ? "text-emerald-600 dark:text-emerald-400" : tone === "bad" ? "text-red-600 dark:text-red-400" : "";
  return (
    <div className="rounded-lg border border-line bg-surface px-3 py-1.5 text-sm">
      <span className="text-muted">{label}</span> <b className={`tabular-nums ${color}`}>{value}</b>
    </div>
  );
}

function Header({ data, canWrite, onSaved }: { data: AnalyticsDetail; canWrite: boolean; onSaved: () => void }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [desc, setDesc] = useState(data.description ?? "");
  useEffect(() => { if (!editing) setDesc(data.description ?? ""); }, [data.description, editing]);
  const save = async () => {
    try {
      await api.updateAnalytics(data.id, { description: desc });
      setEditing(false);
      onSaved();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };
  return (
    <div>
      <div className="text-sm text-muted">{data.category.name} ›</div>
      <h1 className="text-2xl font-bold tracking-tight">{data.name}</h1>
      {editing ? (
        <div className="mt-2 max-w-2xl space-y-2">
          <textarea className="input min-h-[4rem]" value={desc} onChange={(e) => setDesc(e.target.value)} autoFocus
            placeholder="What this analytics detects, which edge runs it, notes for the team" aria-label="Description" />
          <div className="flex gap-2">
            <button className="btn-primary btn-xs" onClick={save}>Save</button>
            <button className="btn-secondary btn-xs" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <p className="mt-1 max-w-3xl text-sm text-muted">
          {data.description || (canWrite ? <span className="italic text-subtle">No description yet.</span> : null)}
          {canWrite && (
            <button className="btn-ghost btn-xs ml-1 align-middle" onClick={() => setEditing(true)} aria-label="Edit description">
              <EditIcon className="h-3.5 w-3.5" />
            </button>
          )}
        </p>
      )}
    </div>
  );
}

// ------------------------------------------------------------------ placeholder card
function SlotCard({ slot, analyticsName, canWrite, onChanged }: {
  slot: AnalyticsSlot; analyticsName: string; canWrite: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const { enqueue } = useUploads();
  const { videos } = useRig();
  const fileInput = useRef<HTMLInputElement>(null);
  const [dialog, setDialog] = useState<null | "edit" | "pick" | "run" | "remove">(null);
  const [uploading, setUploading] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const v = slot.video;

  const attachUpload = (files: File[]) => {
    const f = files.find(isVideoFile);
    if (!f) { toast("bad", "That is not a video file."); return; }
    // Already on the server (same name and size): use it instead of uploading again.
    const same = videos?.find((x) => x.filename === f.name && x.size_bytes === f.size);
    if (same) {
      api.updateSlot(slot.id, { video_id: same.id })
        .then(() => { toast("ok", `${f.name} is already on the server; attached it to '${slot.title}'.`); onChanged(); })
        .catch((e) => toast("bad", (e as ApiError).message));
      return;
    }
    setUploading(f.name);
    enqueue([f], async (video: Video) => {
      setUploading(null);
      await api.updateSlot(slot.id, { video_id: video.id });
      toast("ok", `${video.filename} attached to '${slot.title}'.`);
      onChanged();
    }, (message) => {
      setUploading(null);
      toast("bad", `${f.name}: ${message}`);
    });
  };
  const onDrop = (e: DragEvent) => {
    if (!canWrite) return;
    (e.nativeEvent as globalThis.DragEvent & { rigHandled?: boolean }).rigHandled = true;
    setOver(false);
    attachUpload(Array.from(e.dataTransfer.files));
  };

  return (
    <div className={`card flex flex-col overflow-hidden transition-shadow ${over ? "ring-2 ring-brand" : ""}`}
      onDragOver={(e) => { if (canWrite) { e.preventDefault(); setOver(true); } }}
      onDragLeave={() => setOver(false)} onDrop={onDrop}>
      <input ref={fileInput} type="file" className="hidden" accept="video/*,.mkv,.ts,.mts,.m2ts"
        onChange={(e) => { attachUpload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      {/* media */}
      {v ? (
        <div className="relative aspect-video bg-slate-900">
          {v.thumbnail_url
            ? <img src={v.thumbnail_url} alt="" className="h-full w-full object-cover" loading="lazy" />
            : <div className="flex h-full items-center justify-center text-slate-500"><FilmIcon className="h-8 w-8" /></div>}
          <div className="absolute bottom-2 left-2 flex gap-1">
            <span className="rounded bg-black/70 px-1.5 py-0.5 text-xs font-medium text-white">{formatDuration(v.duration_s)}</span>
            {v.width && <span className="rounded bg-black/70 px-1.5 py-0.5 text-xs text-white">{v.width}×{v.height}</span>}
          </div>
          {uploading && <UploadingOverlay name={uploading} />}
        </div>
      ) : (
        <div className="relative flex aspect-video flex-col items-center justify-center gap-2 border-b border-dashed border-line bg-surface-2 px-4 text-center">
          <UploadIcon className="h-6 w-6 text-subtle" />
          <div className="text-sm text-muted">{canWrite ? "Drop a video here, or" : "No video yet"}</div>
          {canWrite && (
            <div className="flex flex-wrap justify-center gap-2">
              <button className="btn-primary btn-xs" onClick={() => fileInput.current?.click()}><UploadIcon className="h-3.5 w-3.5" /> Upload from this device</button>
              <button className="btn-secondary btn-xs" onClick={() => setDialog("pick")}>Choose from library</button>
            </div>
          )}
          {uploading && <UploadingOverlay name={uploading} />}
        </div>
      )}

      {/* text */}
      <div className="flex flex-1 flex-col gap-3 p-4">
        <div>
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 flex-1 font-semibold leading-snug">{slot.title}</h3>
            {canWrite && (
              <div className="flex shrink-0 gap-0.5">
                <button className="btn-ghost p-1" title="Edit title and description" aria-label="Edit" onClick={() => setDialog("edit")}><EditIcon className="h-3.5 w-3.5" /></button>
                {v && <button className="btn-ghost p-1" title="Change video" aria-label="Change video" onClick={() => setDialog("pick")}><FilmIcon className="h-3.5 w-3.5" /></button>}
                <button className="btn-ghost p-1" title="Remove placeholder" aria-label="Remove placeholder" onClick={() => setDialog("remove")}><TrashIcon className="h-3.5 w-3.5" /></button>
              </div>
            )}
          </div>
          {slot.description
            ? <p className="mt-1 whitespace-pre-line text-sm text-muted">{slot.description}</p>
            : canWrite && <button className="mt-1 text-sm italic text-subtle hover:text-brand" onClick={() => setDialog("edit")}>Add a description…</button>}
          {v && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span className="truncate font-mono" title={v.rel_path}>{v.filename}</span>
              <span>·</span><span>{v.video_codec?.toUpperCase()}{v.fps ? ` ${Math.round(v.fps * 100) / 100} fps` : ""}</span>
              <span>·</span><span>{formatBytes(v.size_bytes)}</span>
              {v.compat_mode === "transcode" && <Pill tone="warn" title={v.compat_reasons.join("; ")}>needs transcoding</Pill>}
              {v.compat_mode === "reject" && <Pill tone="bad" title={v.compat_reasons.join("; ")}>not supported</Pill>}
              {v.status !== "READY" && <Pill tone="bad" title={v.error ?? ""}>{v.status.toLowerCase()}</Pill>}
            </div>
          )}
        </div>

        {/* streams */}
        {slot.streams.length > 0 && (
          <ul className="divide-y divide-line rounded-lg border border-line">
            {slot.streams.map((s) => <StreamRow key={s.id} s={s} canWrite={canWrite} onChanged={onChanged} />)}
          </ul>
        )}

        {canWrite && v && (
          <div className="mt-auto">
            <button className="btn-primary w-full" disabled={v.status !== "READY" || v.compat_mode === "reject"}
              onClick={() => setDialog("run")}>
              <CameraIcon /> Run RTSP{slot.streams.length ? " (more cameras)" : ""}
            </button>
          </div>
        )}
      </div>

      {dialog === "edit" && (
        <SlotDialog title="Edit video placeholder" initialTitle={slot.title} initialDescription={slot.description ?? ""}
          onClose={() => setDialog(null)}
          onSave={async (title, description) => {
            await api.updateSlot(slot.id, { title, description });
            setDialog(null);
            onChanged();
          }} />
      )}
      {dialog === "pick" && (
        <PickVideoDialog current={v?.id ?? null} onClose={() => setDialog(null)}
          onUpload={() => { setDialog(null); fileInput.current?.click(); }}
          onPick={async (video) => {
            try {
              await api.updateSlot(slot.id, { video_id: video.id });
              setDialog(null);
              onChanged();
            } catch (e) {
              toast("bad", (e as ApiError).message);
            }
          }} />
      )}
      {dialog === "run" && v && (
        <RunDialog slot={slot} video={v} analyticsName={analyticsName} onClose={() => setDialog(null)} onDone={onChanged} />
      )}
      {dialog === "remove" && (
        slot.streams.length > 0 ? (
          <ConfirmDialog title="Delete its streams first" confirmLabel="OK" onConfirm={() => setDialog(null)} onCancel={() => setDialog(null)}
            message={<>'{slot.title}' still has {slot.streams.length} stream(s). Delete them first, so no edge loses its
              camera by accident. The video itself stays in the library either way.</>} />
        ) : (
          <ConfirmDialog title={`Remove '${slot.title}'?`} confirmLabel="Remove" danger onCancel={() => setDialog(null)}
            message="The placeholder and its description are removed. The video stays in the library."
            onConfirm={async () => {
              setDialog(null);
              try { await api.deleteSlot(slot.id); onChanged(); } catch (e) { toast("bad", (e as ApiError).message); }
            }} />
        )
      )}
    </div>
  );
}

function UploadingOverlay({ name }: { name: string }) {
  return (
    <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 bg-surface/85 px-4 text-center text-sm">
      <Spinner />
      <div className="max-w-full truncate">Uploading <b>{name}</b>…</div>
      <div className="text-xs text-muted">Progress is shown in the Uploads panel. It is attached here when it finishes.</div>
    </div>
  );
}

function StreamRow({ s, canWrite, onChanged }: { s: Stream; canWrite: boolean; onChanged: () => void }) {
  const addr = useRtspAddress();
  const toast = useToast();
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const live = s.desired_state === "RUNNING";
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setBusy(true);
    try { await fn(); toast("ok", done); onChanged(); } catch (e) { toast("bad", (e as ApiError).message); } finally { setBusy(false); }
  };
  const rt = s.runtime;
  return (
    <li className="flex flex-wrap items-center gap-2 px-3 py-2 text-sm">
      <StatusBadge status={s.status} title={s.status_detail ?? s.last_error} />
      <Link to={`/streams/${s.id}`} className="font-mono font-medium hover:text-brand">{s.name}</Link>
      <span className="flex items-center gap-1 text-xs text-muted" title={`${rt.viewer_count} edge/RTSP viewer(s) now · peak today ${rt.viewer_peak_today}`}>
        <EyeIcon className="h-3.5 w-3.5" />{rt.viewer_count}
      </span>
      {rt.preview_watchers > 0 && (
        <span className="flex items-center gap-1 text-xs text-subtle" title="Watching the preview in the dashboard (not counted as viewers)">
          <MonitorIcon className="h-3.5 w-3.5" />{rt.preview_watchers}
        </span>
      )}
      {s.status === "STOPPED" && s.last_stop_at && <span className="text-xs text-subtle">stopped {formatSince(s.last_stop_at)}</span>}
      {leadText(s) && <span className="text-xs text-sky-700 dark:text-sky-300" title="The slate is on the URL; the video follows">{leadText(s)}</span>}
      {leadText(s) && canWrite && <GoButton s={s} onDone={onChanged} />}
      {queueText(s) && <span className="text-xs text-blue-700 dark:text-blue-300" title="Camera starts are kept 2 s apart">{queueText(s)}</span>}
      {rt.spacing_warning && <span className="text-xs text-amber-700 dark:text-amber-300" title={rt.spacing_warning}>clip too short for 2 s spacing</span>}
      <div className="ml-auto flex items-center gap-1">
        <CopyButton text={addr.url(s.rtsp_url)} label="Copy URL" />
        {canWrite && (
          live
            ? <button className="btn-ghost btn-xs" disabled={busy} onClick={() => act(() => api.action(s.id, "stop"), `${s.name} stopped`)} title="Stop"><StopIcon className="h-3.5 w-3.5" /></button>
            : <button className="btn-ghost btn-xs" disabled={busy} onClick={() => act(() => api.action(s.id, "start"), `${s.name} starting`)} title="Start (same URL)"><PlayIcon className="h-3.5 w-3.5" /></button>
        )}
        {canWrite && <button className="btn-ghost btn-xs text-red-600" disabled={busy} onClick={() => setConfirm(true)} title="Delete stream"><TrashIcon className="h-3.5 w-3.5" /></button>}
      </div>
      {confirm && (
        <ConfirmDialog title={`Delete ${s.name}?`} confirmLabel="Delete" danger onCancel={() => setConfirm(false)}
          message={<>The URL <span className="font-mono">{addr.url(s.rtsp_url)}</span> stops working for good
            {rt.viewer_count ? <> and <b>{rt.viewer_count} viewer(s)</b> are disconnected now</> : null}. The video stays in the library.</>}
          onConfirm={() => { setConfirm(false); act(() => api.remove(s.id), `${s.name} deleted`); }} />
      )}
    </li>
  );
}

function formatSince(iso: string): string {
  const d = new Date(iso);
  const days = Math.floor((Date.now() - d.getTime()) / 86_400_000);
  if (days >= 1) return `${days} day${days > 1 ? "s" : ""} ago`;
  return `at ${d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;
}

// ------------------------------------------------------------------ dialogs
function SlotDialog({ title, initialTitle = "", initialDescription = "", onClose, onSave }: {
  title: string; initialTitle?: string; initialDescription?: string; onClose: () => void;
  onSave: (title: string, description: string) => Promise<void>;
}) {
  const [t, setT] = useState(initialTitle);
  const [d, setD] = useState(initialDescription);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    setErr(null);
    try { await onSave(t.trim(), d.trim()); } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); }
  };
  return (
    <Modal title={title} onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="label" htmlFor="slot-title">Title</label>
          <input id="slot-title" className="input" value={t} onChange={(e) => setT(e.target.value)} autoFocus
            placeholder="e.g. Warehouse gate, day shift" />
        </div>
        <div>
          <label className="label" htmlFor="slot-desc">Description</label>
          <textarea id="slot-desc" className="input min-h-[6rem]" value={d} onChange={(e) => setD(e.target.value)}
            placeholder="Scene, camera angle, time of day, what the analytics should detect, expected events" />
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={!t.trim() || busy} onClick={save}>{busy && <Spinner />} Save</button>
        </div>
      </div>
    </Modal>
  );
}

function PickVideoDialog({ current, onClose, onPick, onUpload }: {
  current: number | null; onClose: () => void; onPick: (v: Video) => void; onUpload: () => void;
}) {
  const { videos, system } = useRig();
  const [q, setQ] = useState("");
  const free = system?.disks?.videos?.free;
  const list = (videos ?? []).filter((v) => v.filename.toLowerCase().includes(q.toLowerCase()));
  return (
    <Modal title="Choose a video" onClose={onClose} wide>
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[14rem] flex-1">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-subtle" />
          <input className="input pl-8" placeholder="Search the library" value={q} onChange={(e) => setQ(e.target.value)} autoFocus />
        </div>
        <button className="btn-secondary" onClick={onUpload}><UploadIcon /> Upload from this device</button>
      </div>
      {free !== undefined && <p className="mt-2 text-xs text-muted">Free space on the server: {formatBytes(free)}</p>}
      <ul className="mt-3 max-h-[60vh] divide-y divide-line overflow-y-auto rounded-lg border border-line">
        {list.length === 0 && <li className="p-4 text-sm text-muted">No videos match.</li>}
        {list.map((v) => {
          const usable = v.status === "READY" && v.compat_mode !== "reject";
          return (
            <li key={v.id}>
              <button className="flex w-full items-center gap-3 px-3 py-2 text-left hover:bg-surface-2 disabled:opacity-50"
                disabled={!usable} onClick={() => onPick(v)}>
                <div className="h-12 w-20 shrink-0 overflow-hidden rounded bg-slate-900">
                  {v.thumbnail_url && <img src={v.thumbnail_url} alt="" className="h-full w-full object-cover" loading="lazy" />}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="truncate font-medium">{v.filename}</div>
                  <div className="text-xs text-muted">{formatDuration(v.duration_s)} · {v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)}</div>
                </div>
                {v.id === current && <Pill tone="info">current</Pill>}
                {!usable && <Pill tone="bad">{v.status !== "READY" ? v.status.toLowerCase() : "not supported"}</Pill>}
              </button>
            </li>
          );
        })}
      </ul>
    </Modal>
  );
}

function RunDialog({ slot, video, analyticsName, onClose, onDone }: {
  slot: AnalyticsSlot; video: Video; analyticsName: string; onClose: () => void; onDone: () => void;
}) {
  const addr = useRtspAddress();
  const { streams, system, config } = useRig();
  const [countText, setCountText] = useState("1");
  const [lead, setLead] = useState<LeadInValue>({
    mode: config?.default_lead_in_mode ?? "fixed", seconds: config?.default_lead_in_s ?? 5, holdMax: null });
  const count = Math.max(1, Math.min(20, Math.floor(Number(countText)) || 1));
  const countErr = /^\s*([1-9]|1\d|20)\s*$/.test(countText) ? null : "Enter 1 to 20";
  const [prefix, setPrefix] = useState(slugify(analyticsName));
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [mode, setMode] = useState<"loop" | "once">("loop");
  const [startNow, setStartNow] = useState(true);
  const [fastStart, setFastStart] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [result, setResult] = useState<RunResult | null>(null);

  const names = useMemo(() => {
    const taken = new Set((streams ?? []).map((s) => s.name.toLowerCase()));
    const out: string[] = [];
    for (let n = 1; out.length < count; n++) if (!taken.has(`${prefix}-${n}`.toLowerCase())) out.push(`${prefix}-${n}`);
    return out;
  }, [streams, prefix, count]);
  const startErr = parseTime(start).error;
  const endErr = parseTime(end).error;
  const prefixErr = /^[A-Za-z0-9_-]{1,50}$/.test(prefix) ? null : "Letters, digits, '-' and '_' only";
  const room = system?.streams ? system.streams.max_active - system.streams.desired_running : null;

  const run = async () => {
    setBusy(true);
    setErr(null);
    try {
      const r = await api.runSlot(slot.id, { count, name_prefix: prefix, start: start.trim() || null, end: end.trim() || null,
        mode, start_now: startNow, force_transcode: fastStart, ...leadInBody(lead) });
      setResult(r);
      onDone();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (result) {
    const urls = result.created.map((s) => addr.url(s.rtsp_url)).join("\n");
    return (
      <Modal title={`${result.created.length} stream(s) created`} onClose={onClose}>
        <p className="text-sm text-muted">Paste these into the Video Analytics portal as the cameras' RTSP URLs.
          {startNow && " They are starting now; the first picture appears within a few seconds."}</p>
        <ul className="mt-3 space-y-1">
          {result.created.map((s) => (
            <li key={s.id} className="flex items-center gap-2 rounded-lg bg-surface-2 px-2 py-1">
              <span className="min-w-0 flex-1 truncate font-mono text-sm">{addr.url(s.rtsp_url)}</span>
              <CopyButton text={addr.url(s.rtsp_url)} />
            </li>
          ))}
        </ul>
        {result.errors.length > 0 && (
          <div className="mt-3"><WarnBox>
            {result.errors.map((e) => <div key={e.name}><b>{e.name}</b>: {e.error}</div>)}
            {result.skipped.length > 0 && <div>Not created: {result.skipped.join(", ")}</div>}
          </WarnBox></div>
        )}
        <div className="mt-4 flex justify-end gap-2">
          {result.created.length > 1 && <CopyButton text={urls} label="Copy all URLs" />}
          <button className="btn-primary" onClick={onClose}>Done</button>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`Run RTSP from '${slot.title}'`} onClose={onClose}>
      <div className="space-y-4">
        <div className="flex items-center gap-3 rounded-lg bg-surface-2 p-2 text-sm">
          <div className="h-10 w-16 shrink-0 overflow-hidden rounded bg-slate-900">
            {video.thumbnail_url && <img src={video.thumbnail_url} alt="" className="h-full w-full object-cover" />}
          </div>
          <div className="min-w-0">
            <div className="truncate font-medium">{video.filename}</div>
            <div className="text-xs text-muted">{formatTime(video.duration_s, false)} · {video.width}×{video.height}</div>
          </div>
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="run-count">Number of cameras</label>
            <input id="run-count" type="number" min={1} max={20} className={`input ${countErr ? "input-error" : ""}`}
              value={countText} onChange={(e) => setCountText(e.target.value)} />
            {countErr && <p className="mt-1 text-xs text-red-600">{countErr}</p>}
            {room !== null && <p className="mt-1 text-xs text-muted">Room for {Math.max(0, room)} more running stream(s) on this server.</p>}
          </div>
          <div>
            <label className="label" htmlFor="run-prefix">Name prefix</label>
            <input id="run-prefix" className={`input font-mono ${prefixErr ? "input-error" : ""}`} value={prefix}
              onChange={(e) => setPrefix(e.target.value)} />
            {prefixErr && <p className="mt-1 text-xs text-red-600">{prefixErr}</p>}
          </div>
          <div>
            <label className="label" htmlFor="run-start">Start at (optional)</label>
            <input id="run-start" className={`input font-mono ${startErr ? "input-error" : ""}`} value={start}
              onChange={(e) => setStart(e.target.value)} placeholder="00:00:00" />
            {startErr && <p className="mt-1 text-xs text-red-600">{startErr}</p>}
          </div>
          <div>
            <label className="label" htmlFor="run-end">End at (optional)</label>
            <input id="run-end" className={`input font-mono ${endErr ? "input-error" : ""}`} value={end}
              onChange={(e) => setEnd(e.target.value)} placeholder="end of video" />
            {endErr && <p className="mt-1 text-xs text-red-600">{endErr}</p>}
          </div>
        </div>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "loop"} onChange={() => setMode("loop")} /> Loop forever</label>
          <label className="flex items-center gap-2"><input type="radio" checked={mode === "once"} onChange={() => setMode("once")} /> Play once</label>
          <label className="ml-auto flex items-center gap-2"><input type="checkbox" checked={startNow} onChange={(e) => setStartNow(e.target.checked)} /> Start now</label>
        </div>
        <div>
          <span className="label">Lead-in (before the video)</span>
          <LeadInFields value={lead} onChange={setLead} idPrefix="run-lead" />
        </div>
        <FastStartNotice video={video} checked={fastStart} onChange={setFastStart} />
        <div>
          <div className="label">Streams to create</div>
          <div className="flex flex-wrap gap-1">
            {names.map((n) => <span key={n} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs">{n}</span>)}
          </div>
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy || !!countErr || !!startErr || !!endErr || !!prefixErr} onClick={run}>
            {busy ? <Spinner /> : <CameraIcon />} Create {count} stream{count > 1 ? "s" : ""}
          </button>
        </div>
      </div>
    </Modal>
  );
}
