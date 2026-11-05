import { useEffect, useRef, useState } from "react";
import Hls from "hls.js";
import { Spinner } from "./common";

type State = "connecting" | "playing" | "error";

/**
 * Live preview of a stream. Browsers can't play RTSP, so this plays the same
 * video as HLS through the backend (behind the dashboard login, no extra port).
 * It is delayed by a few seconds compared with the RTSP URL, more for files
 * with long keyframe intervals. Keeps retrying while the stream is starting.
 */
export function LivePlayer({ streamId, className = "" }: { streamId: number; className?: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<State>("connecting");
  const [slow, setSlow] = useState(false);

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
    const onPlaying = () => { failures = 0; setState("playing"); };
    const slowTimer = setTimeout(() => setSlow(true), 7000);
    el.addEventListener("playing", onPlaying);

    function start() {
      if (closed || !el) return;
      hls?.destroy();
      if (Hls.isSupported()) {
        // the server holds the first playlist until 3 segments exist (up to ~30 s for long keyframe intervals)
        hls = new Hls({ liveSyncDurationCount: 2, manifestLoadingTimeOut: 60000, levelLoadingTimeOut: 30000,
          fragLoadingTimeOut: 60000, backBufferLength: 30 });
        hls.on(Hls.Events.ERROR, (_e, data) => {
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
      }
    }
    start();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      clearTimeout(slowTimer);
      el.removeEventListener("playing", onPlaying);
      hls?.destroy();
    };
  }, [streamId]);

  return (
    <div className={`relative aspect-video overflow-hidden rounded-lg bg-slate-950 ${className}`}>
      <video ref={video} className="h-full w-full object-contain" muted playsInline controls={state === "playing"}
        aria-label="Live preview" />
      {state === "playing" && (
        <span className="absolute left-2 top-2 flex items-center gap-1.5 rounded bg-red-600 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-white">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-white" /> Live
        </span>
      )}
      {state !== "playing" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 p-4 text-center text-xs text-slate-300">
          {state === "connecting" && <Spinner />}
          {state === "connecting" ? "Connecting to the live preview…" : "Preview not available yet; still trying…"}
          {slow && <span className="text-slate-400">The preview needs three keyframe intervals, so footage with long intervals takes up to about 30 s. The RTSP URL already works.</span>}
        </div>
      )}
    </div>
  );
}
