import { useState } from "react";
import { api, ApiError } from "../services/api";
import type { LeadInMode, Stream } from "../types/api";
import { useToast } from "./common";
import { PlayIcon } from "./icons";

export interface LeadInValue {
  mode: LeadInMode;
  seconds: number;
  holdMax: number | null;
}

export const LEAD_IN_MODES: { mode: LeadInMode; label: string; help: string }[] = [
  { mode: "fixed", label: "Fixed", help: "Shows the stream's slate (black, its name and a clock) for N seconds, then the video." },
  { mode: "hold", label: "Until a viewer connects", help: "Shows the slate until the edge connects, then the video N seconds later. Use it when cameras are added in the portal after the stream starts." },
  { mode: "none", label: "None", help: "The video starts at once (an edge that connects later misses the start)." },
];

/** Lead-in choice for create / run / edit forms. */
export function LeadInFields({ value, onChange, idPrefix = "lead" }: {
  value: LeadInValue; onChange: (v: LeadInValue) => void; idPrefix?: string;
}) {
  const [secText, setSecText] = useState(String(value.seconds));
  const [maxText, setMaxText] = useState(value.holdMax != null ? String(value.holdMax) : "");
  const help = LEAD_IN_MODES.find((m) => m.mode === value.mode)?.help;
  return (
    <div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-sm" role="radiogroup" aria-label="Lead-in">
        {LEAD_IN_MODES.map((m) => (
          <label key={m.mode} className="flex items-center gap-1.5">
            <input type="radio" name={`${idPrefix}-mode`} checked={value.mode === m.mode} onChange={() => onChange({ ...value, mode: m.mode })} />
            {m.label}
          </label>
        ))}
      </div>
      {value.mode !== "none" && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
          <label htmlFor={`${idPrefix}-s`} className="text-muted">{value.mode === "hold" ? "Video starts" : "Slate for"}</label>
          <input id={`${idPrefix}-s`} className="input w-20 py-1" inputMode="decimal" value={secText}
            onChange={(e) => {
              setSecText(e.target.value);
              const n = Number(e.target.value);
              if (e.target.value.trim() !== "" && Number.isFinite(n) && n >= 0 && n <= 120) onChange({ ...value, seconds: n });
            }} />
          <span className="text-muted">{value.mode === "hold" ? "s after the first viewer" : "s"}</span>
          {value.mode === "hold" && (
            <>
              <label htmlFor={`${idPrefix}-max`} className="ml-2 text-muted">start anyway after</label>
              <input id={`${idPrefix}-max`} className="input w-24 py-1" inputMode="decimal" placeholder="never" value={maxText}
                onChange={(e) => {
                  setMaxText(e.target.value);
                  const n = Number(e.target.value);
                  if (e.target.value.trim() === "") onChange({ ...value, holdMax: null });
                  else if (Number.isFinite(n) && n > 0) onChange({ ...value, holdMax: n });
                }} />
              <span className="text-muted">s</span>
            </>
          )}
        </div>
      )}
      {leadInError(secText) && value.mode !== "none" && <p className="mt-1 text-xs text-red-600">{leadInError(secText)}</p>}
      {help && <p className="mt-1 text-xs text-subtle">{help}</p>}
    </div>
  );
}

export function leadInError(secText: string): string | null {
  const n = Number(secText);
  if (secText.trim() === "" || !Number.isFinite(n) || n < 0 || n > 120) return "Seconds must be between 0 and 120.";
  return null;
}

export function leadInBody(v: LeadInValue) {
  return { lead_in_mode: v.mode, lead_in_s: v.seconds, hold_max_s: v.mode === "hold" ? v.holdMax : null };
}

/** "Lead-in · video in 3 s", "Waiting for a viewer" ... while the slate is on the URL, else null. */
export function leadText(s: Stream): string | null {
  const l = s.runtime?.lead_in;
  if (!l) return null;
  if (l.phase === "preparing") return "Preparing the lead-in";
  if (l.phase === "waiting") return `Waiting for a viewer · video ${l.lead_in_s ?? 0} s after one connects`;
  if (l.phase === "countdown") {
    const left = l.video_in_s != null ? ` in ${Math.max(0, Math.ceil(l.video_in_s))} s` : " soon";
    return `${l.viewer_seen_at ? `Viewer connected${l.viewer ? ` (${l.viewer})` : ""} · video` : "Lead-in · video"}${left}`;
  }
  return null;
}

export function describeLeadIn(s: Pick<Stream, "lead_in_mode" | "lead_in_s" | "hold_max_s">): string {
  if (s.lead_in_mode === "fixed") return `${s.lead_in_s} s slate, then the video`;
  if (s.lead_in_mode === "hold") {
    return `Slate until a viewer connects, video ${s.lead_in_s} s later${s.hold_max_s ? ` (or after ${s.hold_max_s} s)` : ""}`;
  }
  return "None: the video starts at once";
}

/** "Start video now": ends the lead-in (the 2 s start spacing still applies). */
export function GoButton({ s, onDone, small = true }: { s: Stream; onDone: () => void; small?: boolean }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  if (!s.runtime?.lead_in || s.runtime.lead_in.phase === "preparing") return null;
  return (
    <button className={small ? "btn-secondary btn-xs" : "btn-secondary"} disabled={busy} title="End the lead-in and start the video now"
      onClick={async (e) => {
        e.stopPropagation();
        setBusy(true);
        try { await api.go(s.id); toast("ok", `${s.name}: video starting`); onDone(); }
        catch (err) { toast("bad", (err as ApiError).message); }
        finally { setBusy(false); }
      }}>
      <PlayIcon className="h-3.5 w-3.5" /> Start video now
    </button>
  );
}
