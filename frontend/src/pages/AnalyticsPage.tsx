import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { usePolling } from "../hooks/usePolling";
import { useRig } from "../hooks/useRigData";
import { useRtspAddress } from "../hooks/useRtspAddress";
import { formatBytes, formatDuration, formatTime, parseTime } from "../lib/format";
import { api, ApiError } from "../services/api";
import type { AnalyticsDetail, AnalyticsSlot, ClockTz, RunResult, Stream, Video } from "../types/api";
import {
  ConfirmDialog, CopyButton, ErrorBox, Modal, MoreMenu, Pill, Skeleton, Spinner, StatusBadge, WarnBox, copyText, useToast,
} from "../components/common";
import { isVideoFile, useUploads } from "../components/UploadQueue";
import { codeTone } from "../components/Naming";
import { queueText, StreamsTable } from "../components/StreamsTable";
import { GoButton, leadInBody, LeadInFields, leadText, type LeadInValue } from "../components/LeadIn";
import { videoProblems } from "../lib/libraryFilters";
import { blockedShape, ClipperButton, ClockField, FastStartNotice, OutputSizeField, outputPlan, SHAPE_TEXT } from "../components/CreateStreamDialog";
import { Section, Segmented, videoChips } from "../components/FormParts";
import {
  CameraIcon, ChevronIcon, CopyIcon, DownloadIcon, EditIcon, EyeIcon, FilmIcon, MonitorIcon, PlayIcon, SearchIcon, StopIcon, TrashIcon, UploadIcon,
} from "../components/icons";

const slugify = (s: string) => s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 50) || "camera";

/** One analytics (e.g. People › PPE): its videos and the RTSP streams running them.
 * Upload (or drop) videos straight onto the page: they go into the shared library and are added here. */
