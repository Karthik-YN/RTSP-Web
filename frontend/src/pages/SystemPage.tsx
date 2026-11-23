import { Link, useSearchParams } from "react-router-dom";
import { EventsPage } from "./EventsPage";
import { HealthPage, SettingsPanel } from "./HealthPage";

const TABS = [
  { key: "health", label: "Health" },
  { key: "events", label: "Events" },
  { key: "settings", label: "Settings" },
] as const;
type Tab = (typeof TABS)[number]["key"];

/** Everything about the server itself, in one place: health test, event log, settings. */
export function SystemPage() {
  const [params, setParams] = useSearchParams();
  const tab = (TABS.some((t) => t.key === params.get("tab")) ? params.get("tab") : "health") as Tab;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight">System</h1>
          <p className="text-sm text-muted">The server's health, what happened on it, and its settings.</p>
        </div>
        <Link to="/clock" className="btn-secondary">Rig clock</Link>
      </div>
      <div className="flex gap-1 border-b border-line" role="tablist" aria-label="System">
        {TABS.map((t) => (
          <button key={t.key} role="tab" aria-selected={tab === t.key}
            onClick={() => setParams(t.key === "health" ? {} : { tab: t.key }, { replace: true })}
            className={`-mb-px border-b-2 px-3 py-2 text-sm font-medium ${tab === t.key
              ? "border-brand text-brand" : "border-transparent text-muted hover:text-fg"}`}>
            {t.label}
          </button>
        ))}
      </div>
      {tab === "health" && <HealthPage embedded />}
      {tab === "events" && <EventsPage embedded />}
      {tab === "settings" && <SettingsPanel />}
    </div>
  );
}
