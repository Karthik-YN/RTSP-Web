import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { api, ApiError } from "../services/api";
import { useRig } from "../hooks/useRigData";
import { formatBitrate, formatBytes, formatDuration } from "../lib/format";
import { useTheme, type ThemePref } from "../lib/theme";
import { Avatar, ErrorBox, Modal, Spinner, useToast, WarnBox } from "./common";
import { CreateStreamDialog, type StreamTemplate } from "./CreateStreamDialog";
import {
  ActivityIcon, CameraIcon, FilmIcon, GridIcon, KeyIcon, LogoutIcon, MenuIcon, MonitorIcon, MoonIcon, PlusIcon,
  SunIcon, UsersIcon,
} from "./icons";

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
  useEffect(() => { document.title = `${live} live · RIG-02`; }, [live]);

  return (
    <CreateCtx.Provider value={(videoId, template) => setCreating({ videoId, template })}>
      <div className="flex min-h-screen">
        <Sidebar open={navOpen} onClose={() => setNavOpen(false)} />
        <div className="flex min-w-0 flex-1 flex-col">
          <TopBar onMenu={() => setNavOpen(true)} onCreate={() => setCreating({})} onLogout={onLogout} />
          <Banners />
          <main className="mx-auto w-full max-w-[100rem] flex-1 px-4 pb-10 pt-4 sm:px-6">{children}</main>
        </div>
      </div>
      {creating && rig.streams && (
        <CreateStreamDialog videos={rig.videos ?? []} streams={rig.streams} config={rig.config}
          initialVideoId={creating.videoId} template={creating.template} onClose={() => setCreating(null)}
          onCreated={() => { rig.refreshStreams(); rig.refreshVideos(); }} />
      )}
    </CreateCtx.Provider>
  );
}

// ------------------------------------------------------------------ sidebar
function Sidebar({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user, streams, videos, config } = useRig();
  const problems = streams?.filter((s) => s.status === "ERROR" || (s.desired_state === "RUNNING" && s.status !== "RUNNING")).length ?? 0;
  const items: { to: string; label: string; icon: ReactNode; badge?: ReactNode }[] = [
    { to: "/streams", label: "Streams", icon: <GridIcon />,
      badge: problems ? <Badge tone="bad">{problems}</Badge> : streams ? <Badge>{streams.length}</Badge> : null },
    { to: "/library", label: "Video library", icon: <FilmIcon />, badge: videos ? <Badge>{videos.length}</Badge> : null },
    { to: "/events", label: "Events", icon: <ActivityIcon /> },
  ];
  if (user.is_admin && user.auth_enabled) items.push({ to: "/users", label: "Users", icon: <UsersIcon /> });
  return (
    <>
      {open && <div className="fixed inset-0 z-30 bg-slate-950/50 lg:hidden" onClick={onClose} />}
      <aside className={`fixed inset-y-0 left-0 z-40 flex w-60 flex-col border-r border-line bg-surface transition-transform lg:sticky lg:top-0 lg:h-screen lg:translate-x-0 ${open ? "translate-x-0" : "-translate-x-full"}`}>
        <div className="flex items-center gap-2.5 px-4 py-4">
          <span className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-blue-500 to-indigo-600 text-white shadow-sm">
            <CameraIcon />
          </span>
          <div className="leading-tight">
            <div className="text-sm font-bold">RIG-02</div>
            <div className="text-xs text-muted">Stream Manager</div>
          </div>
        </div>
        <nav className="flex-1 space-y-0.5 px-2" aria-label="Main">
          {items.map((it) => (
            <NavLink key={it.to} to={it.to}
              className={({ isActive }) => `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${isActive
                ? "bg-brand-soft text-brand" : "text-muted hover:bg-surface-2 hover:text-fg"}`}>
              {it.icon}<span className="flex-1">{it.label}</span>{it.badge}
            </NavLink>
          ))}
        </nav>
        <div className="border-t border-line px-4 py-3 text-xs text-subtle">
          <div className="truncate" title="RTSP host shown in stream URLs">rtsp://{config.public_rtsp_host}:{config.public_rtsp_port}</div>
          <div>v{config.version}</div>
        </div>
      </aside>
    </>
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
  const { user } = useRig();
  return (
    <header className="sticky top-0 z-20 border-b border-line bg-surface/85 backdrop-blur">
      <div className="mx-auto flex max-w-[100rem] items-center gap-3 px-4 py-2.5 sm:px-6">
        <button className="btn-ghost p-1.5 lg:hidden" onClick={onMenu} aria-label="Open menu"><MenuIcon /></button>
        <HealthPills />
        <div className="ml-auto flex items-center gap-2">
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
    <div className="flex min-w-0 flex-wrap items-center gap-2 text-xs">
      <div className="relative" ref={ref}>
        <button onClick={() => setOpen((o) => !o)}
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 font-semibold ring-1 ring-inset ${ok
            ? "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300"
            : "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300"}`}
          aria-expanded={open} aria-haspopup="dialog">
          <span className={`h-2 w-2 rounded-full ${ok ? "bg-emerald-500" : "animate-pulse bg-red-500"}`} />
          {appError ? "Server offline" : ok ? "All systems normal" : `MediaMTX ${state}`}
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
      <Metric label="Disk" pct={disk?.percent ?? undefined} warn={85} bad={95} text={disk ? `${formatBytes(disk.free)} free` : "–"} className="hidden md:flex" />
      <Metric label="Streams" pct={s ? (s.desired_running / Math.max(1, s.max_active)) * 100 : undefined} warn={80} bad={100}
        text={s ? `${s.running}/${s.max_active}` : "–"} className="hidden md:flex" />
      <span className="hidden items-center gap-1 text-muted xl:flex">Viewers <b className="text-fg">{system?.viewers ?? "–"}</b>
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
    <span className={`items-center gap-1.5 rounded-full bg-surface-2 px-2.5 py-1 ring-1 ring-inset ring-line ${className || "flex"}`}
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
        aria-label="User menu" aria-expanded={open}>
        <Avatar name={user.username} size="md" />
        <span className="hidden text-left text-xs leading-tight md:block">
          <span className="block font-semibold">{user.username}</span>
          <span className="block text-muted">{user.role}</span>
        </span>
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
          {!user.auth_enabled && <p className="px-2.5 py-2 text-xs text-subtle">Authentication is disabled on this server.</p>}
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
      {appError && <ErrorBox>The server is not responding. Showing the last known state; retrying every 2 s.</ErrorBox>}
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