export function AnalyticsPage() {
  const addr = useRtspAddress();
  const id = Number(useParams().id);
  const { user, refreshAnalytics, refreshStreams, refreshVideos } = useRig();
  const toast = useToast();
  const { enqueue } = useUploads();
  const { data, error, loading, refresh } = usePolling(() => api.analytics(id), 2000, [id]);
  const [picking, setPicking] = useState(false);
  const [uploading, setUploading] = useState<string[]>([]);
  const [over, setOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const changed = () => { refresh(); refreshAnalytics(); refreshStreams(); refreshVideos(); };
  // Videos (the cards, as always) | Streams (this analytics' streams in the full Streams list: select, bulk, table)
  const [params, setParams] = useSearchParams();
  const tab: "videos" | "streams" = params.get("tab") === "streams" ? "streams" : "videos";
  const navigate = useNavigate();

  /** Add library videos to this analytics (each once; repeats are skipped by the server). */
  const addVideos = async (ids: number[], label: string) => {
    try {
      const r = await api.assignVideos(ids, [id]);
      toast("ok", r.created.length ? `${label} added to ${data?.name ?? "this analytics"}.` : `${label} is already here.`);
      changed();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };
  /** Upload files into the shared library and add each one here when it finishes. */
  const uploadFiles = (files: File[]) => {
    const vids = files.filter(isVideoFile);
    if (!vids.length) { toast("bad", "Those are not video files."); return; }
    if (vids.length < files.length) toast("info", `${files.length - vids.length} non-video file(s) skipped.`);
    for (const f of vids) {
      setUploading((u) => [...u, f.name]);
      const done = () => setUploading((u) => { const i = u.indexOf(f.name); return i < 0 ? u : [...u.slice(0, i), ...u.slice(i + 1)]; });
      // the queue checks the CONTENT first: a video already in the library is added here instead of uploaded again
      enqueue([f], async (video: Video) => { done(); await addVideos([video.id], video.filename); },
        (message) => { done(); if (message !== "Upload cancelled.") toast("bad", `${f.name}: ${message}`); },
        { what: data?.code ?? undefined },
        (same) => { done(); addVideos([same.id], `${same.filename} (already in the library)`); });
    }
  };
  const onDrop = (e: DragEvent) => {
    if (!user.can_write) return;
    e.preventDefault();
    (e.nativeEvent as globalThis.DragEvent & { rigHandled?: boolean }).rigHandled = true;
    setOver(false);
    uploadFiles(Array.from(e.dataTransfer.files));
  };

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

  const inHere = new Set(data.slots.map((s) => s.video?.id).filter((x): x is number => x != null));
  return (
    <div className={`space-y-5 rounded-xl ${over ? "outline-dashed outline-2 outline-offset-4 outline-brand" : ""}`}
      onDragOver={(e) => { if (user.can_write && e.dataTransfer.types.includes("Files")) { e.preventDefault(); setOver(true); } }}
      onDragLeave={(e) => { if (e.currentTarget === e.target) setOver(false); }} onDrop={onDrop}>
      <input ref={fileInput} type="file" multiple className="hidden" accept="video/*,.mkv,.ts,.mts,.m2ts"
        data-testid="analytics-upload-input"
        onChange={(e) => { uploadFiles(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <Header data={data} canWrite={user.can_write} onSaved={changed} />
      {user.can_write && (
        <div className="flex flex-wrap items-center gap-2">
          <button className="btn-primary" onClick={() => fileInput.current?.click()}><UploadIcon /> Upload video</button>
          <button className="btn-secondary" onClick={() => setPicking(true)}><FilmIcon /> Add from shared library</button>
          <span className="text-xs text-muted">or drop video files anywhere on this page. Uploads also appear in the shared library.</span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Stat label="Videos" value={data.slots.length} />
        <Stat label="Streams" value={data.streams} />
        <Stat label="Live" value={data.running} tone={data.running ? "ok" : undefined} />
        <Stat label="Viewers" value={data.viewers} />
        {data.problems > 0 && <Stat label="Need attention" value={data.problems} tone="bad" />}
        <div className="ml-auto flex flex-wrap gap-2">
          <Link className="btn-secondary" to={`/library?analytics=${data.id}`} title="The library, showing only this analytics' videos">
            <FilmIcon /> In library
          </Link>
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

      <div className="flex gap-1 border-b border-line" role="tablist" aria-label={`${data.name} views`}>
        {([["videos", `Videos (${data.slots.length})`], ["streams", `Streams (${data.streams})`]] as const).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => setParams(k === "videos" ? {} : { tab: k }, { replace: true })}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${tab === k ? "border-brand text-brand" : "border-transparent text-muted hover:text-fg"}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === "streams" ? (
        <AnalyticsStreams slotIds={new Set(data.slots.map((sl) => sl.id))} onOpen={(sid) => navigate(`/streams/${sid}`)} onChanged={changed} />
      ) : data.slots.length === 0 && uploading.length === 0 ? (
        <div className="card flex flex-col items-center gap-3 px-6 py-12 text-center" data-testid="analytics-empty">
          <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-brand-soft text-brand"><FilmIcon className="h-6 w-6" /></span>
          <div>
            <div className="font-semibold">No videos for {data.name} yet</div>
            <p className="mt-1 max-w-md text-sm text-muted">
              {user.can_write
                ? "Upload a video (or drop it here), or add one that is already in the shared library. Then run RTSP streams from it."
                : "An operator or admin can upload videos for this analytics."}
            </p>
          </div>
          {user.can_write && (
            <div className="flex flex-wrap justify-center gap-2">
              <button className="btn-primary" onClick={() => fileInput.current?.click()}><UploadIcon /> Upload video</button>
              <button className="btn-secondary" onClick={() => setPicking(true)}><FilmIcon /> Add from shared library</button>
            </div>
          )}
        </div>
      ) : (
        <div className="grid items-start gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
          <h2 className="sr-only">Videos of {data.name}</h2>
          {data.slots.map((sl) => (
            <SlotCard key={sl.id} slot={sl} analyticsName={data.name} code={data.code ?? undefined} canWrite={user.can_write} onChanged={changed} />
          ))}
          {uploading.map((name, i) => (
            <div key={`${name}-${i}`} className="card relative min-h-[12rem] overflow-hidden" data-testid="analytics-uploading">
              <UploadingOverlay name={name} />
            </div>
          ))}
        </div>
      )}

      {picking && (
        <PickVideoDialog current={null} exclude={inHere} onClose={() => setPicking(false)}
          onUpload={() => { setPicking(false); fileInput.current?.click(); }}
          onPick={(v) => { setPicking(false); addVideos([v.id], v.filename); }} />
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

/** The streams of one analytics in the full Streams list (cards or table, select all, start/stop, ⋯). */
function AnalyticsStreams({ slotIds, onOpen, onChanged }: { slotIds: Set<number>; onOpen: (id: number) => void; onChanged: () => void }) {
  const { streams, user, mediamtx } = useRig();
  const mine = (streams ?? []).filter((s) => s.slot_id != null && slotIds.has(s.slot_id));
  if (!mine.length) {
    return <div className="card p-8 text-center text-sm text-muted">No streams yet. Open the Videos tab and press <b>New stream</b> on a video.</div>;
  }
  return <StreamsTable streams={mine} user={user} onSelect={(s) => onOpen(s.id)} onChanged={onChanged}
    mediamtxDown={mediamtx?.state === "DOWN"} />;
}

function PageSkeleton() {
  return (
    <div className="space-y-4">
      <Skeleton className="h-8 w-64" />
      <Skeleton className="h-5 w-96" />
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-64" />)}</div>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone?: "ok" | "bad" }) {
  const color = tone === "ok" ? "text-emerald-700 dark:text-emerald-400" : tone === "bad" ? "text-red-600 dark:text-red-400" : "";
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
      <nav className="text-sm text-muted" aria-label="Breadcrumb">
        <Link to="/" className="hover:text-brand hover:underline">Overview</Link> › {data.category.name} ›
      </nav>
      <h1 className="flex flex-wrap items-center gap-2 text-2xl font-bold tracking-tight">
        {data.name}
        {data.code && (
          <span className={`rounded-md px-2 py-0.5 font-mono text-sm font-bold ring-1 ring-inset ${codeTone(data.code)}`}
            title={`Videos for this analytics are named ${data.code}_WHERE_NN (see Naming guide)`}>{data.code}</span>
        )}
      </h1>
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

// ------------------------------------------------------------------ video card
function SlotCard({ slot, analyticsName, code, canWrite, onChanged }: {
  slot: AnalyticsSlot; analyticsName: string; code?: string; canWrite: boolean; onChanged: () => void;
}) {
  const toast = useToast();
  const { enqueue } = useUploads();
  const { config } = useRig();
  const fileInput = useRef<HTMLInputElement>(null);
  const [dialog, setDialog] = useState<null | "edit" | "pick" | "run" | "remove">(null);
  const [uploading, setUploading] = useState<string | null>(null);
  const [over, setOver] = useState(false);
  const v = slot.video;

  const attachUpload = (files: File[]) => {
    const f = files.find(isVideoFile);
    if (!f) { toast("bad", "That is not a video file."); return; }
    setUploading(f.name);
    enqueue([f], async (video: Video) => {
      setUploading(null);
      await api.updateSlot(slot.id, { video_id: video.id });
      toast("ok", `${video.filename} attached to '${slot.title}'.`);
      onChanged();
    }, (message) => {
      setUploading(null);
      if (message !== "Upload cancelled.") toast("bad", `${f.name}: ${message}`);
    }, { what: code }, (same) => {
      // same content already in the library (any name): use it instead of uploading again
      setUploading(null);
      api.updateSlot(slot.id, { video_id: same.id })
        .then(() => { toast("ok", `Attached '${same.filename}' to '${slot.title}'.`); onChanged(); })
        .catch((e) => toast("bad", (e as ApiError).message));
    });
  };
  // Only an empty (older) card takes a dropped file itself; on a card with a video the drop goes to the
  // page, which adds the file to this analytics as a new card instead of replacing this one.
  const takesDrop = canWrite && !v;
  const onDrop = (e: DragEvent) => {
    if (!takesDrop) return;
    e.preventDefault();
    e.stopPropagation();
    (e.nativeEvent as globalThis.DragEvent & { rigHandled?: boolean }).rigHandled = true;
    setOver(false);
    attachUpload(Array.from(e.dataTransfer.files));
  };

  return (
    <div className={`card flex flex-col overflow-hidden transition-shadow ${over ? "ring-2 ring-brand" : ""}`}
      onDragOver={(e) => { if (takesDrop) { e.preventDefault(); e.stopPropagation(); setOver(true); } }}
      onDragLeave={() => setOver(false)} onDrop={onDrop}>
      <input ref={fileInput} type="file" className="hidden" accept="video/*,.mkv,.ts,.mts,.m2ts"
        onChange={(e) => { attachUpload(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      {/* media */}
      {v ? (
        <div className="relative aspect-video shrink-0 overflow-hidden bg-slate-900" data-testid="slot-thumb">
          {/* fixed 16:9 box: portrait thumbnails are pillarboxed, so cards keep the same height */}
          {v.thumbnail_url
            ? <img src={v.thumbnail_url} alt="" className="absolute inset-0 h-full w-full object-contain" loading="lazy" />
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
      <div className="flex flex-1 flex-col gap-2 p-3">
        <div>
          <div className="flex items-start gap-2">
            <h3 className="min-w-0 flex-1 truncate font-semibold leading-snug" title={slot.title}>{slot.title}</h3>
            {canWrite && (
              <MoreMenu label={`More for ${slot.title}`} items={[
                { label: slot.description ? "Edit title and notes" : "Add notes", icon: <EditIcon className="h-4 w-4" />, onClick: () => setDialog("edit") },
                { label: "Replace video", icon: <FilmIcon className="h-4 w-4" />, onClick: () => setDialog("pick") },
                { label: "Remove from this analytics", icon: <TrashIcon className="h-4 w-4" />, danger: true, onClick: () => setDialog("remove"),
                  title: "The video stays in the shared library" },
              ]} />
            )}
          </div>
          {slot.description && <p className="mt-0.5 line-clamp-2 whitespace-pre-line text-xs text-muted" title={slot.description}>{slot.description}</p>}
          {v && (
            <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
              <span className="min-w-0 max-w-full truncate font-mono" title={v.rel_path}>{v.filename}</span>
              <span>{v.video_codec?.toUpperCase()}{v.fps ? ` ${Math.round(v.fps * 100) / 100} fps` : ""} · {formatBytes(v.size_bytes)}</span>
              {videoProblems(v).length > 0 && <Pill tone="warn" title={videoProblems(v).map((x) => x.text).join("\n")}>
                {videoProblems(v).length === 1 ? videoProblems(v)[0].short : `${videoProblems(v).length} issues`}</Pill>}
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

        {canWrite && v && (blockedShape(v, config.output) ? (
          <div className="mt-auto space-y-1.5" data-testid="slot-not-16-9">
            {v.copy_16x9_id ? (
              <button className="btn-primary w-full" onClick={async () => {
                try {
                  await api.updateSlot(slot.id, { video_id: v.copy_16x9_id! });
                  toast("ok", "Now using the 16:9 copy.");
                  onChanged();
                } catch (e) { toast("bad", (e as ApiError).message); }
              }}><FilmIcon /> Use the 16:9 copy</button>
            ) : (
              <Link className="btn-secondary w-full" to={`/library?analytics=${slot.analytics_id}`}>
                <FilmIcon /> Make a 16:9 copy in the library
              </Link>
            )}
            <p className="text-xs text-muted">Not 16:9 ({SHAPE_TEXT[v.shape ?? "other"]}): streams must be 16:9.</p>
          </div>
        ) : (
          <div className="mt-auto pt-1">
            <button className={slot.streams.length ? "btn-secondary btn-xs" : "btn-primary btn-xs"} disabled={v.status !== "READY" || v.compat_mode === "reject"}
              onClick={() => setDialog("run")} title={slot.streams.length ? "Another RTSP camera from this video" : "Publish this video as an RTSP camera"}>
              <CameraIcon className="h-3.5 w-3.5" /> New stream
            </button>
          </div>
        ))}
      </div>

      {dialog === "edit" && (
        <SlotDialog title="Edit title and notes" initialTitle={slot.title} initialDescription={slot.description ?? ""}
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
          <ConfirmDialog title={`Remove '${slot.title}' from ${analyticsName}?`} confirmLabel="Remove" danger onCancel={() => setDialog(null)}
            message="It is no longer listed under this analytics (its notes are removed). The video stays in the shared library and in any other analytics."
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
      <div className="text-xs text-muted">Progress is shown in the Uploads panel. It is added here and to the shared library when it finishes.</div>
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
    <li className="flex flex-wrap items-center gap-x-2 gap-y-1 px-2.5 py-1.5 text-sm">
      <StatusBadge status={s.status} title={s.status_detail ?? s.last_error} />
      <Link to={`/streams/${s.id}`} className="min-w-0 truncate font-mono font-medium hover:text-brand" title={s.name}>{s.name}</Link>
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
      <div className="ml-auto flex shrink-0 items-center">
        <button className="btn-ghost btn-xs" title={`Copy ${addr.url(s.rtsp_url)}`} aria-label={`Copy URL of ${s.name}`}
          onClick={async () => { if (await copyText(addr.url(s.rtsp_url))) toast("ok", "RTSP URL copied."); }}>
          <CopyIcon className="h-3.5 w-3.5" />
        </button>
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
          <label className="label" htmlFor="slot-desc">Notes</label>
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

function PickVideoDialog({ current, exclude, onClose, onPick, onUpload }: {
  current: number | null; exclude?: Set<number>; onClose: () => void; onPick: (v: Video) => void; onUpload: () => void;
}) {
  const { videos, system } = useRig();
  const [q, setQ] = useState("");
  const free = system?.disks?.videos?.free;
  const list = (videos ?? []).filter((v) => v.filename.toLowerCase().includes(q.toLowerCase()));
  return (
    <Modal title="Choose a video from the shared library" onClose={onClose} wide>
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
          const here = !!exclude?.has(v.id);
          const usable = v.status === "READY" && v.compat_mode !== "reject" && !here;
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
                {here && <Pill tone="info">already here</Pill>}
                {!usable && !here && <Pill tone="bad">{v.status !== "READY" ? v.status.toLowerCase() : "not supported"}</Pill>}
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
  const { streams, system, config, user } = useRig();
  const [outputSize, setOutputSize] = useState<string>("original"); // same size as the file: no scaling unless asked
  const [clock, setClock] = useState<{ on: boolean; tz: ClockTz }>({ on: false, tz: "IST" });
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
        mode, start_now: startNow, force_transcode: fastStart, output_size: outputSize, ...leadInBody(lead),
        ...(config?.features?.includes("clock_overlay") ? { clock: clock.on, clock_tz: clock.tz } : {}) });
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

  const blocked = blockedShape(video, config?.output);
  const canRun = !busy && !countErr && !startErr && !endErr && !prefixErr;
  const why = countErr ? "Check the number of cameras" : prefixErr ? "Check the name prefix"
    : startErr || endErr ? "Check the start / end time" : null;
  const footer = (
    <div className="flex flex-wrap items-center justify-end gap-2">
      <p aria-live="polite" className={`mr-auto min-w-0 truncate text-sm ${why ? "text-red-600 dark:text-red-400" : "text-muted"}`}>
        {why ?? `${count} stream${count > 1 ? "s" : ""}${startNow ? ", starting at once" : ", not started yet"}`}
      </p>
      <button className="btn-secondary" onClick={onClose}>Cancel</button>
      <button className="btn-primary" disabled={!canRun} onClick={run}>
        {busy ? <Spinner /> : <CameraIcon />} Create {count} stream{count > 1 ? "s" : ""}
      </button>
    </div>
  );

  return (
    <Modal title={`New stream from '${slot.title}'`} subtitle={analyticsName} onClose={onClose} wide footer={footer}>
      <div className="grid gap-x-7 gap-y-6 md:grid-cols-[minmax(0,1fr)_16rem]">
        <div className="min-w-0 space-y-7">
          <Section title="Cameras">
            <div className="grid gap-3 sm:grid-cols-2">
              <div>
                <label className="mb-1 block text-sm font-medium text-fg" htmlFor="run-count">Number of cameras</label>
                <input id="run-count" type="number" min={1} max={20} className={`input ${countErr ? "input-error" : ""}`}
                  value={countText} onChange={(e) => setCountText(e.target.value)} />
                <p className={`mt-1 text-xs ${countErr ? "text-red-600" : "text-muted"}`}>
                  {countErr ?? (room !== null ? `Room for ${Math.max(0, room)} more running stream(s) on this server.` : "1 to 20")}
                </p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-fg" htmlFor="run-prefix">Name prefix</label>
                <input id="run-prefix" className={`input font-mono ${prefixErr ? "input-error" : ""}`} value={prefix}
                  onChange={(e) => setPrefix(e.target.value)} autoComplete="off" spellCheck={false} />
                <p className={`mt-1 text-xs ${prefixErr ? "text-red-600" : "text-muted"}`}>{prefixErr ?? "Streams are named prefix-1, prefix-2, …"}</p>
              </div>
            </div>
          </Section>

          <Section title="What plays" aside={`File length ${formatTime(video.duration_s, false)}`}>
            <div className="grid grid-cols-2 gap-3">
              <div>
                <label className="mb-1 block text-sm font-medium text-fg" htmlFor="run-start">Start at (optional)</label>
                <input id="run-start" className={`input font-mono ${startErr ? "input-error" : ""}`} value={start}
                  onChange={(e) => setStart(e.target.value)} placeholder="00:00:00" />
                <p className={`mt-1 text-xs ${startErr ? "text-red-600" : "text-muted"}`}>{startErr ?? "from the beginning if empty"}</p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-fg" htmlFor="run-end">End at (optional)</label>
                <input id="run-end" className={`input font-mono ${endErr ? "input-error" : ""}`} value={end}
                  onChange={(e) => setEnd(e.target.value)} placeholder="end of video" />
                <p className={`mt-1 text-xs ${endErr ? "text-red-600" : "text-muted"}`}>{endErr ?? "to the end if empty"}</p>
              </div>
            </div>
            <ClipperButton videoId={video.id} start={parseTime(start).seconds} end={parseTime(end).seconds}
              builtIn={!!config?.features?.includes("clipper")} />
            <div className="flex flex-wrap items-center gap-3">
              <Segmented name="run-mode" ariaLabel="Playback" value={mode} onChange={setMode}
                options={[{ value: "loop", label: "Loop forever" }, { value: "once", label: "Play once" }]} />
              <label className="ml-auto flex items-center gap-2 text-sm">
                <input type="checkbox" checked={startNow} onChange={(e) => setStartNow(e.target.checked)} /> Start now
              </label>
            </div>
          </Section>

          <Section title="Output">
            {blocked && (
              <WarnBox>This video is {SHAPE_TEXT[video.shape ?? "other"]}, not 16:9, so it can't be streamed. Make a 16:9 copy
                in the shared library (blurred bars, black bars or crop), then add the copy to this analytics.</WarnBox>
            )}
            <OutputSizeField out={config?.output} video={video} value={outputSize} onChange={setOutputSize}
              isAdmin={!!user?.is_admin} idPrefix="run" />
            {outputPlan(video, outputSize, config?.output) !== "live" &&
              <FastStartNotice video={video} checked={fastStart} onChange={setFastStart} />}
            <ClockField config={config} value={clock.on} tz={clock.tz} onChange={(on, tz) => setClock({ on, tz })} idPrefix="run" />
          </Section>

          <details className="group rounded-lg ring-1 ring-inset ring-line [&_summary::-webkit-details-marker]:hidden">
            <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-3 py-2.5 text-sm hover:bg-surface-2">
              <ChevronIcon className="h-4 w-4 text-muted transition-transform group-open:rotate-90" />
              <span className="font-medium text-fg">Advanced</span>
              <span className="truncate text-xs text-muted">Lead-in: {lead.mode === "none" ? "none"
                : lead.mode === "hold" ? `until a viewer, then ${lead.seconds} s` : `${lead.seconds} s slate`}</span>
            </summary>
            <div className="border-t border-line px-3 py-3">
              <div className="mb-1 text-sm font-medium text-fg">Lead-in (before the video)</div>
              <LeadInFields value={lead} onChange={setLead} idPrefix="run-lead" />
            </div>
          </details>
          {err && <ErrorBox>{err}</ErrorBox>}
        </div>

        <aside aria-label="Summary" className="min-w-0 space-y-4 md:sticky md:top-4 md:self-start">
          <div className="overflow-hidden rounded-lg ring-1 ring-inset ring-line">
            <div className="flex aspect-video items-center justify-center bg-slate-900">
              {video.thumbnail_url ? <img src={video.thumbnail_url} alt="" className="h-full w-full object-cover" />
                : <span className="text-xs text-slate-400">no picture</span>}
            </div>
            <div className="space-y-1.5 p-2.5">
              <div className="truncate text-sm font-medium" title={video.filename}>{video.filename}</div>
              <div className="flex flex-wrap gap-1">{videoChips(video, blocked ? "not 16:9" : null)}</div>
            </div>
          </div>
          <div>
            <h3 className="mb-1.5 text-sm font-semibold text-fg">Streams to create</h3>
            <div className="flex flex-wrap gap-1">
              {names.map((n) => <span key={n} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs ring-1 ring-inset ring-line">{n}</span>)}
            </div>
          </div>
        </aside>
      </div>
    </Modal>
  );
}
