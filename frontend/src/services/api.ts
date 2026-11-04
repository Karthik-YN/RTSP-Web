import type {
  AppConfig, BulkResult, StreamGroup, StreamHistory, AppEvent, MediaMTXStatus, Stream, StreamDetail, SystemInfo, UploadCheck, User,
  ValidateResult, Video,
} from "../types/api";

/** Error carrying the server's message (FastAPI `detail`) and extra fields. */
export class ApiError extends Error {
  status: number;
  data: Record<string, unknown>;
  constructor(status: number, message: string, data: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

export function errorMessage(body: unknown, status: number): { message: string; data: Record<string, unknown> } {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return { message: d, data: body as Record<string, unknown> };
    if (d && typeof d === "object" && "message" in d) {
      return { message: String((d as { message: unknown }).message), data: d as Record<string, unknown> };
    }
  }
  if (status === 0) return { message: "Cannot reach the server. Check the network connection.", data: {} };
  return { message: `Request failed (HTTP ${status}).`, data: {} };
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function request<T>(method: string, url: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: { ...(body !== undefined ? { "Content-Type": "application/json" } : {}), ...headers },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Cannot reach the server. Check the network connection.");
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let data: unknown = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    if (res.status === 401 && onUnauthorized && !url.endsWith("/auth/login")) onUnauthorized();
    const { message, data: extra } = errorMessage(data, res.status);
    throw new ApiError(res.status, message, extra);
  }
  return data as T;
}

const ifMatch = (version?: number): Record<string, string> =>
  version !== undefined ? { "If-Match": String(version) } : {};

export interface StreamInput {
  name: string;
  video_id: number;
  start: string | null;
  end: string | null;
  mode: "loop" | "once";
  force_transcode: boolean;
  start_now?: boolean;
  note?: string | null;
  protected?: boolean;
}

export interface StreamMetaInput {
  note?: string;
  protected?: boolean;
  owner?: string;
}

export const api = {
  me: () => request<User>("GET", "/api/auth/me"),
  login: (username: string, password: string) => request<User>("POST", "/api/auth/login", { username, password }),
  logout: () => request<void>("POST", "/api/auth/logout"),
  changePassword: (current_password: string, new_password: string) =>
    request<void>("POST", "/api/auth/password", { current_password, new_password }),
  users: () => request<User[]>("GET", "/api/users"),
  createUser: (username: string, role: User["role"], password: string) =>
    request<User>("POST", "/api/users", { username, role, password }),
  updateUser: (username: string, body: { role?: User["role"]; password?: string }) =>
    request<User>("PATCH", `/api/users/${encodeURIComponent(username)}`, body),
  deleteUser: (username: string) => request<void>("DELETE", `/api/users/${encodeURIComponent(username)}`),

  config: () => request<AppConfig>("GET", "/api/config"),
  system: () => request<SystemInfo>("GET", "/api/system"),
  mediamtx: () => request<MediaMTXStatus>("GET", "/api/mediamtx/status"),
  mediamtxStart: () => request<{ message: string }>("POST", "/api/mediamtx/start"),
  mediamtxRestart: () => request<{ message: string }>("POST", "/api/mediamtx/restart"),
  events: (limit = 100) => request<AppEvent[]>("GET", `/api/events?limit=${limit}`),

  videos: (search?: string) =>
    request<Video[]>("GET", `/api/videos${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  refreshVideos: () => request<{ added: number; updated: number; missing: number; settling: string[] }>(
    "POST", "/api/videos/refresh"),
  uploadCheck: (filename: string, size: number) =>
    request<UploadCheck>("POST", "/api/videos/upload/check", { filename, size }),

  streams: () => request<Stream[]>("GET", "/api/streams"),
  stream: (id: number) => request<StreamDetail>("GET", `/api/streams/${id}`),
  logs: (id: number, lines: number) => request<{ stream_id: number; lines: string[] }>("GET", `/api/streams/${id}/logs?lines=${lines}`),
  validate: (body: StreamInput) => request<ValidateResult>("POST", "/api/streams/validate", body),
  create: (body: StreamInput) => request<Stream>("POST", "/api/streams", body),
  update: (id: number, version: number, body: Partial<StreamInput> & StreamMetaInput & { clear_end?: boolean }) =>
    request<Stream>("PATCH", `/api/streams/${id}`, body, ifMatch(version)),
  action: (id: number, action: "start" | "stop" | "restart", version?: number) =>
    request<Stream>("POST", `/api/streams/${id}/${action}`, undefined, ifMatch(version)),
  remove: (id: number, version?: number) => request<void>("DELETE", `/api/streams/${id}`, undefined, ifMatch(version)),
  groups: () => request<StreamGroup[]>("GET", "/api/groups"),
  createGroup: (name: string, stream_ids: number[]) => request<StreamGroup>("POST", "/api/groups", { name, stream_ids }),
  setGroup: (stream_ids: number[], group_id: number | null) =>
    request<{ updated: number }>("POST", "/api/groups/members", { stream_ids, group_id }),
  deleteGroup: (id: number) => request<void>("DELETE", `/api/groups/${id}`),
  groupAction: (id: number, action: "start" | "stop" | "restart") => request<BulkResult>("POST", `/api/groups/${id}/${action}`),
  groupUrls: async (id: number) => {
    const r = await fetch(`/api/groups/${id}/urls`, { credentials: "same-origin" });
    if (!r.ok) throw new ApiError(r.status, "Could not load the group's URLs.");
    return r.text();
  },
  bulk: (stream_ids: number[], action: "start" | "stop" | "restart" | "delete") =>
    request<BulkResult>("POST", "/api/streams/bulk", { stream_ids, action }),
  deleteVideo: (id: number) => request<{ message: string }>("DELETE", `/api/videos/${id}`),
  history: (id: number, hours = 24) => request<StreamHistory>("GET", `/api/streams/${id}/history?hours=${hours}`),
};

const CHUNK = 8 * 1024 * 1024;

function putChunk(sessionId: string, offset: number, blob: Blob, onProgress: (sent: number) => void,
  register: (x: XMLHttpRequest) => void): Promise<{ status: number; body: unknown }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    register(xhr);
    xhr.open("PUT", `/api/videos/uploads/${sessionId}?offset=${offset}`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = (e) => onProgress(e.loaded);
    xhr.onload = () => {
      let body: unknown = null;
      try { body = JSON.parse(xhr.responseText); } catch { /* empty */ }
      resolve({ status: xhr.status, body });
    };
    xhr.onerror = () => reject(new ApiError(0, "network"));
    xhr.onabort = () => reject(new ApiError(-1, "Upload cancelled."));
    xhr.send(blob);
  });
}

/**
 * Resumable upload: the server reserves disk space first, then the file is sent
 * in 8 MB chunks. If the Wi-Fi drops, the client asks the server how much it
 * has and continues from there (retrying with backoff for about two minutes).
 */
export function uploadVideo(
  file: File,
  onProgress: (loaded: number, total: number, note?: string) => void,
): { promise: Promise<Video>; abort: () => void } {
  let current: XMLHttpRequest | null = null;
  let aborted = false;
  let sessionId: string | null = null;
  const promise = (async () => {
    const session = await request<{ id: string; offset: number }>("POST", "/api/videos/uploads",
      { filename: file.name, size: file.size });
    sessionId = session.id;
    let offset = session.offset;
    let failures = 0;
    while (offset < file.size) {
      if (aborted) throw new ApiError(-1, "Upload cancelled.");
      const blob = file.slice(offset, Math.min(file.size, offset + CHUNK));
      try {
        const r = await putChunk(session.id, offset, blob, (sent) => onProgress(offset + sent, file.size),
          (x) => { current = x; });
        if (r.status === 201) return r.body as Video;
        if (r.status === 200) { offset = (r.body as { offset: number }).offset; failures = 0; continue; }
        if (r.status === 409 && r.body && typeof r.body === "object") {  // server has a different offset: resync
          const d = (r.body as { detail?: { offset?: number } }).detail;
          if (d?.offset !== undefined) { offset = d.offset; continue; }
        }
        const { message, data } = errorMessage(r.body, r.status);
        throw new ApiError(r.status, message, data);
      } catch (e) {
        const err = e as ApiError;
        if (err.status !== 0 || aborted) throw err;
        failures += 1;
        if (failures > 12) throw new ApiError(0, "Upload failed: the connection kept dropping. Try again later.");
        const wait = Math.min(15000, 1000 * 2 ** (failures - 1));
        onProgress(offset, file.size, `connection lost, retrying in ${Math.round(wait / 1000)} s…`);
        await new Promise((res) => setTimeout(res, wait));
        try {  // ask the server how much arrived before the drop
          offset = (await request<{ offset: number }>("GET", `/api/videos/uploads/${session.id}`)).offset;
        } catch { /* still offline: retry */ }
      }
    }
    throw new ApiError(500, "Upload ended unexpectedly.");
  })();
  return {
    promise,
    abort: () => {
      aborted = true;
      current?.abort();
      if (sessionId) request("DELETE", `/api/videos/uploads/${sessionId}`).catch(() => {});
    },
  };
}
