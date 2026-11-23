import { Component, createContext, useCallback, useContext, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { StreamStatus } from "../types/api";

// ------------------------------------------------------------------ Modal
export function Modal({ title, onClose, children, wide = false, size, footer, subtitle }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
  /** "xl": two-pane forms (form + summary). Default: "wide" when wide, else a narrow card. */
  size?: "md" | "wide" | "xl";
  /** Action bar kept in view at the bottom while the dialog scrolls. */
  footer?: ReactNode;
  subtitle?: ReactNode;
}) {
  const width = { md: "max-w-lg", wide: "max-w-4xl", xl: "max-w-6xl" }[size ?? (wide ? "wide" : "md")];
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  // Rendered at the end of <body>: a dialog opened from inside a card must not be clipped or
  // positioned by the card (cards use content-visibility, which contains fixed children).
  return createPortal(
    // phones: a full-screen sheet; from sm up: a centred card
    <div className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-slate-950/50 backdrop-blur-sm sm:p-4 sm:pt-12"
      onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-label={title}
        className={`card flex min-h-full w-full flex-col p-0 shadow-pop max-sm:rounded-none max-sm:border-0 sm:min-h-0 ${width}`}>
        <div className="sticky top-0 z-20 flex items-center justify-between gap-2 border-b border-line bg-surface px-4 py-3 sm:static sm:rounded-t-xl sm:px-5">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold" title={title}>{title}</h2>
            {subtitle && <p className="truncate text-xs text-muted">{subtitle}</p>}
          </div>
          <button className="btn-ghost btn-xs shrink-0 max-sm:h-9 max-sm:w-9" onClick={onClose} aria-label="Close">✕</button>
        </div>
        <div className="flex-1 p-4 sm:p-5">{children}</div>
        {footer && (
          <div className="sticky bottom-0 z-20 border-t border-line bg-surface/95 px-4 py-3 backdrop-blur sm:rounded-b-xl sm:px-5">
            {footer}
          </div>
        )}
      </div>
    </div>,
    document.body,
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
  LEAD_IN: "bg-sky-500/10 text-sky-700 ring-sky-500/30 dark:text-sky-300",
  WAITING_FOR_VIEWER: "bg-indigo-500/10 text-indigo-700 ring-indigo-500/30 dark:text-indigo-300",
};

const STATUS_LABEL: Record<StreamStatus, string> = {
  RUNNING: "Live", STARTING: "Starting", RESTARTING: "Restarting", STOPPING: "Stopping",
  STOPPED: "Stopped", ERROR: "Error", COMPLETED: "Completed",
  LEAD_IN: "Lead-in", WAITING_FOR_VIEWER: "Waiting for viewer",
};

export function StatusBadge({ status, title }: { status: StreamStatus; title?: string | null }) {
  const pulse = status === "STARTING" || status === "RESTARTING" || status === "RUNNING" || status === "LEAD_IN";
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
  return <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded-md px-1.5 py-0.5 text-xs font-medium ring-1 ring-inset ${cls}`}>{children}</span>;
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

/** A ⋯ button with a small menu: where rare or secondary actions live, to keep rows and cards calm. */
export function MoreMenu({ label, items }: {
  label: string; items: { label: string; icon: ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean; title?: string }[];
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    const away = () => setOpen(false); // scrolling or resizing: don't leave the menu floating away from its button
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    window.addEventListener("scroll", away, true);
    window.addEventListener("resize", away);
    return () => {
      document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc);
      window.removeEventListener("scroll", away, true); window.removeEventListener("resize", away);
    };
  }, [open]);
  return (
    <div className="relative" ref={ref}>
      <button type="button" aria-label={label} title="More" aria-haspopup="menu" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="rounded-lg border border-line bg-surface px-2 py-1 text-sm leading-none text-muted transition hover:bg-surface-2 hover:text-fg">⋯</button>
      {open && (
        <div role="menu" className="absolute right-0 top-full z-20 mt-1 w-52 rounded-lg border border-line bg-surface p-1 text-sm shadow-pop">
          {items.map((it) => (
            <button key={it.label} role="menuitem" disabled={it.disabled} title={it.title}
              onClick={() => { setOpen(false); it.onClick(); }}
              className={`flex w-full items-center gap-2 rounded-md px-2.5 py-1.5 text-left disabled:opacity-40 ${it.danger ? "text-red-600 hover:bg-red-500/10" : "hover:bg-surface-2"}`}>
              {it.icon}{it.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Contains a crash to the part that crashed: shows a small "couldn't show this" box with Retry instead of a blank
 * app. Used around every page and every settings card.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; label?: string; resetKey?: unknown }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidCatch(error: Error, info: ErrorInfo) { console.error(`[${this.props.label ?? "view"}]`, error, info.componentStack); }
  componentDidUpdate(prev: { resetKey?: unknown }) {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div role="alert" className="card space-y-2 border-amber-500/40 p-4 text-sm">
        <div className="font-medium">Couldn't show {this.props.label ?? "this part"}.</div>
        <div className="text-muted">The rest of the dashboard keeps working. Details: {this.state.error.message}</div>
        <button className="btn-secondary btn-xs" onClick={() => this.setState({ error: null })}>Retry</button>
      </div>
    );
  }
}
