import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation } from "react-router-dom";
import { api, ApiError } from "../services/api";
import { useRig } from "../hooks/useRigData";
import { HOST_KIND_LABEL, urlHost, useRtspAddress } from "../hooks/useRtspAddress";
import { formatBitrate, formatBytes, formatDuration } from "../lib/format";
import { useTheme, type ThemePref } from "../lib/theme";
import { Avatar, ErrorBox, Modal, Spinner, useToast, WarnBox } from "./common";
import { CreateStreamDialog, type StreamTemplate } from "./CreateStreamDialog";
import { NamingGuideButton, refreshNaming } from "./Naming";
import { formatClock, useRigClock } from "../lib/rigClock";
import { isProblem } from "./StreamsTable";
import { conversionSummary, type ConversionSummary } from "../lib/edge";
import {
  CameraIcon, ChevronLeftIcon, FilmIcon, GridIcon, KeyIcon, LogoutIcon, MenuIcon, MonitorIcon, MoonIcon,
  HomeIcon, PlusIcon, ServerIcon, SunIcon, UsersIcon,
} from "./icons";

/** The Plane docs landing page users come from (Streams / Tickets). */
const DOCS_URL = "https://docs.v-analytics.in";

// ------------------------------------------------------------------ create-stream launcher
const CreateCtx = createContext<(videoId?: number, template?: StreamTemplate) => void>(() => {});
export const useCreateStream = () => useContext(CreateCtx);

export function AppShell({ children, onLogout }: { children: ReactNode; onLogout: () => void }) {
  const rig = useRig();
  const [creating, setCreating] = useState<{ videoId?: number; template?: StreamTemplate } | null>(null);
  const [navOpen, setNavOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => setNavOpen(false), [loc.pathname]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el.closest("input, textarea, select, [role=dialog]") || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "n" && rig.user.can_write) { e.preventDefault(); setCreating({}); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [rig.user.can_write]);

  const live = rig.streams?.filter((s) => s.status === "RUNNING").length ?? 0;
  useEffect(() => { document.title = `${live} live · RTSP Studio`; }, [live]);

  const create = useCallback((videoId?: number, template?: StreamTemplate) => setCreating({ videoId, template }), []);

  return (
    <CreateCtx.Provider value={create}>
      <div className="flex min-h-screen">
        <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar onMenu={() => setNavOpen(true)} onCreate={() => setCreating({})} onLogout={onLogout} />
          <Banners />
          <main className="mx-auto w-full max-w-[100rem] flex-1 px-4 pb-10 pt-4 sm:px-6">{children}</main>
        </div>
      </div>
      {creating && rig.streams && (
        <CreateStreamDialog videos={rig.videos ?? []} streams={rig.streams} config={rig.config} isAdmin={rig.user.is_admin}
          initialVideoId={creating.videoId} template={creating.template} onClose={() => setCreating(null)}
          onCreated={() => { rig.refreshStreams(); rig.refreshVideos(); }} />
      )}
    </CreateCtx.Provider>
  );
}

