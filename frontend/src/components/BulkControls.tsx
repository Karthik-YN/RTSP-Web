import { useState, type ReactNode } from "react";
import { useRig } from "../hooks/useRigData";
import { api, ApiError } from "../services/api";
import type { BulkResult, Stream } from "../types/api";
import { ConfirmDialog, Spinner, useToast } from "./common";
import { PlayIcon, RestartIcon, StopIcon } from "./icons";

export type BulkAction = "start" | "stop" | "restart";

const LIVE = new Set(["RUNNING", "STARTING", "RESTARTING", "LEAD_IN", "WAITING_FOR_VIEWER"]);
export const isStreamLive = (s: Stream) => LIVE.has(s.status);

/**
 * Run start/stop/restart on many streams at once (POST /api/streams/bulk), with a confirm step
 * whenever the action would cut off someone reading a stream, and one toast for the result.
 */
export function useBulkRun(onDone?: () => void) {
  const rig = useRig();
  const toast = useToast();
  const [working, setWorking] = useState<BulkAction | null>(null);
  const [pending, setPending] = useState<{ action: BulkAction; streams: Stream[]; label: string } | null>(null);

  const exec = async (action: BulkAction, streams: Stream[], label: string) => {
    if (!streams.length) return;
    setWorking(action);
    try {
      const r: BulkResult = await api.bulk(streams.map((s) => s.id), action);
      const failed = r.results.filter((x) => !x.ok);
      const verb = { start: "Started", stop: "Stopped", restart: "Restarted" }[action];
      if (failed.length) toast("bad", `${label}: ${r.ok} ok, ${failed.length} failed (${failed[0].error})`);
      else toast("ok", `${label}: ${verb.toLowerCase()} ${r.ok} stream${r.ok === 1 ? "" : "s"}.`);
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setWorking(null);
      rig.refreshStreams();
      onDone?.();
    }
  };

  /** Ask first when readers would drop (stop/restart of more than one stream, or any with viewers). */
  const run = (action: BulkAction, streams: Stream[], label: string) => {
    const viewers = streams.reduce((n, s) => n + (s.runtime?.viewer_count ?? 0), 0);
    if (action !== "start" && (streams.length > 1 || viewers > 0)) setPending({ action, streams, label });
    else exec(action, streams, label);
  };

  const cap = rig.system?.streams;
  const dialog = pending && (() => {
    const viewers = pending.streams.reduce((n, s) => n + (s.runtime?.viewer_count ?? 0), 0);
    const n = pending.streams.length;
    const verb = pending.action === "stop" ? "Stop" : "Restart";
    return (
      <ConfirmDialog danger={pending.action === "stop"} title={`${verb} ${n} stream${n === 1 ? "" : "s"}?`}
        confirmLabel={`${verb} ${n}`}
        message={<>
          <p>{pending.label}.</p>
          {viewers > 0
            ? <p className="mt-2 font-medium text-amber-700 dark:text-amber-300">{viewers} reader{viewers === 1 ? " is" : "s are"} watching right now (an edge or a tester) and will lose the feed{pending.action === "restart" ? " for a few seconds" : ""}.</p>
            : <p className="mt-2">Nobody is reading these streams right now.</p>}
          {pending.action === "stop" && <p className="mt-2">The RTSP URLs stay the same; start them again any time.</p>}
        </>}
        onCancel={() => setPending(null)}
        onConfirm={() => { const p = pending; setPending(null); exec(p.action, p.streams, p.label); }} />
    );
  })();

  /** Free start slots, so "Start all" can say up front how many will fit. */
  const freeSlots = cap ? Math.max(0, cap.max_active - cap.desired_running) : null;
  return { run, working, dialog, freeSlots };
}

/**
 * The fast buttons for a set of streams: "▶ Start 5" (the stopped ones) and "■ Stop 12" (the live ones).
 * A button with nothing to act on is not shown (no greyed-out "Start 0").
 * Used for "all shown" on the Streams page and for all streams of one video in the library.
 */
export function StreamSetControls({ streams, label, size = "xs", showRestart = false, extra }: {
  streams: Stream[]; label: string; size?: "xs" | "sm"; showRestart?: boolean; extra?: ReactNode;
}) {
  const { user } = useRig();
  const { run, working, dialog, freeSlots } = useBulkRun();
  if (!user.can_write || !streams.length) return null;
  const live = streams.filter(isStreamLive);
  const stopped = streams.filter((s) => !isStreamLive(s));
  const btn = size === "xs" ? "btn-secondary btn-xs" : "btn-secondary";
  const ic = "h-3.5 w-3.5";
  const tooMany = freeSlots !== null && stopped.length > freeSlots;
  return (
    <div className="flex flex-wrap items-center gap-1.5" onClick={(e) => e.stopPropagation()}>
      {stopped.length > 0 && <button className={btn} disabled={!!working}
        title={tooMany ? `Only ${freeSlots} start slot(s) free (capacity); the rest will be refused` : `Start the ${stopped.length} stopped stream(s)`}
        onClick={() => run("start", stopped, `Start ${label}`)}>
        {working === "start" ? <Spinner /> : <PlayIcon className={`${ic} text-emerald-700`} />} Start {stopped.length}
      </button>}
      {live.length > 0 && <button className={btn} disabled={!!working} title={`Stop the ${live.length} live stream(s)`}
        onClick={() => run("stop", live, `Stop ${label}`)}>
        {working === "stop" ? <Spinner /> : <StopIcon className={`${ic} text-red-600`} />} Stop {live.length}
      </button>}
      {showRestart && live.length > 0 && (
        <button className={btn} disabled={!!working} title={`Restart the ${live.length} live stream(s)`}
          onClick={() => run("restart", live, `Restart ${label}`)}>
          {working === "restart" ? <Spinner /> : <RestartIcon className={ic} />} Restart
        </button>
      )}
      {extra}
      {dialog}
    </div>
  );
}
