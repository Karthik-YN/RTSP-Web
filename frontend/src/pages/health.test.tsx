import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ToastProvider } from "../components/common";
import type { HealthReport } from "../types/api";
import { HealthPage, reportText } from "./HealthPage";

vi.mock("../hooks/useRigData", () => ({
  useRig: () => ({ user: { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false } }),
}));

const report: HealthReport = {
  mode: "full", overall: "WARN", started_at: "2026-11-12T10:00:00+00:00", duration_s: 2.4, server: "rig02.lan",
  summary: { PASS: 2, WARN: 1, FAIL: 0, SKIP: 1 },
  checks: [
    { id: "mediamtx", name: "MediaMTX running", status: "PASS", detail: "Control API answers", ms: 3, data: {} },
    { id: "disk", name: "Disk space", status: "WARN", detail: "6.0 GiB free", ms: 1, data: {} },
    { id: "stream:cam1", name: "Stream cam1", status: "PASS", detail: "50 video packets in 2 s", ms: 2100, data: {} },
    { id: "clock", name: "Clock synchronised", status: "SKIP", detail: "Not checkable here", ms: 5, data: {} },
  ],
};

const capacity = {
  max_active_streams: { value: 20, default: 20, overridden: false, updated_at: null, updated_by: null, min: 1, max: 500 },
  max_transcode_streams: { value: 4, default: 4, overridden: false, updated_at: null, updated_by: null, min: 0, max: 64 },
};

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fn = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) =>
    new Response(JSON.stringify(handler(String(url), init)), { status: 200, headers: { "Content-Type": "application/json" } }));
  globalThis.fetch = fn as typeof fetch;
  return fn;
}

describe("Health page", () => {
  it("runs the test and lists every check with its status", async () => {
    const calls = mockFetch((url, init) => {
      if (url.endsWith("/api/health/test/last")) return { full: null, quick: null, auto_interval_s: 600 };
      if (url.endsWith("/api/health/test") && init?.method === "POST") return report;
      if (url.endsWith("/api/settings/capacity")) {
        return init?.method === "PUT"
          ? { ...capacity, max_active_streams: { ...capacity.max_active_streams, value: 40, overridden: true, updated_by: "anonymous" } }
          : capacity;
      }
      return {};
    });
    const user = userEvent.setup();
    render(<ToastProvider><HealthPage /></ToastProvider>);
    expect(await screen.findByText(/No full test yet/)).toBeInTheDocument();
    expect(screen.getByText(/Automatic quick check every 10 min/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Run health test/ }));
    const card = await screen.findByTestId("health-report");
    expect(card).toHaveTextContent("WARN");
    expect(card).toHaveTextContent("Disk space");
    expect(card).toHaveTextContent("50 video packets in 2 s");

    // capacity limit edit
    const input = await screen.findByLabelText("Streams at once");
    await user.clear(input);
    await user.type(input, "40");
    await user.click(screen.getByRole("button", { name: /Save limits/ }));
    await waitFor(() => expect(screen.getByText(/set by anonymous/)).toBeInTheDocument());
    const put = calls.mock.calls.find(([, i]) => (i as RequestInit | undefined)?.method === "PUT");
    expect(JSON.parse(String((put![1] as RequestInit).body))).toMatchObject({ max_active_streams: 40 });
  });

  it("makes a readable text report", () => {
    const txt = reportText(report);
    expect(txt).toContain("Result: WARN  (2 pass, 1 warn, 0 fail, 1 skipped)");
    expect(txt).toContain("WARN  Disk space: 6.0 GiB free");
  });
});
