import { createContext, useContext, type ReactNode } from "react";
import { api, ApiError } from "../services/api";
import type { AppConfig, MediaMTXStatus, Stream, SystemInfo, User, Video } from "../types/api";
import { usePolling } from "./usePolling";

interface RigData {
  user: User;
  config: AppConfig;
  streams: Stream[] | null;
  system: SystemInfo | null;
  mediamtx: MediaMTXStatus | null;
  videos: Video[] | null;
  streamsLoading: boolean;
  videosLoading: boolean;
  appError: ApiError | null;
  refreshStreams: () => void;
  refreshVideos: () => void;
  refreshAll: () => void;
}

const Ctx = createContext<RigData | null>(null);

/** Polls the live server state once for the whole app (pauses in hidden tabs). */
export function RigDataProvider({ user, config, children }: { user: User; config: AppConfig; children: ReactNode }) {
  const streams = usePolling(api.streams, 2000);
  const system = usePolling(api.system, 2000);
  const mediamtx = usePolling(api.mediamtx, 2000);
  const videos = usePolling(() => api.videos(), 15000);
  const value: RigData = {
    user, config,
    streams: streams.data, system: system.data, mediamtx: mediamtx.data, videos: videos.data,
    streamsLoading: streams.loading, videosLoading: videos.loading,
    appError: streams.error && streams.error.status === 0 ? streams.error : null,
    refreshStreams: streams.refresh, refreshVideos: videos.refresh,
    refreshAll: () => { streams.refresh(); system.refresh(); mediamtx.refresh(); },
  };
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRig(): RigData {
  const v = useContext(Ctx);
  if (!v) throw new Error("useRig outside RigDataProvider");
  return v;
}