// ------------------------------------------------------------------ sidebar
function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, streams, videos, config } = useRig();
  const problems = streams?.filter(isProblem).length ?? 0;
  const items: { to: string; label: string; icon: ReactNode; badge?: ReactNode }[] = [
    { to: "/", label: "Overview", icon: <HomeIcon /> },
    { to: "/streams", label: "Streams", icon: <GridIcon />,
      badge: problems ? <Badge tone="bad">{problems}</Badge> : streams ? <Badge>{streams.length}</Badge> : null },
    { to: "/library", label: "Library", icon: <FilmIcon />, badge: videos ? <Badge>{videos.length}</Badge> : null },
    { to: "/system", label: "System", icon: <ServerIcon /> },
  ];
  if (user.is_admin && user.auth_enabled) items.push({ to: "/users", label: "Users", icon: <UsersIcon /> });
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-slate-950/50 lg:hidden" onClick={onClose} />}
      <aside className={`fixed inset-y-0 left-0 z-40 flex w-72 flex-col border-r border-line bg-surface transition-transform lg:sticky lg:top-0 lg:h-screen lg:w-56 lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center gap-2.5 px-4 py-4">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white shadow-sm">
            <CameraIcon />
          </span>
          <div className="leading-tight">
            <div className="text-sm font-bold">RTSP Studio</div>
            <div className="text-xs text-muted">rig02 · test cameras</div>
          </div>
        </div>
        <nav className="space-y-0.5 px-2" aria-label="Main">
          {items.map((it) => (
            <NavLink key={it.to} to={it.to} end={it.to === "/"}
              className={({ isActive }) => `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${isActive
                ? "bg-brand-soft text-brand" : "text-muted hover:bg-surface-2 hover:text-fg"}`}>
              {it.icon}<span className="flex-1">{it.label}</span>{it.badge}
            </NavLink>
          ))}
        </nav>
        <div className="flex-1" />
        <div className="border-t border-line px-4 py-3 text-xs text-subtle">
          <RtspAddressPicker port={config.public_rtsp_port} />
          <div>v{config.version}</div>
        </div>
      </aside>
    </>
  );
}

/** Which server address stream URLs are copied with (for users on other subnets / VPN). */
export function RtspAddressPicker({ port }: { port: number }) {
  const addr = useRtspAddress();
  const browser = addr.hosts.find((h) => h.kind === "browser");
  if (addr.hosts.length <= 1) {
    return <div className="truncate" title="Server address used in stream URLs">rtsp://{urlHost(addr.host ?? "")}:{port}</div>;
  }
  return (
    <div className="space-y-1">
      <label className="block">
        <span className="sr-only">RTSP address</span>
        <span className="mb-0.5 block text-[11px] uppercase tracking-wide">Copied URLs use</span>
        <select className="input h-8 w-full py-0 font-mono text-xs" value={addr.host ?? ""} aria-label="RTSP address"
          title="Server address used in copied RTSP URLs" onChange={(e) => addr.setHost(e.target.value)}>
          {addr.hosts.map((h) => (
            <option key={h.host} value={h.host}>
              {urlHost(h.host)}:{port} ({h.network === "tailscale" ? "Tailscale, any network" : HOST_KIND_LABEL[h.kind]})
            </option>
          ))}
        </select>
      </label>
      {browser && addr.host !== browser.host && (
        <button className="text-left text-[11px] text-brand hover:underline" onClick={() => addr.setHost(browser.host)}>
          You opened this page via {browser.network === "tailscale" ? "Tailscale " : ""}{urlHost(browser.host)}. Use it in URLs?
        </button>
      )}
      {addr.custom && addr.hosts.find((h) => h.host === addr.host)?.network === "tailscale" && (
        <div className="text-[11px] text-muted">Plays on devices signed in to Tailscale, from any network.</div>
      )}
      {addr.custom && <div className="text-[11px] text-amber-700 dark:text-amber-300">Not the main address: give the edge the main one.</div>}
    </div>
  );
}

/** New analytics (name, category, optional WHAT code for video names); opened from the Overview page. */
export function NewAnalyticsDialog({ categories, onClose, onCreated }: {
  categories: { id: number; name: string }[]; onClose: () => void; onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [cat, setCat] = useState<string>(String(categories[0]?.id ?? "new"));
  const [newCat, setNewCat] = useState("");
  const [desc, setDesc] = useState("");
  const [code, setCode] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const cid = cat === "new" ? (await api.createCategory(newCat.trim())).id : Number(cat);
      await api.createAnalytics(name.trim(), cid, desc.trim() || undefined, code.trim() || undefined);
      refreshNaming();
      onCreated();
    } catch (e) {
      setErr((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  const codeOk = !code || /^[A-Z][A-Z0-9]{1,7}$/.test(code);
  const ok = name.trim() && (cat !== "new" || newCat.trim()) && codeOk;
  return (
    <Modal title="New analytics" onClose={onClose}>
      <div className="space-y-3">
        <div>
          <label className="label" htmlFor="an-name">Name</label>
          <input id="an-name" className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. QueueLength" autoFocus />
        </div>
        <div>
          <label className="label" htmlFor="an-cat">Category</label>
          <select id="an-cat" className="input" value={cat} onChange={(e) => setCat(e.target.value)}>
            {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            <option value="new">+ New category…</option>
          </select>
          {cat === "new" && <input className="input mt-2" value={newCat} onChange={(e) => setNewCat(e.target.value)} placeholder="Category name" aria-label="New category name" />}
        </div>
        <div>
          <label className="label" htmlFor="an-code">Code in video names (optional)</label>
          <input id="an-code" className={`input font-mono ${codeOk ? "" : "input-error"}`} value={code} maxLength={8}
            placeholder="e.g. RLVD — chosen from the name if empty"
            onChange={(e) => setCode(e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ""))} />
          <p className="mt-1 text-xs text-muted">The WHAT part of <span className="font-mono">WHAT_WHERE_NN</span>: 2–8 capital letters/digits.</p>
        </div>
        <div>
          <label className="label" htmlFor="an-desc">Description (optional)</label>
          <textarea id="an-desc" className="input min-h-[4rem]" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={!ok || busy} onClick={submit}>{busy ? <Spinner /> : null} Add analytics</button>
        </div>
      </div>
    </Modal>
  );
}

function Badge({ children, tone }: { children: ReactNode; tone?: "bad" }) {
  return (
    <span className={`rounded-full px-1.5 text-[11px] font-semibold ${tone === "bad" ? "bg-red-500 text-white" : "bg-surface-2 text-muted"}`}>
      {children}
    </span>
  );
}

// ------------------------------------------------------------------ top bar
function TopBar({ onMenu, onCreate, onLogout }: { onMenu: () => void; onCreate: () => void; onLogout: () => void }) {
  // publish the header's height as --header-h, so sticky bars below it sit flush at any width
  const headerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const el = headerRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const set = () => document.documentElement.style.setProperty("--header-h", `${el.offsetHeight}px`);
    set();
    const ro = new ResizeObserver(set);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const { user } = useRig();
  return (
    <header ref={headerRef} className="sticky top-0 z-20 border-b border-line bg-surface/85 backdrop-blur">
      <div className="mx-auto flex max-w-[100rem] items-center gap-3 px-4 py-2.5 sm:px-6">
        <button className="btn-ghost p-1.5 lg:hidden" onClick={onMenu} aria-label="Open menu"><MenuIcon /></button>
        <a href={DOCS_URL} className="btn-secondary whitespace-nowrap" title="Back to the docs page" aria-label="Back to docs">
          <ChevronLeftIcon /> <span className="hidden sm:inline">Docs</span>
        </a>
        <HealthPills />
        <div className="ml-auto flex items-center gap-2">
          <ConversionChip />
          <RigClockChip />
          <NamingGuideButton />
          {user.can_write && (
            <button className="btn-primary whitespace-nowrap" onClick={onCreate} title="New stream (N)">
              <PlusIcon /> <span className="hidden sm:inline">New stream</span>
              <span className="kbd hidden border-white/30 bg-white/15 text-white sm:inline">N</span>
            </button>
          )}
          <UserMenu onLogout={onLogout} />
        </div>
      </div>
    </header>
  );
}

/** Poll the conversion queue: every 5 s while something is queued/running, else every 30 s; never in hidden tabs. */
export function useConversionSummary(activeMs = 5000, idleMs = 30000): ConversionSummary | null {
  const [sum, setSum] = useState<ConversionSummary | null>(null);
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let active = false;
    const tick = async () => {
      timer = null;
      if (document.hidden) return; // resumed by visibilitychange
      try {
        const next = conversionSummary(await api.conversions());
        active = !!next;
        if (alive) setSum((prev) => (JSON.stringify(prev) === JSON.stringify(next) ? prev : next));
      } catch {
        /* endpoint down or not allowed: keep the last state, try again later */
      }
      if (alive && !timer) timer = setTimeout(tick, active ? activeMs : idleMs);
    };
    const onVis = () => {
      if (document.hidden || !alive) return;
      if (timer) clearTimeout(timer);
      void tick();
    };
    void tick();
    document.addEventListener("visibilitychange", onVis);
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [activeMs, idleMs]);
  return sum;
}

/** The rig's own time (IST, hours:minutes: the screen changes once a minute); opens the big Rig clock page with ms. */
function RigClockChip() {
  const [clock] = useRigClock();
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => { if (!document.hidden) setNow(Date.now()); }, 1000);
    return () => clearInterval(t);
  }, []);
  if (!clock) return null;
  const rig = now + clock.offsetMs;
  return (
    <Link to="/clock" className="hidden whitespace-nowrap rounded-full px-2.5 py-1 font-mono text-xs tabular-nums text-muted ring-1 ring-inset ring-line hover:text-fg md:inline-flex"
      title={`The rig's clock (±${Math.round(clock.errorMs)} ms). Click for the big clock with milliseconds.`} aria-label="Rig clock">
      {formatClock(rig, "IST", false).slice(0, 5)} IST
    </Link>
  );
}

