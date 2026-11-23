import { render, screen, waitFor, within } from "@testing-library/react";
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
      if (url.endsWith("/api/system/encoders")) return encoders();
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
    // passing stream reads fold into one line; problems stay visible
    expect(card).toHaveTextContent("Streams read over RTSP: 1 ok");
    expect(card).not.toHaveTextContent("50 video packets in 2 s");
    await user.click(within(card).getByRole("button", { name: /Streams read over RTSP/ }));
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

const encoders = (over: Record<string, unknown> = {}) => ({
  choice: "auto", active: "cpu", active_label: "CPU (libx264)", note: null, checked_at: 1, default: "auto", overridden: false,
  choices: ["auto", "cpu", "nvidia", "intel", "vaapi"],
  encoders: [
    { id: "cpu", label: "CPU (libx264)", encoder: "libx264", about: "Works everywhere.", available: true, reason: null, test_ms: 300 },
    { id: "nvidia", label: "NVIDIA GPU (NVENC)", encoder: "h264_nvenc", about: "", available: false, reason: "No NVIDIA GPU visible to the rig (no /dev/nvidia0).", test_ms: null },
    { id: "intel", label: "Intel iGPU (Quick Sync)", encoder: "h264_qsv", about: "", available: false, reason: "No GPU render device visible to the rig (no /dev/dri/renderD*).", test_ms: null },
    { id: "vaapi", label: "iGPU / GPU via VAAPI", encoder: "h264_vaapi", about: "Generic.", available: false, reason: "No GPU render device visible to the rig (no /dev/dri/renderD*).", test_ms: null },
  ],
  ...over,
});

describe("Video encoding card", () => {
  it("shows a machine without GPU: CPU in use, the others with the reason", async () => {
    mockFetch((url) => {
      if (url.endsWith("/api/system/encoders")) return encoders();
      if (url.endsWith("/api/health/test/last")) return { full: null, quick: null, auto_interval_s: 0 };
      if (url.endsWith("/api/settings/capacity")) return capacity;
      return {};
    });
    render(<ToastProvider><HealthPage /></ToastProvider>);
    expect(await screen.findByTestId("encoder-active")).toHaveTextContent("CPU (libx264)");
    expect(screen.getByText(/No usable GPU on this machine/)).toBeInTheDocument();
    expect(screen.getByTestId("encoder-nvidia")).toHaveTextContent("NO");
    expect(screen.getByTestId("encoder-nvidia")).toHaveTextContent("/dev/nvidia0");
    expect(screen.getByTestId("encoder-cpu")).toHaveTextContent("WORKS");
    expect(screen.getByRole("option", { name: "NVIDIA GPU (not available here)" })).toBeInTheDocument();
  });

  it("saves a choice and shows the fallback note", async () => {
    const calls = mockFetch((url, init) => {
      if (url.endsWith("/api/system/encoders") && init?.method === "PUT") {
        return encoders({ choice: "nvidia", overridden: true, note: "NVIDIA GPU (NVENC) was chosen but doesn't work here; using CPU (libx264)." });
      }
      if (url.endsWith("/api/system/encoders")) return encoders();
      if (url.endsWith("/api/health/test/last")) return { full: null, quick: null, auto_interval_s: 0 };
      if (url.endsWith("/api/settings/capacity")) return capacity;
      return {};
    });
    render(<ToastProvider><HealthPage /></ToastProvider>);
    await userEvent.selectOptions(await screen.findByLabelText("Use"), "nvidia");
    expect(await screen.findByText(/was chosen but doesn't work here/)).toBeInTheDocument();
    const put = calls.mock.calls.find(([u, i]) => String(u).endsWith("/api/system/encoders") && i?.method === "PUT");
    expect(JSON.parse(String(put?.[1]?.body))).toEqual({ choice: "nvidia" });
    expect(screen.getByRole("button", { name: "reset" })).toBeInTheDocument();
  });

  it("uses the GPU automatically when one works", async () => {
    const e = encoders({ active: "intel", active_label: "Intel iGPU (Quick Sync)" });
    (e.encoders as { id: string; available: boolean }[])[2].available = true;
    mockFetch((url) => {
      if (url.endsWith("/api/system/encoders")) return e;
      if (url.endsWith("/api/health/test/last")) return { full: null, quick: null, auto_interval_s: 0 };
      if (url.endsWith("/api/settings/capacity")) return capacity;
      return {};
    });
    render(<ToastProvider><HealthPage /></ToastProvider>);
    expect(await screen.findByTestId("encoder-active")).toHaveTextContent("Intel iGPU");
    expect(screen.queryByText(/No usable GPU/)).not.toBeInTheDocument();
    expect(screen.getByTestId("encoder-intel")).toHaveTextContent("in use");
  });
});
