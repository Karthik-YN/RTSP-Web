import { useRef, useState } from "react";
import { api, ApiError } from "../services/api";
import { useUploads } from "./UploadQueue";
import { formatBytes, formatTime } from "../lib/format";
import type { User, Video } from "../types/api";
import { ConfirmDialog, Modal, Pill, Spinner, useToast } from "./common";
import { CameraIcon, DownloadIcon, FilmIcon, PlayIcon, RefreshIcon, SearchIcon, TrashIcon, UploadIcon } from "./icons";

export function VideoLibrary({ videos, loading, user, onChanged, onCreateStream, search, setSearch }: {
  videos: Video[]; loading: boolean; user: User | null; onChanged: () => void;
  onCreateStream: (videoId: number) => void; search: string; setSearch: (s: string) => void;
}) {
  const toast = useToast();
  const [refreshing, setRefreshing] = useState(false);

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
          onCreateStream={onCreateStream} onDelete={setDeleting} />)}
      </div>

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

/** Browsers play H.264 (and usually VP9/AV1); H.265 and 10-bit often not. */
function browserMayNotPlay(v: Video): boolean {
  const codec = (v.video_codec ?? "").toLowerCase();
  return codec === "hevc" || codec === "h265" || (v.bit_depth ?? 8) > 8;
}

export function VideoPlayer({ v, onClose }: { v: Video; onClose: () => void }) {
  const [failed, setFailed] = useState(false);
  return (
    <Modal title={v.filename} wide onClose={onClose}>
      {failed ? (
        <div className="space-y-2 text-sm">
          <p>This browser can't play this file ({v.video_codec}{v.bit_depth && v.bit_depth > 8 ? `, ${v.bit_depth}-bit` : ""}).</p>
          <p className="text-muted">Download it and open it in VLC. It can still be streamed: the server converts it for RTSP.</p>
        </div>
      ) : (
        <video data-testid="video-player" className="max-h-[70vh] w-full rounded bg-black" src={videoFileUrl(v.id)}
          controls autoPlay playsInline preload="metadata" onError={() => setFailed(true)} />
      )}
      <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-muted">
        <span>{v.width}×{v.height} · {v.video_codec} · {formatBytes(v.size_bytes)}{browserMayNotPlay(v) && !failed
          ? " · may not play in every browser" : ""}</span>
        <a className="btn-secondary" href={videoFileUrl(v.id, true)} download={v.filename}><DownloadIcon /> Download</a>
      </div>
    </Modal>
  );
}

function VideoCard({ v, canWrite, isAdmin, onCreateStream, onDelete }: {
  v: Video; canWrite: boolean; isAdmin: boolean; onCreateStream: (id: number) => void; onDelete: (v: Video) => void;
}) {
  const tip = [...v.compat_reasons, ...v.compat_warnings].join("\n");
  const [playing, setPlaying] = useState(false);
  return (
    <div className="card flex flex-col overflow-hidden">
      <div className="group relative aspect-video bg-slate-800">
        {v.thumbnail_url
          ? <img src={v.thumbnail_url} alt="" loading="lazy" className="h-full w-full object-contain" />
          : <div className="flex h-full items-center justify-center"><FilmIcon className="h-8 w-8 text-muted" /></div>}
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
        <div className="break-all text-sm font-medium leading-snug" title={v.rel_path}>{v.rel_path}</div>
        <div className="flex flex-wrap gap-x-2 text-xs text-muted">
          <span>{v.width}×{v.height}</span>
          <span>{v.fps ? `${v.fps.toFixed(3).replace(/\.?0+$/, "")} fps` : "–"}</span>
          <span>{v.video_codec}{v.bit_depth && v.bit_depth !== 8 ? ` ${v.bit_depth}-bit` : ""}</span>
          <span>{formatBytes(v.size_bytes)}</span>
        </div>
        <div className="flex flex-wrap gap-1" title={tip}>
          {v.compat_mode === "copy" && <Pill tone="ok">Edge-ready (copy)</Pill>}
          {v.compat_mode === "transcode" && <Pill tone="warn">Needs transcode</Pill>}
          {v.is_vfr && <Pill tone="info">VFR</Pill>}
          {v.audio_codec && <Pill tone="muted">audio dropped</Pill>}
          {v.keyframe_interval_s && v.keyframe_interval_s > 4 && <Pill tone="warn">long GOP</Pill>}
        </div>
        <div className="mt-auto flex gap-1.5">
          {canWrite
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
      {playing && <VideoPlayer v={v} onClose={() => setPlaying(false)} />}
    </div>
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