/** "Converting 2 · 45%" while the library converts videos; hidden otherwise. */
function ConversionChip() {
  const sum = useConversionSummary();
  if (!sum) return null;
  return (
    <Link to="/library" title={sum.title} aria-label={`Converting ${sum.active} video${sum.active > 1 ? "s" : ""}`}
      className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-blue-500/10 px-2.5 py-1 text-xs font-medium text-blue-700 ring-1 ring-inset ring-blue-500/25 hover:bg-blue-500/20 dark:text-blue-300">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" />
      <span className="hidden sm:inline">Converting {sum.active}</span>
      <span className="tabular-nums"><span className="hidden sm:inline"> · </span>{sum.percent != null ? `${sum.percent}%` : "queued"}</span>
    </Link>
  );
}

function HealthPills() {
  const { system, mediamtx, appError, user, refreshAll } = useRig();
  const toast = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const state = appError ? "OFFLINE" : mediamtx?.state ?? "UNKNOWN";
  const ok = state === "UP";
  const s = system?.streams;
  const disk = system?.disks?.videos;

  const control = async (kind: "start" | "restart") => {
    setBusy(true);
    try {
      toast("info", (kind === "start" ? await api.mediamtxStart() : await api.mediamtxRestart()).message);
      refreshAll();
    } catch (e) {
      toast("bad", (e as ApiError).message);
    } finally {
      setBusy(false);
      setOpen(false);
    }
  };

  return (
    // one line always: lower-priority metrics hide as the bar narrows (lg: the sidebar takes 16 rem)
    <div className="flex min-w-0 flex-nowrap items-center gap-2 overflow-hidden text-xs">
      <div className="relative shrink-0" ref={ref}>
        <button onClick={() => setOpen((o) => !o)}
          className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-1 font-semibold ring-1 ring-inset ${ok
            ? "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300"
            : "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300"}`}
          aria-expanded={open} aria-haspopup="dialog">
          <span className={`h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "animate-pulse bg-red-500"}`} />
          <span className="sm:hidden">{appError ? "Offline" : ok ? "OK" : `MediaMTX ${state}`}</span>
          <span className="hidden sm:inline">{appError ? "Server offline" : ok ? "All systems normal" : `MediaMTX ${state}`}</span>
        </button>
        {open && (
          <div className="absolute left-0 top-full z-30 mt-2 w-72 rounded-xl border border-line bg-surface p-4 text-sm shadow-pop" role="dialog">
            <div className="mb-2 font-semibold">RTSP server (MediaMTX)</div>
            <dl className="space-y-1 text-xs">
              <Row k="State" v={state} />
              <Row k="Control" v={mediamtx?.control_mode ?? "–"} />
              <Row k="Paths ready" v={mediamtx ? `${mediamtx.ready_paths}/${mediamtx.path_count}` : "–"} />
              <Row k="Readers" v={mediamtx?.total_readers ?? "–"} />
              <Row k="Recoveries" v={mediamtx?.recoveries ?? 0} />
              {mediamtx?.last_error && <Row k="Last error" v={mediamtx.last_error} />}
            </dl>
            {user.can_write && (
              <div className="mt-3 flex gap-2">
                {ok
                  ? <button className="btn-secondary btn-xs" disabled={busy} onClick={() => control("restart")}>Restart MediaMTX</button>
                  : <button className="btn-primary btn-xs" disabled={busy} onClick={() => control("start")}>Start MediaMTX</button>}
                {busy && <Spinner />}
              </div>
            )}
            <p className="mt-2 text-[11px] text-subtle">Restarting drops every reader briefly; streams are republished automatically.</p>
          </div>
        )}
      </div>
      <Metric label="CPU" pct={system?.cpu_percent} text={system ? `${system.cpu_percent.toFixed(0)}%` : "–"} />
      <Metric label="RAM" pct={system?.memory.percent} text={system ? `${formatBytes(system.memory.available)} free` : "–"} className="hidden sm:flex" />
      <Metric label="Disk" pct={disk?.percent ?? undefined} warn={85} bad={95} text={disk ? `${formatBytes(disk.free)} free` : "–"} className="hidden xl:flex" />
      <Metric label="Streams" pct={s ? (s.desired_running / Math.max(1, s.max_active)) * 100 : undefined} warn={80} bad={100}
        text={s ? `${s.running}/${s.max_active}` : "–"} className="hidden xl:flex" />
      <span className="hidden shrink-0 items-center gap-1 whitespace-nowrap text-muted 2xl:flex">Viewers <b className="text-fg">{system?.viewers ?? "–"}</b>
        {system?.rtsp_egress_bps != null && <span className="ml-1" title="Total RTSP bitrate sent to readers">· {formatBitrate(system.rtsp_egress_bps)} out</span>}</span>
    </div>
  );
}

function Row({ k, v }: { k: string; v: ReactNode }) {
  return <div className="flex justify-between gap-3"><dt className="text-muted">{k}</dt><dd className="truncate text-right font-medium">{v}</dd></div>;
}

function Metric({ label, pct, text, warn = 80, bad = 90, className = "" }: {
  label: string; pct?: number | null; text: string; warn?: number; bad?: number; className?: string;
}) {
  const p = Math.max(0, Math.min(100, pct ?? 0));
  const color = p >= bad ? "bg-red-500" : p >= warn ? "bg-amber-500" : "bg-emerald-500";
  return (
    <span className={`shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full bg-surface-2 px-2.5 py-1 ring-1 ring-inset ring-line ${className || "flex"}`}
      title={`${label}: ${text}`}>
      <span className="font-semibold text-muted">{label}</span>
      <span className="h-1.5 w-10 overflow-hidden rounded-full bg-line"><span className={`block h-full ${color}`} style={{ width: `${p}%` }} /></span>
      <span className="font-medium tabular-nums">{text}</span>
    </span>
  );
}

// ------------------------------------------------------------------ user menu
function UserMenu({ onLogout }: { onLogout: () => void }) {
  const { user } = useRig();
  const { pref, set } = useTheme();
  const [open, setOpen] = useState(false);
  const [pw, setPw] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);
  const themes: [ThemePref, string, ReactNode][] = [["light", "Light", <SunIcon key="l" />], ["dark", "Dark", <MoonIcon key="d" />], ["system", "System", <MonitorIcon key="s" />]];
  return (
    <div className="relative" ref={ref}>
      <button className="flex items-center gap-2 rounded-full p-0.5 pr-2 hover:bg-surface-2" onClick={() => setOpen((o) => !o)}
        aria-label={user.auth_enabled ? "User menu" : "Theme"} aria-expanded={open}>
        {user.auth_enabled ? (
          <>
            <Avatar name={user.username} size="md" />
            <span className="hidden text-left text-xs leading-tight md:block">
              <span className="block font-semibold">{user.username}</span>
              <span className="block text-muted">{user.role}</span>
            </span>
          </>
        ) : <span className="flex h-8 w-8 items-center justify-center text-muted" title="Theme">{pref === "dark" ? <MoonIcon /> : <SunIcon />}</span>}
      </button>
      {open && (
        <div className="absolute right-0 top-full z-30 mt-2 w-56 rounded-xl border border-line bg-surface p-1.5 text-sm shadow-pop" role="menu">
          <div className="px-2.5 py-1.5 text-xs font-semibold uppercase tracking-wide text-subtle">Theme</div>
          <div className="mx-1.5 mb-1.5 grid grid-cols-3 gap-1 rounded-lg bg-surface-2 p-1">
            {themes.map(([v, label, icon]) => (
              <button key={v} onClick={() => set(v)} aria-pressed={pref === v}
                className={`flex flex-col items-center gap-0.5 rounded-md py-1.5 text-[11px] ${pref === v ? "bg-surface text-fg shadow-sm" : "text-muted hover:text-fg"}`}>
                {icon}{label}
              </button>
            ))}
          </div>
          {user.auth_enabled && (
            <>
              <button className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left hover:bg-surface-2" onClick={() => { setPw(true); setOpen(false); }}>
                <KeyIcon /> Change password
              </button>
              <button className="flex w-full items-center gap-2 rounded-md px-2.5 py-2 text-left text-red-600 hover:bg-red-500/10" onClick={onLogout}>
                <LogoutIcon /> Sign out
              </button>
            </>
          )}
        </div>
      )}
      {pw && <ChangePasswordDialog onClose={() => setPw(false)} />}
    </div>
  );
}

function ChangePasswordDialog({ onClose }: { onClose: () => void }) {
  const toast = useToast();
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [again, setAgain] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = again.length > 0 && again !== next;
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.changePassword(cur, next);
      toast("ok", "Password changed.");
      onClose();
    } catch (ex) {
      setErr((ex as ApiError).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal title="Change password" onClose={onClose}>
      <form className="space-y-3" onSubmit={submit}>
        <div><label className="label" htmlFor="pw-cur">Current password</label>
          <input id="pw-cur" type="password" className="input" autoComplete="current-password" value={cur} onChange={(e) => setCur(e.target.value)} autoFocus /></div>
        <div><label className="label" htmlFor="pw-new">New password (min. 8 characters)</label>
          <input id="pw-new" type="password" className="input" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} /></div>
        <div><label className="label" htmlFor="pw-again">Repeat new password</label>
          <input id="pw-again" type="password" className={`input ${mismatch ? "input-error" : ""}`} autoComplete="new-password" value={again} onChange={(e) => setAgain(e.target.value)} />
          {mismatch && <p className="mt-1 text-xs text-red-600">Passwords do not match.</p>}</div>
        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn-primary" disabled={busy || !cur || next.length < 8 || next !== again}>{busy && <Spinner />} Change password</button>
        </div>
      </form>
    </Modal>
  );
}

// ------------------------------------------------------------------ banners
function Banners() {
  const { appError, mediamtx, config, system } = useRig();
  const down = mediamtx?.state === "DOWN";
  const rec = mediamtx?.last_recovery;
  const recentRecovery = rec && !down && Date.now() - new Date(rec.recovered_at).getTime() < 120000;
  const warnings = [...(config.warnings ?? []), ...(system?.warnings ?? [])];
  if (!appError && !down && !recentRecovery && warnings.length === 0) return null;
  return (
    <div className="mx-auto w-full max-w-[100rem] space-y-2 px-4 pt-4 sm:px-6">
      {appError && (
        <ErrorBox>
          <span role="status" data-testid="offline-banner">
            {appError.status === 0 ? "Connection to the server lost" : `The server is failing (${appError.status})`}
            {(appError as { since?: number }).since ? ` since ${new Date((appError as { since?: number }).since!).toLocaleTimeString()}` : ""}.
            {" "}What you see is the last known state, not live; retrying every 2 s.
          </span>
        </ErrorBox>
      )}
      {down && (
        <ErrorBox>
          <b>The RTSP server (MediaMTX) is down</b>
          {mediamtx?.last_detection_s != null && <> (detected after {mediamtx.last_detection_s.toFixed(1)} s)</>}.{" "}
          {mediamtx?.auto_recover ? "Automatic recovery is in progress; streams are republished when it is back." : "Use the status button above to start it."}
          {mediamtx?.last_control_message && <span className="block text-xs opacity-80">Recovery: {mediamtx.last_control_message}</span>}
        </ErrorBox>
      )}
      {recentRecovery && (
        <WarnBox>MediaMTX recovered {formatDuration((Date.now() - new Date(rec!.recovered_at).getTime()) / 1000)} ago after a{" "}
          {rec!.outage_s?.toFixed(1) ?? "?"} s outage; streams are being republished.</WarnBox>
      )}
      {warnings.map((w, i) => <WarnBox key={i}>{w}</WarnBox>)}
    </div>
  );
}
