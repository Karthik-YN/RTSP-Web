import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { api, ApiError, uploadVideo } from "../services/api";
import { formatBytes, formatTime } from "../lib/format";
import type { Video } from "../types/api";
import { useToast } from "./common";
import { CheckIcon, UploadIcon } from "./icons";

/**
 * App-wide upload queue: files go to the server one after another, keep going
 * while you move between pages, and resume by themselves after a network drop
 * (see uploadVideo). Drop video files anywhere on the page to queue them.
 */

type ItemState = "queued" | "uploading" | "done" | "error" | "cancelled";
interface Item {
  id: number;
  file: File;
  state: ItemState;
  loaded: number;
  started: number;
  note?: string;
  error?: string;
  onDone?: (v: Video) => void | Promise<void>;
  onFail?: (message: string) => void;
}

interface UploadApi {
  /** Queue files; `onDone` runs for each finished file (e.g. attach it to a placeholder),
   * `onFail` when a file fails or is cancelled. */
  enqueue: (files: File[], onDone?: (v: Video) => void | Promise<void>, onFail?: (message: string) => void) => void;
  /** Names of files still uploading or queued (to show a hint where they will appear). */
  pending: string[];
}

const Ctx = createContext<UploadApi>({ enqueue: () => {}, pending: [] });
export const useUploads = () => useContext(Ctx);

const VIDEO_EXT = /\.(mp4|m4v|mov|mkv|webm|avi|ts|mts|m2ts|mpg|mpeg|flv|wmv|3gp)$/i;
export const isVideoFile = (f: File) => f.type.startsWith("video/") || VIDEO_EXT.test(f.name);

export function UploadQueueProvider({ children, onUploaded }: { children: ReactNode; onUploaded: () => void }) {
  const toast = useToast();
  const [items, setItems] = useState<Item[]>([]);
  const [dragging, setDragging] = useState(false);
  const nextId = useRef(1);
  const abortRef = useRef<(() => void) | null>(null);
  const busy = useRef(false);

  const patch = (id: number, p: Partial<Item>) => setItems((xs) => xs.map((x) => (x.id === id ? { ...x, ...p } : x)));

  const enqueue = useCallback((files: File[], onDone?: Item["onDone"], onFail?: Item["onFail"]) => {
    const vids = files.filter(isVideoFile);
    if (vids.length < files.length) toast("info", `Skipped ${files.length - vids.length} file(s) that are not videos.`);
    setItems((xs) => [...xs, ...vids.map((file) => ({ id: nextId.current++, file, state: "queued" as const, loaded: 0,
      started: 0, onDone, onFail }))]);
  }, [toast]);

  // Run the queue: one upload at a time, so each gets the full bandwidth.
  useEffect(() => {
    if (busy.current) return;
    const it = items.find((x) => x.state === "queued");
    if (!it) return;
    busy.current = true;
    (async () => {
      patch(it.id, { state: "uploading", started: Date.now() });
      try {
        const chk = await api.uploadCheck(it.file.name, it.file.size);
        if (chk.exists) throw new ApiError(409, `A file named '${chk.filename}' is already in the library.`);
        if (!chk.ok) {
          throw new ApiError(507, `Not enough disk space on the server: needs ${formatBytes(chk.required)}, ` +
            `available ${formatBytes(chk.available)}.`);
        }
        const { promise, abort } = uploadVideo(it.file, (loaded, _t, note) => patch(it.id, { loaded, note }));
        abortRef.current = abort;
        const v = await promise;
        patch(it.id, { state: "done", loaded: it.file.size, note: undefined });
        toast("ok", `Uploaded ${v.filename} (${formatTime(v.duration_s, false)}, ${v.video_codec ?? "?"})`);
        onUploaded();
        try {
          await it.onDone?.(v);
        } catch (e) {
          toast("bad", (e as ApiError).message);
        }
      } catch (e) {
        const err = e as ApiError;
        patch(it.id, err.status === -1 ? { state: "cancelled" } : { state: "error", error: err.message });
        it.onFail?.(err.status === -1 ? "Upload cancelled." : err.message);
      } finally {
        abortRef.current = null;
        busy.current = false;
        setItems((xs) => [...xs]); // wake the queue for the next file
      }
    })();
  }, [items, onUploaded, toast]);

  // Drop anywhere: a full-page target appears while files are dragged over the window.
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const enter = (e: DragEvent) => { if (hasFiles(e)) { depth++; setDragging(true); } };
    const leave = (e: DragEvent) => { if (hasFiles(e) && --depth <= 0) { depth = 0; setDragging(false); } };
    const over = (e: DragEvent) => { if (hasFiles(e)) e.preventDefault(); };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      // a drop zone (e.g. a placeholder card) that handled it marks the event
      if ((e as DragEvent & { rigHandled?: boolean }).rigHandled) return;
      const files = Array.from(e.dataTransfer?.files ?? []);
      if (files.length) enqueue(files);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  }, [enqueue]);

  // Warn before closing the tab while uploads are running.
  const active = items.some((x) => x.state === "uploading" || x.state === "queued");
  useEffect(() => {
    if (!active) return;
    const h = (e: BeforeUnloadEvent) => { e.preventDefault(); };
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [active]);

  const pending = items.filter((x) => x.state === "uploading" || x.state === "queued").map((x) => x.file.name);
  return (
    <Ctx.Provider value={{ enqueue, pending }}>
      {children}
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-brand/10 backdrop-blur-[1px]">
          <div className="rounded-2xl border-2 border-dashed border-brand bg-surface px-8 py-6 text-center shadow-lg">
            <UploadIcon className="mx-auto h-8 w-8 text-brand" />
            <div className="mt-2 font-semibold">Drop videos to upload them to the server</div>
            <div className="text-sm text-muted">Drop onto a video placeholder to attach it there</div>
          </div>
        </div>
      )}
      <QueuePanel items={items} onCancel={(it) => {
        if (it.state === "uploading") abortRef.current?.();
        else { patch(it.id, { state: "cancelled" }); it.onFail?.("Upload cancelled."); }
      }} onClear={() => setItems((xs) => xs.filter((x) => x.state === "uploading" || x.state === "queued"))} />
    </Ctx.Provider>
  );
}

