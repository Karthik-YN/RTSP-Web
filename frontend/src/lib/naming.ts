/**
 * Video naming convention WHAT_WHERE_NN[_tag].ext (mirror of backend/app/services/naming.py;
 * the server checks again on upload and rename).
 *
 *   ALPR_toll-s1_01.mp4   ->  ALPR · toll-s1 · #01
 */

export const TAGS = ["720p", "1080p", "16x9", "h264", "4k", "web", "night"] as const;
export const WHERE_MAX = 14;
export const MISC = "MISC";
const CODE = /^[A-Z][A-Z0-9]{1,7}$/;
const WORD = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const TAG = /^(720p|1080p|16x9|h264|4k|web|night|\d{1,3}fps)$/;

export interface NameParts { what: string; where: string; nn: number; tags: string[] }

export function splitName(name: string): { stem: string; ext: string } {
  const i = name.lastIndexOf(".");
  return i > 0 ? { stem: name.slice(0, i), ext: name.slice(i).toLowerCase() } : { stem: name, ext: "" };
}

/** Reasons a name does not follow the convention (empty = fine). */
export function nameErrors(name: string, codes?: Set<string>): string[] {
  const { stem, ext } = splitName(name.trim());
  const errs: string[] = [];
  if (!ext) errs.push("Add the file extension (for example .mp4).");
  if (/[^A-Za-z0-9_.-]/.test(stem)) errs.push("Use only letters, digits, '-' and '_' (no spaces or brackets).");
  const parts = stem.split("_");
  if (parts.length < 3) {
    errs.push("Use three parts: WHAT_WHERE_NN, for example ALPR_toll-s1_01.");
    return errs;
  }
  const [what, where, nn, ...tags] = parts;
  if (!CODE.test(what)) errs.push(`WHAT '${what}' must be an UPPERCASE analytics code such as ALPR or FALL.`);
  else if (codes && !codes.has(what)) errs.push(`WHAT '${what}' is not an analytics code.`);
  if (!WORD.test(where)) errs.push(`WHERE '${where}' must be lowercase letters/digits, words joined by '-'.`);
  else if (where.length > WHERE_MAX) errs.push(`WHERE '${where}' is longer than ${WHERE_MAX} characters.`);
  if (!/^\d{2}$/.test(nn) || nn === "00") errs.push(`NN '${nn}' must be a two-digit clip number 01-99.`);
  const bad = tags.filter((t) => !TAG.test(t));
  if (bad.length) errs.push(`Unknown tag(s) ${bad.join(", ")}. Allowed: ${TAGS.join(", ")}, <n>fps.`);
  return errs;
}

export function parseName(name: string): NameParts | null {
  if (nameErrors(name).length) return null;
  const [what, where, nn, ...tags] = splitName(name).stem.split("_");
  return { what, where, nn: Number(nn), tags };
}

/** Free text -> WHERE: "Toll Plaza S1" -> "toll-plaza-s1". */
export function slug(text: string, limit = WHERE_MAX): string {
  let t = text.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  if (t.length > limit) {
    // cut at a word boundary: "tirupathi-bench" -> "tirupathi", not "tirupathi-benc"
    const cut = t.slice(0, limit);
    t = t[limit] === "-" || !cut.includes("-") ? cut : cut.slice(0, cut.lastIndexOf("-"));
    t = t.replace(/-+$/, "");
  }
  return t;
}

/** Next free clip number for WHAT + WHERE among ``names`` (library + names picked in this batch). */
export function nextNumber(what: string, where: string, names: string[]): number {
  const used = new Set(names.map(parseName).filter((p): p is NameParts => !!p && p.what === what && p.where === where)
    .map((p) => p.nn));
  let n = 1;
  while (used.has(n)) n++;
  return n;
}

export function buildName(what: string, where: string, nn: number, tags: string[], ext: string): string {
  const e = ext.startsWith(".") ? ext : `.${ext}`;
  return [what, where, String(nn).padStart(2, "0"), ...tags].join("_") + e.toLowerCase();
}

/** What a file's current name already tells: an analytics code, tags and a place, for prefilling. */
export function guessParts(original: string, codes: string[], hintWhat?: string): { what: string; where: string; tags: string[] } {
  const parsed = parseName(original);
  if (parsed) return { what: parsed.what, where: parsed.where, tags: parsed.tags };
  const words = splitName(original).stem.split(/[^A-Za-z0-9]+/).filter(Boolean);
  const upper = new Set(words.map((w) => w.toUpperCase()));
  const what = hintWhat ?? ([...codes].sort((a, b) => b.length - a.length).find((c) => c !== MISC && upper.has(c)) ?? "");
  const lower = words.map((w) => w.toLowerCase());
  const tags = TAGS.filter((t) => lower.includes(t));
  const rest = words.filter((w) => w.toUpperCase() !== what && !(TAGS as readonly string[]).includes(w.toLowerCase())
    && !/^\d+$/.test(w) && !/^(copy|of|final|test|clip|clipped|video|new|vidssave|com)$/i.test(w));
  return { what, where: slug(rest.slice(0, 2).join("-")), tags: [...tags] };
}
