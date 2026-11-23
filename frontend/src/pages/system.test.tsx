import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Navigate, Route, Routes } from "react-router-dom";
import { ToastProvider } from "../components/common";
import { RigDataProvider } from "../hooks/useRigData";
import { SystemPage } from "./SystemPage";
import type { AppConfig, User } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = { version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554 } as unknown as AppConfig;

function at(path: string) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    const p = String(url).split("?")[0];
    const body = p === "/api/health/test/last" ? { full: null, quick: null, auto_interval_s: 600 }
      : p === "/api/events" ? [{ id: 1, ts: "2026-11-20T10:00:00Z", level: "INFO", kind: "stream.start", stream_name: "cam-1", username: "x", message: "started" }]
        : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return render(
    <ToastProvider><MemoryRouter initialEntries={[path]}><RigDataProvider user={admin} config={config}>
      <Routes>
        <Route path="/system" element={<SystemPage />} />
        <Route path="/health" element={<Navigate to="/system" replace />} />
        <Route path="/events" element={<Navigate to="/system?tab=events" replace />} />
      </Routes>
    </RigDataProvider></MemoryRouter></ToastProvider>,
  );
}

describe("System page (Health + Events + Settings)", () => {
  it("old /events links land on the Events tab, and tabs switch", async () => {
    const u = userEvent.setup();
    at("/events");
    expect(await screen.findByText("started")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: "Events" })).toHaveAttribute("aria-selected", "true");
    await u.click(screen.getByRole("tab", { name: "Health" }));
    expect(await screen.findByText(/No full test yet/)).toBeInTheDocument();
    await u.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.getByRole("tab", { name: "Settings" })).toHaveAttribute("aria-selected", "true");
  });

  it("old /health links land on the Health tab", async () => {
    at("/health");
    expect(await screen.findByText(/No full test yet/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "System" })).toBeInTheDocument();
  });
});
