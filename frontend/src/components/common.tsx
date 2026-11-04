import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import type { StreamStatus } from "../types/api";

// ------------------------------------------------------------------ Modal
export function Modal({ title, onClose, children, wide = false }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-slate-950/50 p-4 pt-12 backdrop-blur-sm"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-label={title}
        className={`card w-full shadow-pop ${wide ? "max-w-4xl" : "max-w-lg"} p-0`}>
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <h2 className="text-base font-semibold">{title}</h2>
          <button className="btn-ghost btn-xs" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ Confirm
export function ConfirmDialog({ title, message, confirmLabel, danger, onConfirm, onCancel }: {
  title: string; message: ReactNode; confirmLabel: string; danger?: boolean;
  onConfirm: () => void; onCancel: () => void;
}) {
  return (
    <Modal title={title} onClose={onCancel}>
      <div className="text-sm text-muted">{message}</div>
      <div className="mt-4 flex justify-end gap-2">
        <button className="btn-secondary" onClick={onCancel}>Cancel</button>
        <button className={danger ? "btn-danger" : "btn-primary"} onClick={onConfirm} autoFocus>{confirmLabel}</button>
      </div>
    </Modal>
  );
}

// ------------------------------------------------------------------ Status
const STATUS_STYLE: Record<StreamStatus, string> = {
  RUNNING: "bg-emerald-500/10 text-emerald-700 ring-emerald-500/30 dark:text-emerald-300",
  STARTING: "bg-blue-500/10 text-blue-700 ring-blue-500/30 dark:text-blue-300",
  RESTARTING: "bg-amber-500/10 text-amber-700 ring-amber-500/30 dark:text-amber-300",
  STOPPING: "bg-slate-500/10 text-muted ring-slate-500/30",
  STOPPED: "bg-slate-500/10 text-muted ring-slate-500/20",
  ERROR: "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300",
  COMPLETED: "bg-violet-500/10 text-violet-700 ring-violet-500/30 dark:text-violet-300",
};

const STATUS_LABEL: Record<StreamStatus, string> = {
  RUNNING: "Live", STARTING: "Starting", RESTARTING: "Restarting", STOPPING: "Stopping",
  STOPPED: "Stopped", ERROR: "Error", COMPLETED: "Completed",
};

export function StatusBadge({ status, title }: { status: StreamStatus; title?: string | null }) {
  const pulse = status === "STARTING" || status === "RESTARTING" || status === "RUNNING";
  return (
    <span title={title ?? status}
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${STATUS_STYLE[status]}`}>
      <span className="relative flex h-1.5 w-1.5">
        {pulse && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-current opacity-60" />}
        <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-current" />
      </span>
      {STATUS_LABEL[status]}
    </span>
  );
}

export function Pill({ tone, children, title }: { tone: "ok" | "warn" | "bad" | "info" | "muted"; children: ReactNode; title?: string }) {
  const cls = {
    ok: "bg-emerald-500/10 text-emerald-700 ring-emerald-500/25 dark:text-emerald-300",
    warn: "bg-amber-500/10 text-amber-800 ring-amber-500/30 dark:text-amber-300",
    bad: "bg-red-500/10 text-red-700 ring-red-500/30 dark:text-red-300",
    info: "bg-blue-500/10 text-blue-700 ring-blue-500/25 dark:text-blue-300",
    muted: "bg-slate-500/10 text-muted ring-slate-500/20",
  }[tone];
  return <span title={title} className={`inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${cls}`}>{children}</span>;
}

// ------------------------------------------------------------------ Copy
function legacyCopy(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  document.body.removeChild(ta);
  return ok;
}

/** Copy to clipboard. The dashboard is usually served over plain http:// on
 * the office LAN, where navigator.clipboard does not exist, so fall back to
 * execCommand and finally to a prompt with the text pre-selected. */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through */
    }
  }
  if (legacyCopy(text)) return true;
  window.prompt("Copy this text (Ctrl+C / ⌘C):", text);
  return false;
}

export function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    if (await copyText(text)) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }
  };
  return (
    <button type="button" className="btn-secondary btn-xs" onClick={copy} title={`Copy ${text}`}>
      {copied ? "Copied ✓" : label}
    </button>
  );
}

// ------------------------------------------------------------------ Toasts
type ToastAction = { label: string; run: () => void };
type Toast = { id: number; tone: "ok" | "bad" | "info"; text: string; action?: ToastAction };
type Push = (tone: Toast["tone"], text: string, action?: ToastAction) => void;
const ToastCtx = createContext<Push>(() => {});

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = useCallback((id: number) => setToasts((t) => t.filter((x) => x.id !== id)), []);
  const push = useCallback<Push>((tone, text, action) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-4), { id, tone, text, action }]);
    setTimeout(() => dismiss(id), action ? 7000 : tone === "bad" ? 9000 : 4000);
  }, [dismiss]);
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed bottom-4 right-4 z-50 flex w-[min(26rem,calc(100vw-2rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} role="status"
            className={`flex items-start gap-3 rounded-xl border px-4 py-3 text-sm shadow-pop ${t.tone === "bad"
              ? "border-red-500/30 bg-red-600 text-white" : t.tone === "ok"
              ? "border-emerald-500/30 bg-emerald-600 text-white" : "border-line bg-surface text-fg"}`}>
            <span className="flex-1">{t.text}</span>
            {t.action && (
              <button className="font-semibold underline underline-offset-2" onClick={() => { t.action!.run(); dismiss(t.id); }}>
                {t.action.label}
              </button>
            )}
            <button className="opacity-70 hover:opacity-100" onClick={() => dismiss(t.id)} aria-label="Dismiss">✕</button>
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

export const useToast = () => useContext(ToastCtx);

export function ErrorBox({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-800 dark:text-red-200">{children}</div>;
}

export function WarnBox({ children }: { children: ReactNode }) {
  return <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-900 dark:text-amber-200">{children}</div>;
}

export function Spinner() {
  return <span className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-line border-t-brand" />;
}

export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`skeleton ${className}`} aria-hidden="true" />;
}

/** Initials avatar for owners/users (stable color per name). */
export function Avatar({ name, size = "sm" }: { name: string | null | undefined; size?: "sm" | "md" }) {
  const n = name || "?";
  const hue = [...n].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7);
  const dim = size === "md" ? "h-8 w-8 text-xs" : "h-5 w-5 text-[10px]";
  return (
    <span title={n} className={`inline-flex shrink-0 items-center justify-center rounded-full font-semibold text-white ${dim}`}
      style={{ background: `hsl(${hue} 55% 45%)` }}>
      {n.slice(0, 2).toUpperCase()}
    </span>
  );
}
