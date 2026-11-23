import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, ApiError } from "../services/api";
import { useRig } from "../hooks/useRigData";
import { buildName, guessParts, MISC, nameErrors, nextNumber, slug, splitName, TAGS, WHERE_MAX } from "../lib/naming";
import type { NamingConvention, Video } from "../types/api";
import { ErrorBox, Modal, Spinner, useToast, WarnBox } from "./common";

// ------------------------------------------------------------------ convention (fetched once, shared)
let cache: NamingConvention | null = null;
let inflight: Promise<void> | null = null;
/** The server has no naming API (a backend older than the naming convention): the dashboard skips naming. */
let unavailable = false;
const subscribers = new Set<() => void>();

// The agreed convention, built in, so the guide can be shown before the server has the naming API.
const DEFAULT_CODES: Record<string, string> = {
  "Charging station": "EV", videoANPR: "ANPR", TrafficCongestion: "JAM", VehicleALPR: "ALPR", VehicleDwell: "DWELL",
  "Trigger line videos": "TRIG", "Camera Tampering": "TAMPER", SlipAndFall: "FALL", CrowdDensity: "CROWD",
  RegionCount: "REGION", LineCount: "LINE", PPE: "PPE", AbandonedObject: "ABANDON", DigitalFence: "FENCE",
  "Human Attributes": "ATTR", Elephant: "ELEPHANT", FireSmoke: "FIRE", Parking: "PARK", RLVD: "RLVD",
};
const EXAMPLES = [
  { name: "ALPR_toll-s1_01.mp4", reads: "ALPR · toll plaza camera s1 · clip 1" },
  { name: "FALL_office_01.mp4", reads: "slip-and-fall · office · clip 1" },
  { name: "FIRE_forest_01.mp4", reads: "fire/smoke · forest · clip 1" },
  { name: "JAM_dhaka_01_web.mp4", reads: "traffic jam · Dhaka · clip 1 · downloaded from the internet" },
  { name: "CROWD_railway_01_h264.mp4", reads: "crowd · railway station · clip 1 · H.264 copy" },
];
let builtIn: NamingConvention | null = null;
function builtInConvention(): Promise<NamingConvention> {
  return api.analyticsTree().catch(() => []).then((tree) => {
    const codes: NamingConvention["codes"] = [];
    const seen = new Set<string>();
    for (const cat of tree) for (const a of cat.analytics) {
      const code = a.code ?? DEFAULT_CODES[a.name];
      if (code && !seen.has(code)) { seen.add(code); codes.push({ code, analytics_id: a.id, analytics: a.name, category: cat.name }); }
    }
    for (const [name, code] of Object.entries(DEFAULT_CODES)) {
      if (!seen.has(code)) { seen.add(code); codes.push({ code, analytics_id: null, analytics: name, category: "Other" }); }
    }
    codes.push({ code: MISC, analytics_id: null, analytics: "not decided yet", category: "Other" });
    return { format: "WHAT_WHERE_NN[_tag].ext", pattern: "", where_max: WHERE_MAX, tags: [...TAGS, "<n>fps"],
      mode: "off", codes, places: [], examples: EXAMPLES };
  });
}

export function refreshNaming(): Promise<void> {
  inflight ??= api.naming()
    .then((c) => { if (Array.isArray(c?.codes)) { cache = c; unavailable = false; } })
    .catch(async (e: ApiError) => {
      if (e?.status === 404 || e?.status === 405) { unavailable = true; builtIn ??= await builtInConvention(); }
    })
    .finally(() => { inflight = null; subscribers.forEach((f) => f()); });
  return inflight;
}

/** Upload naming check in effect: the server's setting, "off" when the server has no naming API. */
export function namingMode(): "enforce" | "warn" | "off" {
  return cache?.mode ?? (unavailable ? "off" : "enforce");
}

/** The naming convention from the server (codes, known places, mode); null until loaded. */
export function useNaming(): NamingConvention | null {
  return useNamingRaw() ?? (unavailable ? builtIn : null);
}
/** Only what the server says (null on an older backend): upload naming, renames. */
function useNamingRaw(): NamingConvention | null {
  const [, tick] = useState(0);
  useEffect(() => {
    const f = () => tick((x) => x + 1);
    subscribers.add(f);
    if (!cache && !inflight) refreshNaming();
    return () => { subscribers.delete(f); };
  }, []);
  return cache;
}

