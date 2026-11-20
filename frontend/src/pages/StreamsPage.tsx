import { useNavigate } from "react-router-dom";
import { usePolling } from "../hooks/usePolling";
import { api } from "../services/api";
import { formatTime } from "../lib/format";
import { useRig } from "../hooks/useRigData";
import { useCreateStream } from "../components/AppShell";
import { Skeleton } from "../components/common";
import { CameraIcon, EyeIcon, AlertIcon, ServerIcon, PlusIcon, FilmIcon } from "../components/icons";
import { isProblem, StreamsTable } from "../components/StreamsTable";

export function StreamsPage() {
  const rig = useRig();
  const navigate = useNavigate();
  const create = useCreateStream();
  const list = rig.streams ?? [];
  const groups = usePolling(api.groups, 5000);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Streams</h1>
          <p className="text-sm text-muted">Recorded videos published as live RTSP cameras.</p>
        </div>
      </div>

      <StreamsSummary />

      {rig.streamsLoading && !rig.streams ? (
        <div className="grid gap-3 xl:grid-cols-2">{[0, 1, 2, 3].map((i) => <Skeleton key={i} className="h-36" />)}</div>
      ) : list.length === 0 ? (
        <GettingStarted canWrite={rig.user.can_write} hasVideos={(rig.videos?.length ?? 0) > 0}
          onLibrary={() => navigate("/library")} onCreate={() => create()} />
      ) : (
        <StreamsTable streams={list} user={rig.user} onSelect={(s) => navigate(`/streams/${s.id}`)}
          onChanged={rig.refreshStreams} mediamtxDown={rig.mediamtx?.state === "DOWN"}
          groups={groups.data ?? []} onGroupsChanged={groups.refresh}
          onClone={(s) => s.video_id && create(s.video_id, {
            videoId: s.video_id, name: s.name, mode: s.playback_mode, forceTranscode: s.force_transcode,
            clock: s.clock, clockTz: s.clock_tz,
            start: formatTime(s.start_offset_s, true), end: s.end_offset_s !== null ? formatTime(s.end_offset_s, true) : "",
          })} />
      )}
    </div>
  );
}

/**
 * One line with the same numbers the Overview shows as cards (live, needs attention, viewers, capacity).
 * Its own component: CPU/capacity updates redraw this line only, not the stream list.
 */
function StreamsSummary() {
  const { streams, system } = useRig();
  const list = streams ?? [];
  const live = list.filter((s) => s.status === "RUNNING").length;
  const problems = list.filter(isProblem).length;
  const cap = system?.streams;
  const full = cap && cap.desired_running >= cap.max_active;
  const item = "inline-flex items-center gap-1.5 whitespace-nowrap";
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-sm text-muted" data-testid="streams-summary">
      <span className={item}><CameraIcon className="h-4 w-4 text-emerald-700 dark:text-emerald-400" />
        <b className="tabular-nums text-fg">{streams ? live : "–"}</b> live of {list.length}</span>
      <span className={item}><AlertIcon className={`h-4 w-4 ${problems ? "text-red-600" : ""}`} />
        <b className={`tabular-nums ${problems ? "text-red-600 dark:text-red-400" : "text-fg"}`}>{streams ? problems : "–"}</b> need attention</span>
      <span className={item} title="RTSP readers (edges)"><EyeIcon className="h-4 w-4 text-blue-600 dark:text-blue-400" />
        <b className="tabular-nums text-fg">{system ? system.viewers ?? "?" : "–"}</b> viewers</span>
      <span className={item} title="Streams set to run / the server's limit · live transcodes / their limit">
        <ServerIcon className={`h-4 w-4 ${full ? "text-red-600" : "text-violet-600 dark:text-violet-400"}`} />
        capacity <b className={`tabular-nums ${full ? "text-red-600" : "text-fg"}`}>{cap ? `${cap.desired_running}/${cap.max_active}` : "–"}</b>
        {cap && <span className="text-subtle">· {cap.transcoding}/{cap.max_transcode} transcoding</span>}</span>
    </div>
  );
}

function GettingStarted({ canWrite, hasVideos, onLibrary, onCreate }: {
  canWrite: boolean; hasVideos: boolean; onLibrary: () => void; onCreate: () => void;
}) {
  const steps = [
    { title: "Add a video", text: "Upload it in the Video library, or copy it into the server's video folder and press Refresh.", done: hasVideos, icon: <FilmIcon /> },
    { title: "Create a stream", text: "Pick the video, a unique name and a start time; loop or play once.", done: false, icon: <PlusIcon /> },
    { title: "Add the camera", text: "Copy the rtsp:// URL into the Video Analytics portal as the camera URL.", done: false, icon: <CameraIcon /> },
  ];
  return (
    <div className="card overflow-hidden">
      <div className="bg-gradient-to-br from-blue-600 to-indigo-700 px-6 py-6 text-white">
        <h2 className="text-lg font-semibold">Publish a recorded video as a live camera</h2>
        <p className="mt-1 text-sm text-blue-100">No streams yet. Three steps, under a minute.</p>
      </div>
      <ol className="grid gap-3 p-6 md:grid-cols-3">
        {steps.map((s, i) => (
          <li key={s.title} className={`rounded-xl border p-4 ${s.done ? "border-emerald-500/30 bg-emerald-500/5" : "border-line"}`}>
            <div className="flex items-center gap-2 text-sm font-semibold">
              <span className={`flex h-7 w-7 items-center justify-center rounded-full text-xs ${s.done ? "bg-emerald-600 text-white" : "bg-brand-soft text-brand"}`}>
                {s.done ? "✓" : i + 1}
              </span>
              {s.title}
            </div>
            <p className="mt-2 text-sm text-muted">{s.text}</p>
          </li>
        ))}
      </ol>
      <div className="flex gap-2 px-6 pb-6">
        {canWrite && hasVideos && <button className="btn-primary" onClick={onCreate}><PlusIcon /> New stream</button>}
        <button className="btn-secondary" onClick={onLibrary}>Open video library</button>
      </div>
    </div>
  );
}
