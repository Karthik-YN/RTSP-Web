import { memo, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../services/api";
import { useUploads } from "./UploadQueue";
import { formatBitrate, formatBytes, formatDuration, formatTime, since } from "../lib/format";
import type { Conversion, ConvertFit, ConvertProfile, OutputConfig, PreviewConfig, User, Video } from "../types/api";
import { useRig } from "../hooks/useRigData";
import { SHAPE_TEXT } from "./CreateStreamDialog";
import { ConfirmDialog, ErrorBox, Modal, Pill, Spinner, useToast } from "./common";
import { CameraIcon, DownloadIcon, FilmIcon, InfoIcon, PlayIcon, RefreshIcon, SearchIcon, TrashIcon, UploadIcon } from "./icons";

export function VideoLibrary({ videos, loading, user, onChanged, onCreateStream, search, setSearch }: {
  videos: Video[]; loading: boolean; user: User | null; onChanged: () => void;
  onCreateStream: (videoId: number) => void; search: string; setSearch: (s: string) => void;
}) {
  const toast = useToast();
  const { output: out, preview } = useRig().config;
  const [refreshing, setRefreshing] = useState(false);
  const [fitting, setFitting] = useState<Video | null>(null);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await api.refreshVideos();
      toast("info", `Library rescanned: ${r.added} new, ${r.updated} changed, ${r.missing} missing` +
        (r.settling.length ? `, ${r.settling.length} still being copied` : ""));
      onChanged();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setRefreshing(false);
    }
  };

  const conv = useConversions(onChanged);

  const [deleting, setDeleting] = useState<Video | null>(null);
  const usable = videos.filter((v) => v.status === "READY" && v.compat_mode !== "reject");
  const doDelete = async (v: Video) => {
    try {
      toast("ok", (await api.deleteVideo(v.id)).message);
      onChanged();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    }
  };
  const unusable = videos.filter((v) => !(v.status === "READY" && v.compat_mode !== "reject"));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full max-w-xs">
          <SearchIcon className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-subtle" />
          <input className="input pl-8" placeholder="Search file names…" value={search}
            onChange={(e) => setSearch(e.target.value)} aria-label="Search videos" />
        </div>
        <button className="btn-secondary" onClick={refresh} disabled={refreshing}>
          {refreshing ? <Spinner /> : <RefreshIcon />} Refresh
        </button>
        {user?.can_write && <UploadButton onDone={onChanged} />}
        <span className="text-xs text-muted lg:ml-auto">Files copied into the server's video folder appear after Refresh (or within 30 s).</span>
      </div>

      {loading && videos.length === 0 && <div className="p-6"><Spinner /></div>}
      {!loading && videos.length === 0 && (
        <div className="card flex flex-col items-center gap-2 p-10 text-center text-muted">
          <FilmIcon className="h-10 w-10 text-subtle" />
          {search ? "No videos match the search." : <>The library is empty.<br />Upload a video or copy files into the server folder, then press Refresh.</>}
        </div>
      )}

      <div className="grid grid-cols-[repeat(auto-fill,minmax(15rem,1fr))] gap-3">
        {usable.map((v) => <VideoCard key={v.id} v={v} canWrite={!!user?.can_write} isAdmin={!!user?.is_admin}
          onCreateStream={onCreateStream} onDelete={setDeleting} job={conv.activeFor(v.id)}
          onConvert={conv.start} onCancel={conv.cancel} out={out} onFit={setFitting} preview={preview} />)}
      </div>

      {fitting && <FitDialog v={fitting} out={out} onClose={() => setFitting(null)}
        onStart={async (fit) => { const err = await conv.start(fitting, { profile: "16x9", fit }); if (!err) setFitting(null); return err; }} />}

      {deleting && (
        <ConfirmDialog title={`Delete ${deleting.filename}?`} danger confirmLabel="Move to trash"
          message={<>The file is moved to the library's <span className="font-mono">.trash</span> folder on the server and purged after 7 days
            (an admin can restore it from there). Videos used by a stream cannot be deleted.</>}
          onCancel={() => setDeleting(null)} onConfirm={() => { const v = deleting; setDeleting(null); doDelete(v); }} />
      )}

      {unusable.length > 0 && (
        <details className="card p-3" open={usable.length === 0}>
          <summary className="cursor-pointer text-sm font-medium text-fg">
            {unusable.length} file{unusable.length > 1 ? "s" : ""} cannot be streamed
          </summary>
          <ul className="mt-2 space-y-1 text-sm">
            {unusable.map((v) => (
              <li key={v.id} className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{v.rel_path}</span>
                {user?.is_admin && v.status !== "MISSING" && (
                  <button className="btn-ghost btn-xs text-red-600" onClick={() => setDeleting(v)}>Delete</button>
                )}
                <span className="text-red-700 dark:text-red-300">{v.error ?? v.compat_reasons.join(" ")}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

/** The video file on the server: plays in the browser, or downloads with its own name. */
export function videoFileUrl(id: number, download = false): string {
  return `/api/videos/${id}/file${download ? "?download=true" : ""}`;
}

/**
 * "Convert to H.264" jobs: start, cancel, and follow progress (polled every 2 s
 * while one is queued or running). Finished jobs refresh the library.
 */
export function useConversions(onChanged: () => void) {
  const toast = useToast();
  const [jobs, setJobs] = useState<Conversion[]>([]);
  const seen = useRef<Record<string, Conversion["state"]>>({});
  const load = useCallback(async () => {
    try {
      const list = await api.conversions();
      let changed = false;
      for (const j of list) {
        const before = seen.current[j.id];
        if (before && before !== j.state && (before === "queued" || before === "running")) {
          if (j.state === "done") { toast("ok", `Done: ${j.target} is in the library`); changed = true; }
          if (j.state === "failed") toast("bad", j.error ?? `Converting ${j.source} failed`);
        }
        seen.current[j.id] = j.state;
      }
      setJobs(list);
      if (changed) onChanged();
    } catch { /* keep the last list */ }
  }, [onChanged, toast]);
  const busy = jobs.some((j) => j.state === "queued" || j.state === "running");
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [busy, load]);
  /** Starts a conversion; resolves to the error message, or null when it started. */
  const start = useCallback(async (v: Video, opts?: { profile: ConvertProfile; fit?: ConvertFit }): Promise<string | null> => {
    try {
      const j = await api.convertVideo(v.id, opts);
      seen.current[j.id] = j.state;
      setJobs((cur) => [j, ...cur]);
      toast("info", `${WHAT[opts?.profile ?? "h264"]} of ${v.filename} in the background; the original is kept.`);
      return null;
    } catch (e) {
      const msg = (e as ApiError).message;
      toast("bad", msg);
      return msg;
    }
  }, [toast]);
  const cancel = useCallback(async (j: Conversion) => {
    try { await api.cancelConversion(j.id); await load(); } catch (e) { toast("bad", (e as ApiError).message); }
  }, [load, toast]);
  return {
    jobs,
    activeFor: (videoId: number) => jobs.find((j) => j.video_id === videoId && (j.state === "queued" || j.state === "running")),
    start,
    cancel,
  };
}

const WHAT: Record<ConvertProfile, string> = {
  h264: "Converting to H.264", "720p": "Making a 720p copy", "16x9": "Making a 16:9 copy",
};

function formatEta(s: number | null): string {
  if (s === null) return "";
  return s < 60 ? ` · ~${s} s left` : ` · ~${Math.round(s / 60)} min left`;
}

export function ConversionProgress({ job, onCancel }: { job: Conversion; onCancel?: (j: Conversion) => void }) {
  const pct = Math.round(job.progress * 100);
  return (
    <div className="space-y-1" data-testid="conversion-progress">
      <div className="flex items-center justify-between text-xs">
        <span>{job.state === "queued" ? "Waiting to convert…" : `${WHAT[job.profile ?? "h264"]}: ${pct}%${formatEta(job.eta_s)}`}</span>
        {onCancel && <button className="btn-ghost btn-xs" onClick={() => onCancel(job)}>Cancel</button>}
      </div>
      <div className="h-1.5 overflow-hidden rounded bg-surface-2">
        <div className="h-full bg-brand transition-all" style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

/** Short clip of a long video (made and cached by the server). */
export function previewUrl(id: number, start = 0): string {
  return `/api/videos/${id}/preview${start > 0 ? `?start=${start}` : ""}`;
}

/** Where a preview can start: the beginning, then a quarter, half and three quarters in (whole minutes). */
export function previewStarts(duration: number, clip: number): number[] {
  const last = Math.max(0, duration - clip);
  const out = [0, 0.25, 0.5, 0.75].map((f) => Math.min(last, Math.floor((duration * f) / 60) * 60));
  return [...new Set(out)];
}

/**
 * Plays the file in the browser. Long videos play a short preview clip instead of the
 * whole file; formats browsers can't play get the H.264 copy offer; Download is always there.
 */
export function VideoPlayer({ v, onClose, onConvert, job, preview }: {
  v: Video; onClose: () => void; onConvert?: () => Promise<string | null> | void; job?: Conversion;
  preview?: PreviewConfig;
}) {
  if (preview && (v.duration_s ?? 0) > preview.threshold_s) {
    return <PreviewPlayer v={v} clip={preview.seconds} onClose={onClose} />;
  }
  return <FilePlayer v={v} onClose={onClose} onConvert={onConvert} job={job} />;
}

function FilePlayer({ v, onClose, onConvert, job }: {
  v: Video; onClose: () => void; onConvert?: () => Promise<string | null> | void; job?: Conversion;
}) {
  const [failed, setFailed] = useState(false);
  const [convertErr, setConvertErr] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const convert = async () => {
    if (!onConvert) return;
    setStarting(true);
    setConvertErr((await onConvert()) ?? null);
    setStarting(false);
  };
  const unplayable = v.browser_playable === false || failed;
  return (
    <Modal title={v.filename} wide onClose={onClose}>
      {unplayable ? (
        <div className="space-y-3 text-sm" data-testid="not-playable">
          <p>{v.browser_note ?? `This browser can't play this file (${v.video_codec}).`}</p>
          <p className="text-muted">
            It still streams over RTSP. To watch it here, convert it once to H.264: the copy plays in every browser,
            streams without live transcoding and starts within 2 s. The original is kept.
          </p>
          {job ? <ConversionProgress job={job} />
            : onConvert && (
              <button className="btn-primary" onClick={convert} disabled={starting}>
                {starting ? <Spinner /> : <RefreshIcon />} Convert to H.264
              </button>
            )}
          {convertErr && !job && <ErrorBox>{convertErr}</ErrorBox>}
        </div>
      ) : (
        <video data-testid="video-player" className="max-h-[70vh] w-full rounded bg-black" src={videoFileUrl(v.id)}
          controls autoPlay playsInline preload="metadata" onError={() => setFailed(true)} />
      )}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)}</span>
        <a className="btn-secondary" href={videoFileUrl(v.id, true)} download={v.filename}><DownloadIcon /> Download</a>
      </div>
    </Modal>
  );
}

/** Memoised: the library re-renders on every poll, a card only when its own video or job changes. */
const VideoCard = memo(function VideoCard({ v, canWrite, isAdmin, onCreateStream, onDelete, job, onConvert: convertVideo, onCancel, out, onFit, preview }: {
  v: Video; canWrite: boolean; isAdmin: boolean; onCreateStream: (id: number) => void; onDelete: (v: Video) => void;
  job?: Conversion; onConvert: (v: Video, opts?: { profile: ConvertProfile; fit?: ConvertFit }) => Promise<string | null>;
  onCancel: (j: Conversion) => void; out?: OutputConfig; onFit: (v: Video) => void; preview?: PreviewConfig;
}) {
  const onConvert = () => convertVideo(v);
  const notWide = v.is_16_9 === false;
  const blocked = notWide && !!out?.require_16_9;
  const isStd = !!out && v.display_width === out.width && v.display_height === out.height;
  const offer720 = canWrite && !!out && v.is_16_9 === true && !isStd && !v.copy_720p_id && !/ \(720p\)\.mp4$/.test(v.rel_path);
  const reasons = v.convert_reasons ?? [];
  const tip = [...v.compat_reasons, ...v.compat_warnings].join("\n");
  const [playing, setPlaying] = useState(false);
  const [details, setDetails] = useState(false);
  return (
    <div className="card flex flex-col overflow-hidden" style={{ contentVisibility: "auto", containIntrinsicSize: "auto 22rem" }}>
      {/* Fixed 16:9 box: portrait and 4:3 thumbnails are letterboxed inside it, so every card has the same height. */}
      <div className="group relative aspect-video shrink-0 overflow-hidden bg-slate-900" data-testid="thumb-box">
        {v.thumbnail_url
          ? <img src={v.thumbnail_url} alt="" loading="lazy" decoding="async"
              className="absolute inset-0 h-full w-full object-contain" />
          : <div className="absolute inset-0 flex items-center justify-center"><FilmIcon className="h-8 w-8 text-muted" /></div>}
        <button className="absolute inset-0 flex items-center justify-center bg-black/0 transition hover:bg-black/30"
          onClick={() => setPlaying(true)} aria-label={`Play ${v.filename}`} title="Play in the browser">
          <span className="rounded-full bg-black/60 p-3 text-white opacity-80 group-hover:opacity-100"><PlayIcon className="h-6 w-6" /></span>
        </button>
        <span className="absolute bottom-1.5 right-1.5 rounded bg-black/75 px-1.5 py-0.5 font-mono text-[11px] text-white">
          {formatTime(v.duration_s, false)}
        </span>
        {v.streams_using > 0 && (
          <span className="absolute left-1.5 top-1.5 rounded bg-green-600/90 px-1.5 py-0.5 text-[11px] font-medium text-white">
            {v.streams_using} stream{v.streams_using > 1 ? "s" : ""}
          </span>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-1.5 p-3">
        <div className="flex items-start gap-1">
          <div className="line-clamp-2 min-w-0 flex-1 break-words text-sm font-medium leading-snug" title={v.rel_path}>{v.rel_path}</div>
          <button className="-mr-1 -mt-0.5 rounded p-1 text-muted hover:bg-slate-100 hover:text-current dark:hover:bg-slate-800"
            onClick={() => setDetails(true)} aria-label={`Details of ${v.filename}`} title="Details and uploader">
            <InfoIcon />
          </button>
        </div>
        <UploaderLine v={v} />
        <div className="flex flex-wrap gap-x-2 text-xs text-muted">
          <span>{v.width}×{v.height}</span>
          <span>{v.fps ? `${v.fps.toFixed(3).replace(/\.?0+$/, "")} fps` : "–"}</span>
          <span>{v.video_codec}{v.bit_depth && v.bit_depth !== 8 ? ` ${v.bit_depth}-bit` : ""}</span>
          <span>{formatBytes(v.size_bytes)}</span>
        </div>
        <div className="flex flex-wrap gap-1" title={tip}>
          {v.compat_mode === "copy" && !blocked && <Pill tone="ok">Edge-ready (copy)</Pill>}
          {v.compat_mode === "transcode" && <Pill tone="warn">Needs transcode</Pill>}
          {v.is_vfr && <Pill tone="info">VFR</Pill>}
          {v.audio_codec && <Pill tone="muted">audio dropped</Pill>}
          {v.keyframe_interval_s && v.keyframe_interval_s > 4 && <Pill tone="warn">long GOP</Pill>}
          {v.browser_playable === false && <Pill tone="muted" title={v.browser_note ?? undefined}>plays in VLC only</Pill>}
          {notWide && <Pill tone="muted" title="Kept in the library; streams must be 16:9. Make a 16:9 copy to stream it.">
            Not 16:9 · {SHAPE_TEXT[v.shape ?? "other"]}</Pill>}
          {isStd && <Pill tone="info">{out!.height}p</Pill>}
          {!!v.copy_720p_id && <Pill tone="ok" title="Streams set to Standard use the 720p copy (no live transcoding)">720p copy ready</Pill>}
          {!!v.copy_16x9_id && <Pill tone="ok">16:9 copy ready</Pill>}
        </div>
        {job ? <ConversionProgress job={job} onCancel={canWrite ? onCancel : undefined} />
          : canWrite && (
            <>
              {reasons.length > 0 && !notWide && !offer720 && (
                <button className="btn-secondary w-full" onClick={onConvert} title={reasons.join("\n")}
                  aria-label={`Convert ${v.filename} to H.264`}>
                  <RefreshIcon /> Convert to H.264
                </button>
              )}
              {offer720 && (
                <button className="btn-secondary w-full" onClick={() => convertVideo(v, { profile: "720p" })}
                  title={[`One-off copy at ${out!.width}×${out!.height}, keyframe every 2 s, plays in every browser: `
                    + "Standard streams then use it without live transcoding.", ...reasons].join("\n")}
                  aria-label={`Make a 720p copy of ${v.filename}`}>
                  <RefreshIcon /> Make 720p copy
                </button>
              )}
            </>
          )}
        <div className="mt-auto flex gap-1.5">
          {canWrite && blocked && v.copy_16x9_id
            ? <button className="btn-primary flex-1" onClick={() => onCreateStream(v.copy_16x9_id!)}
                title="Streams the 16:9 copy of this video"><CameraIcon /> Stream 16:9 copy</button>
            : canWrite && blocked
            ? <button className="btn-primary flex-1" onClick={() => onFit(v)} disabled={!!job}
                title="Streams must be 16:9: make a 16:9 copy (the original is kept)"><RefreshIcon /> Make 16:9 copy</button>
            : canWrite
              ? <button className="btn-primary flex-1" onClick={() => onCreateStream(v.id)}><CameraIcon /> Stream this</button>
              : <button className="btn-secondary flex-1" onClick={() => setPlaying(true)}><PlayIcon /> Play</button>}
          <a className="btn-secondary px-2" href={videoFileUrl(v.id, true)} download={v.filename}
            title="Download to this computer" aria-label={`Download ${v.filename}`}><DownloadIcon /></a>
          {canWrite && isAdmin && (
            <button className="btn-secondary px-2 text-red-600 dark:text-red-400" onClick={() => onDelete(v)}
              disabled={v.streams_using > 0} title={v.streams_using > 0 ? "Used by a stream" : "Delete video (admin)"}
              aria-label={`Delete ${v.filename}`}><TrashIcon /></button>
          )}
        </div>
      </div>
      {details && <VideoDetails v={v} onClose={() => setDetails(false)} />}
      {playing && <VideoPlayer v={v} onClose={() => setPlaying(false)} job={job} preview={preview}
        onConvert={canWrite && reasons.length > 0 ? onConvert : undefined} />}
    </div>
  );
});

/** "Uploaded by alice · 2h ago", or "Added on server" for files copied straight into the video folder. */
export function UploaderLine({ v, now }: { v: Video; now?: number }) {
  if (!v.uploaded_by) {
    return <div className="truncate text-xs text-muted" data-testid="uploader">Added on server</div>;
  }
  const anon = v.uploaded_by === "anonymous";
  const who = anon ? (v.uploaded_device || v.uploaded_from || "anonymous") : v.uploaded_by;
  const when = v.uploaded_at ? `${since(v.uploaded_at, now)} ago` : null;
  const full = [v.uploaded_by, v.uploaded_at && new Date(v.uploaded_at).toLocaleString(), v.uploaded_from, v.uploaded_device]
    .filter(Boolean).join(" · ");
  return (
    <div className="truncate text-xs text-muted" data-testid="uploader" title={full}>
      Uploaded by <span className="font-medium text-current">{who}</span>{when && <> · {when}</>}
    </div>
  );
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[8.5rem_1fr] gap-2 border-b border-slate-100 py-1.5 text-sm last:border-0 dark:border-slate-800">
      <dt className="text-muted">{k}</dt><dd className="min-w-0 break-words">{children ?? "–"}</dd>
    </div>
  );
}

/** FFmpeg reports a family ("mov,mp4,m4a,…"): show the member matching the file's extension. */
export function containerName(v: Video): string | null {
  if (!v.container) return null;
  const names = v.container.split(",");
  const ext = v.rel_path.split(".").pop()?.toLowerCase() ?? "";
  return names.includes(ext) ? ext : names[0];
}

/** Everything known about one library video: who uploaded it, from where, and its technical details. */
export function VideoDetails({ v, onClose }: { v: Video; onClose: () => void }) {
  const fps = v.fps ? `${v.fps.toFixed(3).replace(/\.?0+$/, "")}${v.is_vfr ? " (variable)" : ""}` : null;
  const disp = v.display_width && v.display_height && (v.display_width !== v.width || v.display_height !== v.height)
    ? ` (shown as ${v.display_width}×${v.display_height})` : "";
  return (
    <Modal title={v.filename} onClose={onClose}>
      <dl data-testid="video-details">
        <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Upload</h3>
        {v.uploaded_by ? (
          <>
            <Row k="Uploaded by">{v.uploaded_by}</Row>
            <Row k="When">{v.uploaded_at ? `${new Date(v.uploaded_at).toLocaleString()} (${since(v.uploaded_at)} ago)` : null}</Row>
            <Row k="From address">{v.uploaded_from}</Row>
            <Row k="Device">{v.uploaded_device}</Row>
          </>
        ) : <Row k="Uploaded by">Added on server (copied into the video folder, not uploaded through this app)</Row>}
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">File</h3>
        <Row k="Path"><span className="font-mono text-xs">{v.rel_path}</span></Row>
        <Row k="Size">{formatBytes(v.size_bytes)}</Row>
        <Row k="Duration">{v.duration_s != null ? `${formatTime(v.duration_s, false)} (${formatDuration(v.duration_s)})` : null}</Row>
        <Row k="Container">{containerName(v)}</Row>
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Video</h3>
        <Row k="Resolution">{v.width && v.height ? `${v.width}×${v.height}${disp}` : null}</Row>
        <Row k="Shape">{v.shape ? (v.is_16_9 ? "16:9" : `Not 16:9 · ${SHAPE_TEXT[v.shape]}`) : null}</Row>
        <Row k="Codec">{[v.video_codec, v.video_profile, v.pix_fmt, v.bit_depth ? `${v.bit_depth}-bit` : null].filter(Boolean).join(" · ") || null}</Row>
        <Row k="Frame rate">{fps}</Row>
        <Row k="Bitrate">{v.bitrate_bps ? formatBitrate(v.bitrate_bps) : null}</Row>
        <Row k="Keyframes">{v.keyframe_interval_s ? `every ${v.keyframe_interval_s.toFixed(1)} s` : null}</Row>
        <Row k="Audio">{v.audio_codec ? `${v.audio_codec} (dropped when streaming)` : "none"}</Row>
        <h3 className="mb-1 mt-4 text-xs font-semibold uppercase tracking-wide text-muted">Streaming</h3>
        <Row k="Mode">{v.compat_mode === "copy" ? "Copy (no transcoding)" : v.compat_mode === "transcode" ? "Live transcode" : v.compat_mode}</Row>
        <Row k="In use by">{`${v.streams_using} stream${v.streams_using === 1 ? "" : "s"}`}</Row>
        <Row k="Copies">{[v.copy_720p_id && "720p copy ready", v.copy_16x9_id && "16:9 copy ready"].filter(Boolean).join(", ") || "none"}</Row>
        {[...v.compat_reasons, ...v.compat_warnings].length > 0 && (
          <Row k="Notes"><ul className="list-disc pl-4">{[...v.compat_reasons, ...v.compat_warnings].map((n) => <li key={n}>{n}</li>)}</ul></Row>
        )}
      </dl>
    </Modal>
  );
}

const FITS: { fit: ConvertFit; title: string; text: string }[] = [
  { fit: "blur", title: "Blurred bars (recommended)", text: "The whole picture, with a blurred, zoomed copy of it filling the sides. Nothing is cut off; looks natural for portrait clips." },
  { fit: "pad", title: "Black bars", text: "The whole picture with plain black bars. Nothing is cut off." },
  { fit: "crop", title: "Crop to fill", text: "Fills the frame and cuts off the top and bottom (or the sides). Objects near the edges can be lost." },
];

/** Choose how a portrait / 4:3 video is fitted into the standard 16:9 frame, then start the copy. */
export function FitDialog({ v, out, onClose, onStart }: {
  v: Video; out?: OutputConfig; onClose: () => void; onStart: (fit: ConvertFit) => Promise<string | null>;
}) {
  const [fit, setFit] = useState<ConvertFit>("blur");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const size = out ? `${out.width}×${out.height}` : "16:9";
  return (
    <Modal title={`Make a 16:9 copy of ${v.filename}`} onClose={onClose}>
      <div className="space-y-3 text-sm">
        <p className="text-muted">
          This video is {v.display_width}×{v.display_height} ({SHAPE_TEXT[v.shape ?? "other"]}). Streams must be 16:9, so a copy
          at {size} is made next to it; the original stays in the library for playing and downloading.
        </p>
        <div className="space-y-2" role="radiogroup" aria-label="How to fit the picture">
          {FITS.map((f) => (
            <label key={f.fit} className={`flex cursor-pointer gap-3 rounded-lg border p-3 ${fit === f.fit ? "border-brand bg-brand/5" : "border-line"}`}>
              <input type="radio" name="fit" className="mt-1" checked={fit === f.fit} onChange={() => setFit(f.fit)} />
              <span><span className="font-medium">{f.title}</span><br /><span className="text-muted">{f.text}</span></span>
            </label>
          ))}
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy}
            onClick={async () => { setBusy(true); setErr(await onStart(fit)); setBusy(false); }}>
            {busy ? <Spinner /> : <RefreshIcon />} Make 16:9 copy
          </button>
        </div>
      </div>
    </Modal>
  );
}

function PreviewPlayer({ v, clip, onClose }: { v: Video; clip: number; onClose: () => void }) {
  const duration = v.duration_s ?? 0;
  const starts = previewStarts(duration, clip);
  const [start, setStart] = useState(0);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const pick = (s: number) => { if (s !== start) { setStart(s); setLoading(true); setFailed(false); } };
  return (
    <Modal title={v.filename} wide onClose={onClose}>
      <p className="mb-2 text-sm text-muted" data-testid="preview-label">
        Preview: {formatDuration(clip)} from {formatTime(start, false)} of a {formatDuration(duration)} video.
        Download it for the full length.
      </p>
      <div className="relative">
        <video key={start} data-testid="video-player" className="max-h-[70vh] w-full rounded bg-black" src={previewUrl(v.id, start)}
          controls autoPlay playsInline preload="auto" onLoadedData={() => setLoading(false)}
          onError={() => { setLoading(false); setFailed(true); }} />
        {loading && !failed && (
          <div className="absolute inset-0 flex items-center justify-center gap-2 rounded bg-black/60 text-sm text-white">
            <Spinner /> Preparing the preview (first time only, a few seconds)…
          </div>
        )}
      </div>
      {failed && <div className="mt-2"><ErrorBox>The preview could not be made. Download the video to watch it.</ErrorBox></div>}
      <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
        <span className="text-muted">Start the preview at</span>
        {starts.map((s) => (
          <button key={s} className={s === start ? "btn-primary btn-xs" : "btn-secondary btn-xs"} onClick={() => pick(s)}
            aria-pressed={s === start}>{formatTime(s, false)}</button>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)}</span>
        <a className="btn-secondary" href={videoFileUrl(v.id, true)} download={v.filename}><DownloadIcon /> Download full video</a>
      </div>
    </Modal>
  );
}

function UploadButton({ onDone: _onDone }: { onDone: () => void }) {
  // Uploads run in the app-wide queue (bottom-right panel), so they keep going while you
  // move between pages; the library refreshes when each file lands.
  const { enqueue } = useUploads();
  const input = useRef<HTMLInputElement>(null);
  return (
    <>
      <input ref={input} type="file" multiple className="hidden" accept="video/*,.mkv,.ts,.mts,.m2ts"
        onChange={(e) => { enqueue(Array.from(e.target.files ?? [])); e.target.value = ""; }} />
      <button className="btn-primary" onClick={() => input.current?.click()} title="You can also drop videos anywhere on the page">
        <UploadIcon /> Upload videos
      </button>
    </>
  );
}
