import { useEffect, useRef, useState } from "react";
import type Hls from "hls.js";
import { Spinner } from "./common";

type State = "connecting" | "playing" | "error";

/**
 * Live preview of a stream. Browsers can't play RTSP, so this plays the same
 * video as HLS through the backend (behind the dashboard login, no extra port).
 * It is delayed by a few seconds compared with the RTSP URL, more for files
 * with long keyframe intervals. Keeps retrying while the stream is starting.
 * If the browser can't play it (codec, no MSE) or it hasn't started after 15 s, it shows a fresh still
 * frame from the RTSP stream every few seconds instead of spinning forever, and keeps trying the video.
 */
export function LivePlayer({ streamId, className = "" }: { streamId: number; className?: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<State>("connecting");
  const [slow, setSlow] = useState(false);
  const [stills, setStills] = useState(false);
  const [frame, setFrame] = useState<string | null>(null);

  useEffect(() => {
    const el = video.current;
    if (!el) return;
    const src = `/api/streams/${streamId}/live/index.m3u8`;
    let hls: Hls | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    let failures = 0;

    const scheduleRetry = () => {
      if (closed) return;
      failures += 1;
      if (failures > 3) setState("error");
      retry = setTimeout(start, 2000);
    };
    const onPlaying = () => { failures = 0; setState("playing"); setStills(false); };
    const slowTimer = setTimeout(() => setSlow(true), 7000);
    const stillsTimer = setTimeout(() => setStills(true), 15000); // not playing yet: show still frames meanwhile
    el.addEventListener("playing", onPlaying);

    // hls.js is about 500 kB: load it only when a live preview is actually opened.
    let Hls: typeof import("hls.js").default | null = null;
    function start() {
      if (closed || !el) return;
      hls?.destroy();
      if (Hls?.isSupported()) {
        // the server holds the first playlist until 3 segments exist (up to ~30 s for long keyframe intervals)
        hls = new Hls({ liveSyncDurationCount: 2, manifestLoadingTimeOut: 60000, levelLoadingTimeOut: 30000,
          fragLoadingTimeOut: 60000, backBufferLength: 30 });
        hls.on(Hls.Events.ERROR, (_e, data) => {
          // the browser can't decode this video (codec): still frames instead of waiting
          if (data.type === "mediaError" || /Codec|codec/.test(String(data.details))) setStills(true);
          if (!data.fatal) return;
          hls?.destroy();
          hls = null;
          scheduleRetry();
        });
        hls.on(Hls.Events.MANIFEST_PARSED, () => { el.play().catch(() => {}); });
        hls.loadSource(src);
        hls.attachMedia(el);
      } else if (el.canPlayType("application/vnd.apple.mpegurl")) {
        el.src = src; // Safari plays HLS natively
        el.onerror = () => scheduleRetry();
        el.play().catch(() => {});
      } else {
        setState("error");
        setStills(true); // no HLS in this browser at all
      }
    }
    import("hls.js")
      .then((m) => { Hls = m.default; start(); })
      .catch(() => start()); // Safari can still play natively

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      clearTimeout(slowTimer);
      clearTimeout(stillsTimer);
      el.removeEventListener("playing", onPlaying);
      hls?.destroy();
    };
  }, [streamId]);

  // Still frames: load each one fully off-screen, show it, then ask for the next 2 s later
  // (a frame grab takes 2-3 s, so a fixed timer would replace frames before they ever appear).
  useEffect(() => {
    if (!stills || state === "playing") return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let shown: string | null = null;
    const next = async () => {
      if (!alive) return;
      if (document.hidden) { timer = setTimeout(next, 2000); return; }
      try {
        // fetched once and shown from memory (snapshots are no-store: an <img src> would download it again)
        const r = await fetch(`/api/streams/${streamId}/snapshot?t=${Date.now()}`);
        if (!r.ok) throw new Error(String(r.status));
        const url = URL.createObjectURL(await r.blob());
        if (!alive) { URL.revokeObjectURL(url); return; }
        if (shown) URL.revokeObjectURL(shown);
        shown = url;
        setFrame(url);
        timer = setTimeout(next, 2000);
      } catch {
        if (alive) timer = setTimeout(next, 4000);
      }
    };
    void next();
    return () => { alive = false; if (timer) clearTimeout(timer); if (shown) URL.revokeObjectURL(shown); };
  }, [stills, state, streamId]);

  return (
    <div className={`relative aspect-video overflow-hidden rounded-lg bg-slate-950 ${className}`}>
      <video ref={video} className="h-full w-full object-contain" muted playsInline controls={state === "playing"}
        aria-label="Live preview" />
      {state === "playing" && (
        <span className="absolute left-2 top-2 flex items-center gap-1.5 rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" /> Live
        </span>
      )}
      {state !== "playing" && stills && (
        <>
          {frame ? <img src={frame} alt="Still frame from the stream" data-testid="preview-stills"
            className="absolute inset-0 h-full w-full object-contain" />
            : <div className="absolute inset-0 flex items-center justify-center"><Spinner /></div>}
          <span className="absolute left-2 top-2 rounded bg-black/70 px-1.5 py-0.5 text-[10px] font-medium text-white"
            title="The browser can't play the live video here; showing a fresh frame from the RTSP stream every few seconds">
            Still frames · about every 4 s
          </span>
        </>
      )}
      {state !== "playing" && !stills && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center text-xs text-slate-300">
          {state === "connecting" && <Spinner />}
          {state === "connecting" ? "Connecting to the live preview…" : "Preview not available yet; still trying…"}
          {slow && <span className="text-slate-400">The preview needs three keyframe intervals, so footage with long intervals takes up to about 30 s. The RTSP URL already works.</span>}
        </div>
      )}
    </div>
  );
}
