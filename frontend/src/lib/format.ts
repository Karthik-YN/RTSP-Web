// Formatting and client-side validation helpers. Validation here is only for
// instant feedback; the backend re-validates everything authoritatively.

export function formatBytes(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return "–";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (Math.abs(v) >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : v >= 100 ? 0 : 1)} ${units[i]}`;
}

export function formatBitrate(bps: number | null | undefined): string {
  if (!bps) return "–";
  if (bps >= 1e6) return `${(bps / 1e6).toFixed(1)} Mbit/s`;
  return `${(bps / 1e3).toFixed(0)} kbit/s`;
}

/** Seconds -> HH:MM:SS(.mmm) */
export function formatTime(seconds: number | null | undefined, withMs = true): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "–";
  const totalMs = Math.round(seconds * 1000);
  const h = Math.floor(totalMs / 3_600_000);
  const m = Math.floor((totalMs % 3_600_000) / 60_000);
  const s = Math.floor((totalMs % 60_000) / 1000);
  const ms = totalMs % 1000;
  const base = `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return withMs && ms ? `${base}.${String(ms).padStart(3, "0")}` : base;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "–";
  const s = Math.max(0, Math.floor(seconds));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

export function since(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "–";
  return formatDuration((now - new Date(iso).getTime()) / 1000);
}

export interface ParsedTime {
  seconds: number | null; // null = empty
  error: string | null;
}

const TS_RE = /^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$/;

/** Parse HH:MM:SS(.mmm), MM:SS or seconds (same rules as the backend). */
export function parseTime(text: string): ParsedTime {
  const t = text.trim();
  if (t === "") return { seconds: null, error: null };
  if (t.startsWith("-")) return { seconds: null, error: "Time cannot be negative." };
  const m = TS_RE.exec(t);
  if (!m) return { seconds: null, error: "Use HH:MM:SS, MM:SS or seconds." };
  const parts = [m[1], m[2], m[3]].filter((p) => p !== undefined).map(Number);
  let h = 0, mi = 0, s = 0;
  if (parts.length === 3) [h, mi, s] = parts;
  else if (parts.length === 2) [mi, s] = parts;
  else [s] = parts;
  if (parts.length >= 2 && (s >= 60 || (parts.length === 3 && mi >= 60))) {
    return { seconds: null, error: "Minutes and seconds must be below 60." };
  }
  return { seconds: h * 3600 + mi * 60 + s, error: null };
}

export function validateName(name: string, pattern: string, reserved: string[], existing: string[]): string | null {
  if (!name) return "Stream name is required.";
  if (/\s/.test(name)) return "No spaces allowed.";
  if (name.length > 64) return "At most 64 characters.";
  if (!new RegExp(pattern).test(name)) return "Only letters, digits, '_' and '-' (no slashes or special characters).";
  if (reserved.map((r) => r.toLowerCase()).includes(name.toLowerCase())) return `'${name}' is reserved.`;
  if (existing.map((e) => e.toLowerCase()).includes(name.toLowerCase())) return `'${name}' is already in use.`;
  return null;
}

/** Suggest a stream name from a file name. */
export function suggestName(filename: string, existing: string[]): string {
  const base = filename.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "")
    .slice(0, 56) || "stream";
  const lower = existing.map((e) => e.toLowerCase());
  if (!lower.includes(base.toLowerCase())) return base;
  for (let i = 2; i < 1000; i++) {
    const c = `${base}_${i}`;
    if (!lower.includes(c.toLowerCase())) return c;
  }
  return base;
}
