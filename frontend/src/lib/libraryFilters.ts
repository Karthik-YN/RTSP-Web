// Library search, filters and sort (client side: the whole list is already loaded and polled).
import type { OutputConfig, Video } from "../types/api";

export type SortKey = "name" | "newest" | "size" | "duration";
export type ShapeFilter = "" | "16:9" | "other";
/** "" = all videos, "none" = in no analytics, otherwise an analytics id. */
export type AnalyticsFilter = "" | "none" | `${number}`;

export interface LibraryFilters {
  q: string;
  sort: SortKey;
  shape: ShapeFilter;
  /** Only videos with notes (needs transcoding, long keyframe gap, VFR, …). */
  attention: boolean;
  /** "" = anyone; "-" = added on the server; otherwise a username. */
  uploader: string;
  analytics: AnalyticsFilter;
  /** Only videos whose name does not follow WHAT_WHERE_NN (to rename them). */
  naming?: boolean;
}

export const NO_FILTERS: LibraryFilters = { q: "", sort: "name", shape: "", attention: false, uploader: "", analytics: "", naming: false };

export const SORT_LABEL: Record<SortKey, string> = {
  name: "Name", newest: "Newest", size: "Largest", duration: "Longest",
};

export function filtersActive(f: LibraryFilters): boolean {
  return !!(f.q.trim() || f.shape || f.attention || f.uploader || f.analytics || f.naming);
}

/** Things worth knowing before streaming this video; shown as one "N notes" chip. */
/** What makes this video behave unlike a real camera for the edges (empty: camera-like).
 *  Short label for the chip, and the full sentence for its tooltip. */
export function videoProblems(v: Video): { short: string; text: string }[] {
  const p: { short: string; text: string }[] = [];
  if (v.compat_mode === "transcode") {
    p.push({ short: "Live transcoding", text: "Sent with live transcoding (costs a CPU core per stream)" +
      (v.compat_reasons.length ? `: ${v.compat_reasons.join("; ")}` : ".") });
  }
  const k = v.keyframe_interval_s;
  if (k != null && Number.isFinite(k) && k > 4) { // same limit as the server; real cameras: 1-2 s
    const n = String(Number(k.toFixed(1)));
    p.push({ short: `Keyframe every ${n} s`, text: `Keyframe every ${n} s: edges wait up to ${n} s for the first picture after connecting.` });
  }
  if (v.b_frames) p.push({ short: "B-frames", text: "Has B-frames (real cameras don't): some edges show frames out of order." });
  return p;
}

export function videoNotes(v: Video): string[] {
  const n: string[] = [];
  if (v.compat_mode === "transcode") n.push("Needs live transcoding" + (v.compat_reasons.length ? `: ${v.compat_reasons.join("; ")}` : ""));
  if (v.keyframe_interval_s && v.keyframe_interval_s > 4) n.push(`Keyframes only every ${v.keyframe_interval_s.toFixed(1)} s: readers wait that long to start`);
  if (v.is_vfr) n.push("Variable frame rate");
  if (v.audio_codec) n.push("Has audio: dropped when streaming");
  if (v.browser_playable === false) n.push(v.browser_note || "Browsers can't play it; VLC can");
  for (const w of v.compat_warnings) if (!n.includes(w)) n.push(w);
  return n;
}

/** Who added the video, as shown: "admin" for administrators (and anything from before login existed),
 * otherwise the user name. The server decides; this only falls back for older servers. */
export function uploaderName(v: Video): string {
  if (v.uploader) return v.uploader;
  return !v.uploaded_by || v.uploaded_by === "anonymous" ? "admin" : v.uploaded_by;
}

export function uploaderKey(v: Video): string {
  return uploaderName(v);
}

function inAnalytics(v: Video, f: AnalyticsFilter): boolean {
  if (!f) return true;
  if (f === "none") return !v.analytics?.length;
  const id = Number(f);
  return !!v.analytics?.some((a) => a.id === id);
}

const newest = (v: Video) => Date.parse(v.uploaded_at ?? v.updated_at ?? "") || 0;

export function applyLibraryFilters(videos: Video[], f: LibraryFilters, _out?: OutputConfig): Video[] {
  const q = f.q.trim().toLowerCase();
  const list = videos.filter((v) =>
    (!q || v.rel_path.toLowerCase().includes(q) || uploaderName(v).toLowerCase().includes(q)
      || (v.analytics ?? []).some((a) => a.name.toLowerCase().includes(q))
      || [v.original_name ?? "", ...(v.previous_names ?? [])].some((n) => n.toLowerCase().includes(q)))
    && (!f.naming || v.name_parts === null)
    && (!f.shape || (f.shape === "16:9" ? v.is_16_9 !== false : v.is_16_9 === false))
    && (!f.attention || videoProblems(v).length > 0)
    && (!f.uploader || uploaderKey(v) === f.uploader)
    && inAnalytics(v, f.analytics));
  const byName = (a: Video, b: Video) => a.rel_path.localeCompare(b.rel_path, undefined, { numeric: true, sensitivity: "base" });
  const cmp: Record<SortKey, (a: Video, b: Video) => number> = {
    name: byName,
    newest: (a, b) => newest(b) - newest(a) || byName(a, b),
    size: (a, b) => b.size_bytes - a.size_bytes || byName(a, b),
    duration: (a, b) => (b.duration_s ?? 0) - (a.duration_s ?? 0) || byName(a, b),
  };
  return list.sort(cmp[f.sort]);
}

/** Everyone shown as an uploader, "admin" first. */
export function uploaders(videos: Video[]): string[] {
  return [...new Set(videos.map(uploaderKey))].sort((a, b) => (a === "admin" ? -1 : b === "admin" ? 1 : a.localeCompare(b)));
}
