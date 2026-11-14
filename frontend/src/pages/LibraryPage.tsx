import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useRig } from "../hooks/useRigData";
import { useCreateStream } from "../components/AppShell";
import { VideoLibrary } from "../components/VideoLibrary";
import { AnalyticsFilterSelect, filterByAnalytics, SuggestionsDialog, type AnalyticsFilter } from "../components/LibraryAnalytics";
import { useToast } from "../components/common";
import { TagIcon } from "../components/icons";
import { api } from "../services/api";
import type { AnalyticsSuggestion } from "../types/api";

export function LibraryPage() {
  const rig = useRig();
  const toast = useToast();
  const create = useCreateStream();
  const [search, setSearch] = useState("");
  // ?analytics=<id>|none keeps the filter in the URL (links from analytics pages, reloads)
  const [params, setParams] = useSearchParams();
  const filter = (params.get("analytics") ?? "") as AnalyticsFilter;
  const setFilter = (f: AnalyticsFilter) => setParams(f ? { analytics: f } : {}, { replace: true });
  const q = search.trim().toLowerCase();
  const all = rig.videos;
  const videos = useMemo(
    () => filterByAnalytics((all ?? []).filter((v) => !q || v.rel_path.toLowerCase().includes(q)), filter),
    [all, q, filter]);

  // name-based suggestions, re-checked whenever videos or their analytics change
  const [suggestions, setSuggestions] = useState<AnalyticsSuggestion[]>([]);
  const [reviewing, setReviewing] = useState(false);
  const canWrite = !!rig.user?.can_write;
  const sig = (all ?? []).map((v) => `${v.id}:${v.analytics?.length ?? 0}`).join(",");
  useEffect(() => {
    if (!canWrite) return;
    let live = true;
    api.analyticsSuggestions().then((s) => live && setSuggestions(s)).catch(() => {});
    return () => { live = false; };
  }, [sig, canWrite]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-bold tracking-tight">Video library</h1>
          <p className="text-sm text-muted">Recordings available to publish. Upload here or copy files into the server's video folder.</p>
        </div>
        <AnalyticsFilterSelect value={filter} onChange={setFilter} tree={rig.analytics} />
        {canWrite && suggestions.length > 0 && (
          <button className="btn-secondary" onClick={() => setReviewing(true)}
            title="Videos whose names match an analytics they are not in yet">
            <TagIcon /> Suggested analytics ({suggestions.length})
          </button>
        )}
      </div>
      <VideoLibrary videos={videos} loading={rig.videosLoading} user={rig.user} onChanged={rig.refreshVideos}
        onCreateStream={create} search={search} setSearch={setSearch} />
      {reviewing && (
        <SuggestionsDialog items={suggestions} onClose={() => setReviewing(false)}
          onDone={(n) => {
            setReviewing(false);
            toast("ok", `${n} video${n === 1 ? "" : "s"} added to analytics.`);
            rig.refreshVideos();
            rig.refreshAnalytics();
          }} />
      )}
    </div>
  );
}