// one colour per WHAT code, so the same analytics always looks the same in every list
const TONES = [
  "bg-blue-500/10 text-blue-700 ring-blue-500/30 dark:text-blue-300",
  "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300",
  "bg-violet-500/10 text-violet-700 ring-violet-500/30 dark:text-violet-300",
  "bg-amber-500/10 text-amber-800 ring-amber-500/30 dark:text-amber-300",
  "bg-rose-500/10 text-rose-700 ring-rose-500/30 dark:text-rose-300",
  "bg-cyan-500/10 text-cyan-700 ring-cyan-500/30 dark:text-cyan-300",
  "bg-lime-500/10 text-lime-800 ring-lime-500/30 dark:text-lime-300",
  "bg-fuchsia-500/10 text-fuchsia-700 ring-fuchsia-500/30 dark:text-fuchsia-300",
];
export function codeTone(code: string): string {
  if (code === MISC) return "bg-slate-500/10 text-muted ring-slate-500/30";
  let h = 0;
  for (const ch of code) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return TONES[h % TONES.length];
}

/** "ALPR · toll-s1 · #01 · 720p" as chips, or a "Name not standard" flag with a Rename button. */
export function NameChips({ v, onRename }: { v: Video; onRename?: () => void }) {
  const naming = useNaming();
  const p = v.name_parts;
  if (p === undefined) return null; // older server: it does not check names yet
  if (!p) {
    return (
      <span className="inline-flex flex-wrap items-center gap-1 text-[11px]">
        <span className="rounded px-1.5 py-0.5 font-medium ring-1 ring-inset bg-amber-500/10 text-amber-800 ring-amber-500/30 dark:text-amber-300"
          title="Not named WHAT_WHERE_NN (see Naming guide)">Name not standard</span>
        {onRename && <button className="rounded px-1.5 py-0.5 font-medium text-brand hover:underline" onClick={onRename}>Rename</button>}
      </span>
    );
  }
  const analytics = naming?.codes.find((c) => c.code === p.what)?.analytics;
  return (
    <span className="inline-flex flex-wrap items-center gap-1 text-[11px]" data-testid="name-chips">
      <span className={`rounded px-1.5 py-0.5 font-bold tracking-wide ring-1 ring-inset ${codeTone(p.what)}`}
        title={analytics ? `Analytics: ${analytics}` : "Analytics code"}>{p.what}</span>
      <span className="rounded bg-surface-2 px-1.5 py-0.5 font-medium ring-1 ring-inset ring-line" title="Where it was recorded">{p.where}</span>
      <span className="rounded bg-surface-2 px-1.5 py-0.5 tabular-nums text-muted ring-1 ring-inset ring-line" title="Clip number">#{String(p.nn).padStart(2, "0")}</span>
      {p.tags.map((t) => <span key={t} className="rounded px-1.5 py-0.5 text-muted ring-1 ring-inset ring-line">{t}</span>)}
      {onRename && <button className="px-1 text-subtle hover:text-brand" onClick={onRename} title="Rename" aria-label={`Rename ${v.filename}`}>✎</button>}
    </span>
  );
}

// ------------------------------------------------------------------ the editor: WHAT · WHERE · tags
export interface Draft { what: string; where: string; tags: string[] }

function CodeSelect({ value, onChange, id }: { value: string; onChange: (v: string) => void; id?: string }) {
  const naming = useNaming();
  const groups = useMemo(() => {
    const m = new Map<string, NamingConvention["codes"]>();
    for (const c of naming?.codes ?? []) {
      const k = c.category ?? "Other";
      m.set(k, [...(m.get(k) ?? []), c]);
    }
    return [...m.entries()];
  }, [naming]);
  return (
    <select id={id} className={`input font-mono ${value ? "" : "text-subtle"}`} value={value} aria-label="WHAT (analytics)"
      onChange={(e) => onChange(e.target.value)}>
      <option value="">WHAT…</option>
      {groups.map(([cat, codes]) => (
        <optgroup key={cat} label={cat}>
          {codes.map((c) => <option key={c.code} value={c.code}>{c.code} — {c.analytics}</option>)}
        </optgroup>
      ))}
    </select>
  );
}

