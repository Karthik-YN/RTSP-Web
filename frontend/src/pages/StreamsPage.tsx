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
  const live = list.filter((s) => s.status === "RUNNING").length;
  const problems = list.filter(isProblem).length;
  const cap = rig.system?.streams;
  const groups = usePolling(api.groups, 5000);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Streams</h1>
          <p className="text-sm text-muted">Recorded videos published as live RTSP cameras.</p>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat icon={<CameraIcon />} tone="emerald" label="Live" value={rig.streams ? `${live}` : null} sub={`of ${list.length} streams`} />
        <Stat icon={<AlertIcon />} tone={problems ? "red" : "slate"} label="Needs attention" value={rig.streams ? `${problems}` : null}
          sub={problems ? "see the red cards" : "all good"} />
        <Stat icon={<EyeIcon />} tone="blue" label="Viewers" value={rig.system ? `${rig.system.viewers ?? "?"}` : null} sub="RTSP readers (edges)" />
        <Stat icon={<ServerIcon />} tone={cap && cap.desired_running >= cap.max_active ? "red" : "violet"} label="Capacity"
          value={cap ? `${cap.desired_running}/${cap.max_active}` : null}
          sub={cap ? `${cap.transcoding}/${cap.max_transcode} transcoding` : ""} />
      </div>

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
            start: formatTime(s.start_offset_s, true), end: s.end_offset_s !== null ? formatTime(s.end_offset_s, true) : "",
          })} />
      )}
    </div>
  );
}

const TONES: Record<string, string> = {
  emerald: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  red: "bg-red-500/10 text-red-600 dark:text-red-400",
  blue: "bg-blue-500/10 text-blue-600 dark:text-blue-400",
  violet: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
  slate: "bg-slate-500/10 text-muted",
};

function Stat({ icon, label, value, sub, tone }: { icon: React.ReactNode; label: string; value: string | null; sub: string; tone: string }) {
  return (
    <div className="card flex items-center gap-3 p-4">
      <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl ${TONES[tone]}`}>{icon}</span>
      <div className="min-w-0">
        <div className="text-xs font-medium text-muted">{label}</div>
        {value === null ? <Skeleton className="mt-1 h-6 w-12" /> : <div className="text-2xl font-bold tabular-nums leading-tight">{value}</div>}
        <div className="truncate text-xs text-subtle">{sub}</div>
      </div>
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