function QueuePanel({ items, onCancel, onClear }: { items: Item[]; onCancel: (it: Item) => void; onClear: () => void }) {
  const [open, setOpen] = useState(true);
  if (!items.length) return null;
  const running = items.filter((x) => x.state === "uploading" || x.state === "queued").length;
  return (
    <div className="fixed bottom-4 left-4 z-40 w-[22rem] max-w-[calc(100vw-2rem)] lg:left-[19rem] overflow-hidden rounded-xl border border-line bg-surface shadow-lg"
      role="region" aria-label="Uploads">
      <div className="flex items-center gap-2 border-b border-line px-3 py-2 text-sm">
        <UploadIcon className="h-4 w-4 text-brand" />
        <span className="flex-1 font-semibold">{running ? `Uploading ${running} file(s)` : "Uploads finished"}</span>
        {!running && <button className="btn-ghost btn-xs" onClick={onClear}>Clear</button>}
        <button className="btn-ghost btn-xs" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide" : "Show"}</button>
      </div>
      {open && (
        <ul className="max-h-72 divide-y divide-line overflow-y-auto">
          {items.map((it) => {
            const pct = Math.floor((it.loaded / Math.max(1, it.file.size)) * 100);
            const rate = it.started ? it.loaded / Math.max(0.5, (Date.now() - it.started) / 1000) : 0;
            return (
              <li key={it.id} className="px-3 py-2 text-xs">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-medium" title={it.file.name}>{it.file.name}</span>
                  {it.state === "done" && <CheckIcon className="h-4 w-4 text-emerald-600" />}
                  {(it.state === "uploading" || it.state === "queued") && (
                    <button className="btn-ghost btn-xs" onClick={() => onCancel(it)}>Cancel</button>
                  )}
                </div>
                {it.state === "uploading" && (
                  <>
                    <div className="mt-1 h-1.5 overflow-hidden rounded bg-line"><div className="h-full bg-brand-strong transition-all" style={{ width: `${pct}%` }} /></div>
                    <div className="mt-0.5 text-muted">{pct}% · {formatBytes(it.loaded)} / {formatBytes(it.file.size)} · {formatBytes(rate)}/s
                      {it.note && <span className="ml-1 text-amber-600 dark:text-amber-400">{it.note}</span>}</div>
                  </>
                )}
                {it.state === "queued" && <div className="text-muted">Waiting · {formatBytes(it.file.size)}</div>}
                {it.state === "done" && <div className="text-muted">On the server</div>}
                {it.state === "cancelled" && <div className="text-muted">Cancelled</div>}
                {it.state === "error" && <div className="text-red-600 dark:text-red-400">{it.error}</div>}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