export function DraftEditor({ d, onChange, compact = false }: { d: Draft; onChange: (d: Draft) => void; compact?: boolean }) {
  const naming = useNaming();
  const listId = useRef(`places-${Math.random().toString(36).slice(2)}`).current;
  const places = (naming?.places ?? []).slice().sort((a, b) =>
    Number(b.codes.includes(d.what)) - Number(a.codes.includes(d.what)) || b.videos - a.videos);
  return (
    <div className={`grid gap-2 ${compact ? "sm:grid-cols-[13rem_10rem_1fr]" : "sm:grid-cols-[11rem_1fr]"}`}>
      <CodeSelect value={d.what} onChange={(what) => onChange({ ...d, what })} />
      <div>
        <input className="input font-mono" list={listId} placeholder="WHERE, e.g. toll-s1" aria-label="WHERE (site or camera)"
          value={d.where} maxLength={40} onChange={(e) => onChange({ ...d, where: e.target.value })}
          onBlur={(e) => onChange({ ...d, where: slug(e.target.value) })} />
        <datalist id={listId}>{places.map((p) => <option key={p.where} value={p.where}>{p.codes.join(", ")}</option>)}</datalist>
      </div>
      <div className={`flex flex-wrap items-center gap-1 ${compact ? "" : "sm:col-span-2"}`} aria-label="Tags">
        {TAGS.map((t) => {
          const on = d.tags.includes(t);
          return (
            <button key={t} type="button" aria-pressed={on}
              onClick={() => onChange({ ...d, tags: on ? d.tags.filter((x) => x !== t) : TAGS.filter((x) => x === t || d.tags.includes(x)) })}
              className={`rounded px-1.5 py-0.5 font-mono text-[11px] ring-1 ring-inset ${on ? "bg-brand-strong text-white ring-brand-strong" : "text-muted ring-line hover:text-fg"}`}>
              {t}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Final names for drafts, numbering each WHAT + WHERE after what the library (and earlier rows) already use. */
export function finalNames(drafts: Draft[], exts: string[], taken: string[]): { name: string | null; errors: string[] }[] {
  const reserved = [...taken];
  return drafts.map((d, i) => {
    const where = slug(d.where);
    if (!d.what) return { name: null, errors: ["Pick WHAT (the analytics)."] };
    if (!where) return { name: null, errors: ["Type WHERE (site or camera)."] };
    const name = buildName(d.what, where, nextNumber(d.what, where, reserved), d.tags, exts[i]);
    const errors = nameErrors(name);
    if (!errors.length) reserved.push(name);
    return { name: errors.length ? null : name, errors };
  });
}

// ------------------------------------------------------------------ upload: name the files first
export function NameFilesDialog({ files, hintWhat, mode, onConfirm, onCancel }: {
  files: File[]; hintWhat?: string; mode: string;
  onConfirm: (names: string[]) => void; onCancel: () => void;
}) {
  const naming = useNaming();
  const { videos } = useRig();
  const codes = (naming?.codes ?? []).map((c) => c.code);
  const [drafts, setDrafts] = useState<Draft[]>(() => files.map((f) => guessParts(f.name, codes, hintWhat)));
  // codes arrive after the first render: fill WHAT where the file name tells it
  useEffect(() => {
    if (!codes.length) return;
    setDrafts((ds) => ds.map((d, i) => (d.what ? d : { ...d, what: guessParts(files[i].name, codes, hintWhat).what })));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [codes.length]);
  const taken = (videos ?? []).map((v) => v.filename);
  const out = finalNames(drafts, files.map((f) => splitName(f.name).ext || ".mp4"), taken);
  const ok = out.every((o) => o.name);
  const set = (i: number, d: Draft) => setDrafts((ds) => ds.map((x, j) => (j === i ? d : x)));
  const [all, setAll] = useState<Draft>({ what: hintWhat ?? "", where: "", tags: [] });
  return (
    <Modal title={`Name ${files.length === 1 ? "this video" : `these ${files.length} videos`}`} onClose={onCancel} wide>
      <p className="text-sm text-muted">
        Every video is named <b className="font-mono text-fg">WHAT_WHERE_NN</b>: the analytics, where it was recorded, and a clip
        number the app picks. <NamingGuideLink /> The file's own name is kept with the video.
      </p>
      {files.length > 1 && (
        <div className="mt-3 rounded-lg bg-surface-2 p-3 ring-1 ring-inset ring-line">
          <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-subtle">Same for all</div>
          <DraftEditor d={all} compact onChange={(d) => {
            setAll(d);
            setDrafts((ds) => ds.map((x) => ({ what: d.what || x.what, where: d.where || x.where, tags: d.tags.length ? d.tags : x.tags })));
          }} />
        </div>
      )}
      <ol className="mt-3 max-h-[55vh] space-y-3 overflow-y-auto pr-1">
        {files.map((f, i) => (
          <li key={i} className="rounded-lg border border-line p-3">
            <div className="mb-2 truncate text-xs text-muted" title={f.name}>{f.name}</div>
            <DraftEditor d={drafts[i]} compact onChange={(d) => set(i, d)} />
            <div className="mt-2 text-sm">
              {out[i].name
                ? <span className="font-mono font-semibold text-emerald-700 dark:text-emerald-300" data-testid="final-name">→ {out[i].name}</span>
                : <span className="text-xs text-amber-700 dark:text-amber-300">{out[i].errors[0]}</span>}
            </div>
          </li>
        ))}
      </ol>
      <div className="mt-4 flex flex-wrap justify-end gap-2">
        <button className="btn-secondary" onClick={onCancel}>Cancel</button>
        {mode !== "enforce" && (
          <button className="btn-ghost" onClick={() => onConfirm(files.map((f) => f.name))}
            title="Allowed for now (naming check is set to warn); the library flags these names">Keep original names</button>
        )}
        <button className="btn-primary" disabled={!ok} onClick={() => onConfirm(out.map((o) => o.name!))}>
          Upload {files.length === 1 ? "" : `${files.length} videos`}
        </button>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ rename one video
export function RenameDialog({ v, hintWhat, onClose, onDone }: {
  v: Video; hintWhat?: string; onClose: () => void; onDone?: () => void;
}) {
  const naming = useNaming();
  const { videos, streams, analytics, refreshVideos, refreshStreams } = useRig();
  const toast = useToast();
  const codes = (naming?.codes ?? []).map((c) => c.code);
  // not named yet: suggest the code of the (first) analytics the video is in
  const inAnalytics = v.analytics?.[0]?.id;
  const fromTree = analytics?.flatMap((c) => c.analytics).find((a) => a.id === inAnalytics)?.code ?? undefined;
  const [d, setD] = useState<Draft>(() => guessParts(v.filename, codes, v.name_parts ? undefined : hintWhat ?? fromTree));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const ext = splitName(v.filename).ext;
  // keep this video's own number when WHAT + WHERE stay the same
  const taken = (videos ?? []).filter((x) => x.id !== v.id).map((x) => x.filename);
  const p = v.name_parts;
  const where = slug(d.where);
  const keep = !!p && p.what === d.what && p.where === where;
  const result = keep
    ? { name: buildName(d.what, where, p!.nn, d.tags, ext), errors: [] as string[] }
    : finalNames([d], [ext], taken)[0];
  const users = (streams ?? []).filter((s) => s.video_id === v.id);
  const submit = async () => {
    if (!result.name) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await api.renameVideo(v.id, result.name);
      toast("ok", `Renamed to ${result.name}${r.streams.length ? ` — ${r.streams.length} stream(s) kept running` : ""}.`);
      refreshVideos();
      refreshStreams();
      refreshNaming();
      onDone?.();
      onClose();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Rename video" onClose={onClose}>
      <div className="space-y-3 text-sm">
        <div className="text-muted">Now: <span className="break-all font-mono text-fg">{v.filename}</span></div>
        <DraftEditor d={d} onChange={setD} />
        <div>New name:{" "}
          {result.name
            ? <span className="font-mono font-semibold text-emerald-700 dark:text-emerald-300" data-testid="final-name">{result.name}</span>
            : <span className="text-amber-700 dark:text-amber-300">{result.errors[0]}</span>}
        </div>
        {users.length > 0 && (
          <WarnBox>
            {users.length} stream{users.length === 1 ? "" : "s"} use{users.length === 1 ? "s" : ""} this video ({users.map((s) => s.name).join(", ")}).
            They keep running, and their RTSP URLs don't change.
          </WarnBox>
        )}
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={!result.name || busy || result.name === v.filename} onClick={submit}>
            {busy && <Spinner />} Rename
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ the guide (button in the top bar)
let openGuide: (() => void) | null = null;

function NamingGuideLink() {
  return <button type="button" className="text-brand hover:underline" onClick={() => openGuide?.()}>Naming guide</button>;
}

export function NamingGuideButton({ className = "" }: { className?: string }) {
  const [open, setOpen] = useState(false);
  useNaming();
  useEffect(() => {
    openGuide = () => setOpen(true);
    return () => { openGuide = null; };
  }, []);
  return (
    <>
      <button className={`btn-secondary whitespace-nowrap ${className}`} onClick={() => setOpen(true)}
        title="How to name videos: WHAT_WHERE_NN">
        <span className="font-mono text-xs font-bold">Aa</span> <span className="hidden md:inline">Naming guide</span>
      </button>
      {open && <NamingGuide onClose={() => setOpen(false)} />}
    </>
  );
}

export function NamingGuide({ onClose }: { onClose: () => void }) {
  const naming = useNaming();
  const { user } = useRig();
  const toast = useToast();
  const [probe, setProbe] = useState("");
  const [server, setServer] = useState<{ taken: boolean; suggestion?: string | null } | null>(null);
  const codes = useMemo(() => new Set((naming?.codes ?? []).map((c) => c.code)), [naming]);
  const errors = probe.trim() ? nameErrors(probe.trim(), codes.size ? codes : undefined) : [];
  useEffect(() => {
    setServer(null);
    const name = probe.trim();
    if (!name) return;
    const t = setTimeout(() => api.namingCheck(name).then(setServer).catch(() => {}), 350);
    return () => clearTimeout(t);
  }, [probe]);
  const byCat = useMemo(() => {
    const m = new Map<string, NamingConvention["codes"]>();
    for (const c of naming?.codes ?? []) m.set(c.category ?? "Other", [...(m.get(c.category ?? "Other") ?? []), c]);
    return [...m.entries()];
  }, [naming]);
  const setMode = async (mode: "enforce" | "warn" | "off") => {
    try { await api.setNamingMode(mode); await refreshNaming(); toast("ok", `Upload naming check: ${mode}.`); }
    catch (e) { toast("bad", (e as ApiError).message); }
  };
  return (
    <Modal title="Naming guide" onClose={onClose} wide>
      <div className="space-y-5 text-sm">
        <section>
          <div className="rounded-xl bg-surface-2 p-4 text-center ring-1 ring-inset ring-line">
            <div className="font-mono text-xl font-bold tracking-wide sm:text-2xl">
              <span className="text-brand">WHAT</span>_<span className="text-emerald-700 dark:text-emerald-400">WHERE</span>_<span className="text-violet-600 dark:text-violet-400">NN</span>
              <span className="text-subtle">.mp4</span>
            </div>
            <div className="mt-1 text-muted">what it tests · where it's from · which clip</div>
          </div>
          <ul className="mt-3 space-y-1.5">
            {(naming?.examples ?? []).map((e) => (
              <li key={e.name} className="flex flex-wrap items-baseline gap-x-3"><span className="font-mono font-semibold">{e.name}</span><span className="text-muted">{e.reads}</span></li>
            ))}
          </ul>
        </section>

        <section className="grid gap-3 sm:grid-cols-3">
          <Rule title="WHAT" tone="text-brand">The analytics code below, UPPERCASE. One per video: its main analytics.</Rule>
          <Rule title="WHERE" tone="text-emerald-700 dark:text-emerald-400">Site or camera, lowercase, <span className="font-mono">-</span> between words, up to {WHERE_MAX} characters. Unknown? one word for the scene.</Rule>
          <Rule title="NN" tone="text-violet-600 dark:text-violet-400">Clip number 01–99. The app picks the next free one.</Rule>
        </section>
        <p className="text-muted">
          Optional tags at the end: {TAGS.map((t) => <code key={t} className="mx-0.5 rounded bg-surface-2 px-1 font-mono text-xs">{t}</code>)} and
          <code className="mx-0.5 rounded bg-surface-2 px-1 font-mono text-xs">12fps</code>. <b>web</b> = downloaded from the internet.
          No dates, lengths, "test", "copy of", spaces or brackets: the library already shows length, size, uploader and date.
        </p>

        <section>
          <h3 className="mb-2 font-semibold">Analytics codes</h3>
          <div className="grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
            {byCat.map(([cat, list]) => (
              <div key={cat}>
                <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-subtle">{cat}</div>
                <ul className="space-y-1">
                  {list.map((c) => (
                    <li key={c.code} className="flex items-center gap-2">
                      <span className={`w-20 shrink-0 rounded px-1.5 py-0.5 text-center font-mono text-xs font-bold ring-1 ring-inset ${codeTone(c.code)}`}>{c.code}</span>
                      <span className="truncate text-muted">{c.analytics}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
            {!naming && <Spinner />}
          </div>
        </section>

        {!!naming?.places.length && (
          <section>
            <h3 className="mb-2 font-semibold">WHERE values already in use</h3>
            <div className="flex flex-wrap gap-1.5">
              {naming.places.map((p) => (
                <span key={p.where} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-xs ring-1 ring-inset ring-line"
                  title={`${p.videos} video(s): ${p.codes.join(", ")}`}>{p.where}</span>
              ))}
            </div>
          </section>
        )}

        <section>
          <h3 className="mb-2 font-semibold">Check a name</h3>
          <input className="input font-mono" placeholder="e.g. ALPR_toll-s1_01.mp4" value={probe} aria-label="Name to check"
            onChange={(e) => setProbe(e.target.value)} />
          {probe.trim() && (
            <div className="mt-2 text-sm" aria-live="polite">
              {errors.length
                ? <ul className="list-disc space-y-0.5 pl-5 text-amber-700 dark:text-amber-300">{errors.map((x) => <li key={x}>{x}</li>)}</ul>
                : server?.taken ? <span className="text-amber-700 dark:text-amber-300">Valid, but a video already has this name.</span>
                  : <span className="font-semibold text-emerald-700 dark:text-emerald-300">✓ Follows the convention.</span>}
              {errors.length > 0 && server?.suggestion && <div className="mt-1 text-muted">Suggestion: <span className="font-mono text-fg">{server.suggestion}</span></div>}
            </div>
          )}
        </section>

        {naming && unavailable && (
          <section className="border-t border-line pt-3 text-xs text-muted">
            Uploads are not checked yet: please name new videos this way already. The check, renaming and the
            "Non-standard names" filter switch on with the next server update.
          </section>
        )}
        {naming && !unavailable && (
          <section className="border-t border-line pt-3 text-xs text-muted">
            Uploads: {naming.mode === "enforce" ? "other names are refused" : naming.mode === "warn" ? "other names are allowed and flagged in the library" : "names are not checked"}.
            {user.is_admin && (
              <select className="input ml-2 inline-block h-7 w-auto py-0 text-xs" value={naming.mode} aria-label="Upload naming check"
                onChange={(e) => setMode(e.target.value as "enforce" | "warn" | "off")}>
                <option value="enforce">enforce</option><option value="warn">warn</option><option value="off">off</option>
              </select>
            )}
          </section>
        )}
      </div>
    </Modal>
  );
}

function Rule({ title, tone, children }: { title: string; tone: string; children: ReactNode }) {
  return (
    <div className="rounded-lg border border-line p-3">
      <div className={`font-mono text-sm font-bold ${tone}`}>{title}</div>
      <div className="mt-1 text-muted">{children}</div>
    </div>
  );
}
