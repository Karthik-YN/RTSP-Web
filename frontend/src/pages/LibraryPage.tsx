import { useState } from "react";
import { useRig } from "../hooks/useRigData";
import { useCreateStream } from "../components/AppShell";
import { VideoLibrary } from "../components/VideoLibrary";

export function LibraryPage() {
  const rig = useRig();
  const create = useCreateStream();
  const [search, setSearch] = useState("");
  const q = search.trim().toLowerCase();
  const videos = (rig.videos ?? []).filter((v) => !q || v.rel_path.toLowerCase().includes(q));
  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Video library</h1>
        <p className="text-sm text-muted">Recordings available to publish. Upload here or copy files into the server's video folder.</p>
      </div>
      <VideoLibrary videos={videos} loading={rig.videosLoading} user={rig.user} onChanged={rig.refreshVideos}
        onCreateStream={(id) => create(id)} search={search} setSearch={setSearch} />
    </div>
  );
}
