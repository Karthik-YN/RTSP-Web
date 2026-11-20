import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CreateStreamDialog } from "./CreateStreamDialog";
import { StatusBadge, ToastProvider } from "./common";
import type { AppConfig, Video } from "../types/api";

const config = {
  version: "1.0.0", public_rtsp_host: "rig02", public_rtsp_port: 8554, rtsp_url_template: "rtsp://rig02:8554/{name}",
  stream_name_pattern: "^[A-Za-z0-9_-]{1,64}$", reserved_names: ["all"], max_active_streams: 20,
  transcode_policy: "auto", min_segment_s: 1, allowed_extensions: [".mp4"], upload_max_bytes: 1e12,
  auth_enabled: false, ffmpeg_version: null, warnings: [],
} as AppConfig;

const video = {
  id: 1, rel_path: "store_cam.mp4", filename: "store_cam.mp4", size_bytes: 1000, status: "READY", error: null,
  container: "mp4", duration_s: 600, width: 1920, height: 1080, fps: 25, fps_text: "25/1", is_vfr: false,
  video_codec: "h264", video_profile: "High", pix_fmt: "yuv420p", bit_depth: 8, audio_codec: null, bitrate_bps: 4e6,
  keyframe_interval_s: 2, compat_mode: "copy", compat_reasons: [], compat_warnings: [], thumbnail_url: null,
  streams_using: 0, updated_at: "", last_seen_at: "",
} as Video;

function mockFetch(handler: (url: string, init?: RequestInit) => unknown) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const body = handler(String(url), init);
    const status = (body as { __status?: number })?.__status ?? 200;
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

const okValidation = {
  ok: true, errors: [], start_s: 125, effective_start_s: 124, end_s: null, effective_end_s: 600, period_s: 476,
  output_mode: "copy", warnings: [], rtsp_url: "rtsp://rig02:8554/store_cam",
};

describe("StatusBadge", () => {
  it("shows the status text", () => {
    render(<StatusBadge status="RUNNING" />);
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.getByTitle("RUNNING")).toBeInTheDocument();
  });
});

describe("CreateStreamDialog", () => {
  it("suggests a name, validates locally and shows the server preview", async () => {
    mockFetch((url) => (url.includes("/validate") ? okValidation : {}));
    const user = userEvent.setup();
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    const name = screen.getByLabelText(/Stream name/i) as HTMLInputElement;
    expect(name.value).toBe("store_cam");

    await user.clear(name);
    await user.type(name, "bad name");
    expect(screen.getByText(/No spaces allowed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create/ })).toBeDisabled();

    await user.clear(name);
    await user.type(name, "cam1");
    await user.type(screen.getByLabelText("Start time"), "00:02:05");
    expect(screen.getByText("= 00:02:05")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("preview")).toHaveTextContent("00:02:04"), { timeout: 3000 });
    expect(screen.getByText(/aligned to the nearest earlier keyframe/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create/ })).toBeEnabled();
  });

  it("rejects an end before the start without asking the server", async () => {
    mockFetch(() => okValidation);
    const user = userEvent.setup();
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    await user.type(screen.getByLabelText("Start time"), "00:05:00");
    await user.type(screen.getByLabelText("End time"), "00:04:00");
    expect(screen.getByText("End must be after start.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Start time"));
    await user.type(screen.getByLabelText("Start time"), "00:11:00");
    expect(screen.getByText(/beyond the end of the file/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Create/ })).toBeDisabled();
  });

  it("shows the server's duplicate-name error and the URL after creation", async () => {
    let created = false;
    mockFetch((url, init) => {
      if (url.includes("/validate")) return okValidation;
      if (url === "/api/streams" && init?.method === "POST") {
        created = true;
        return { id: 9, name: "cam2", rtsp_url: "rtsp://rig02:8554/cam2" };
      }
      return {};
    });
    const user = userEvent.setup();
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    await waitFor(() => expect(screen.getByRole("button", { name: /Create/ })).toBeEnabled(), { timeout: 3000 });
    await user.click(screen.getByRole("button", { name: /Create/ }));
    await waitFor(() => expect(screen.getByTestId("created-url")).toHaveTextContent("rtsp://rig02:8554/cam2"));
    expect(created).toBe(true);
  });

  it("offers the server clock explicitly (off by default), with IST or UTC", async () => {
    let body: Record<string, unknown> | null = null;
    mockFetch((url, init) => {
      if (url.includes("/validate")) return okValidation;
      if (url === "/api/streams" && init?.method === "POST") { body = JSON.parse(String(init.body)); return { id: 9, name: "cam2", rtsp_url: "rtsp://rig02:8554/cam2" }; }
      return {};
    });
    const user = userEvent.setup();
    const withClock = { ...config, features: ["clock_overlay"] } as typeof config;
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={withClock} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    // both choices are visible at once: clock yes/no, and the time standard (greyed out while "No")
    const yes = screen.getByRole("radio", { name: /Yes: Server clock in picture/ });
    expect(screen.getByRole("radio", { name: /No: send the video as it is/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /IST/ })).toBeDisabled();
    await user.click(yes);
    expect(screen.getByRole("radio", { name: /IST/ })).toBeChecked(); // IST unless UTC is picked
    expect(screen.getByRole("radio", { name: "UTC" })).toBeEnabled();
    await user.click(screen.getByRole("radio", { name: "UTC" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Create/ })).toBeEnabled(), { timeout: 3000 });
    await user.click(screen.getByRole("button", { name: /Create/ }));
    await waitFor(() => expect(body).toMatchObject({ clock: true, clock_tz: "UTC" }));
  });

  it("hides the clock option on servers that don't have it", () => {
    mockFetch(() => okValidation);
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    expect(screen.queryByTestId("clock-field")).toBeNull();
  });

  it("blocks names already used by existing streams", async () => {
    mockFetch(() => okValidation);
    const user = userEvent.setup();
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[{ name: "Taken" } as never]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    const name = screen.getByLabelText(/Stream name/i);
    await user.clear(name);
    await user.type(name, "taken");
    expect(screen.getByText(/already in use/)).toBeInTheDocument();
  });

  it("warns about a long keyframe interval and offers a fast-start re-encode", async () => {
    let body: Record<string, unknown> = {};
    mockFetch((url, init) => {
      if (init?.method === "POST" && url.includes("/validate")) body = JSON.parse(String(init.body));
      return okValidation;
    });
    const user = userEvent.setup();
    const slow = { ...video, keyframe_interval_s: 10.4 } as Video;
    render(<ToastProvider><CreateStreamDialog videos={[slow]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    expect(screen.getByTestId("long-gop")).toHaveTextContent("black screen for up to 11 s");
    await user.click(screen.getByLabelText(/Re-encode for a fast start/));
    await waitFor(() => expect(body.force_transcode).toBe(true));
  });

  it("does not warn for a normal keyframe interval", () => {
    mockFetch(() => okValidation);
    render(<ToastProvider><CreateStreamDialog videos={[video]} streams={[]} config={config} onClose={() => {}} onCreated={() => {}} /></ToastProvider>);
    expect(screen.queryByTestId("long-gop")).toBeNull();
  });
});
