import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "./common";
import { RigDataProvider } from "../hooks/useRigData";
import { StreamsTable } from "./StreamsTable";
import { useConversionSummary } from "./AppShell";
import { UploadQueueProvider } from "./UploadQueue";
import { VideoLibrary } from "./VideoLibrary";
import { OverviewPage } from "../pages/OverviewPage";
import {
  buildEdgeCsv, conversionSummary, csvField, edgeCsvFilename, frameHealth, keyframeHint, readerLines, stripPort,
} from "../lib/edge";
import type { AppConfig, Conversion, Reader, Stream, User, Video } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = {
  version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554,
  output: { width: 1280, height: 720, default: "standard", sizes: [], require_16_9: true },
} as unknown as AppConfig;

const NOW = Date.parse("2026-11-19T14:30:40Z");
const ago = (s: number) => new Date(NOW - s * 1000).toISOString();

function mkStream(extra: Record<string, unknown> = {}, progress: Record<string, unknown> | null = {}): Stream {
  return {
    id: 1, name: "cam-a", rtsp_url: "rtsp://rig02:8554/cam-a", status: "RUNNING", desired_state: "RUNNING", slot_id: null,
    video_id: 7, source_rel_path: "gate.mp4", note: null, owner: null, protected: false, playback_mode: "loop",
    effective_start_s: 0, end_offset_s: null, output_mode: "copy", output_resolution: "1280x720", version: 1,
    last_stop_at: null, last_start_at: null, running_since: ago(60),
    runtime: {
      viewer_count: 0, viewer_peak_today: 0, preview_watchers: 0, loop_count: 0, readers: [],
      progress: progress && { frame: 100, fps: 25, out_time_s: 4, speed: 1, drop_frames: 0, dup_frames: 0, updated_at: ago(1), ...progress },
    },
    ...extra,
  } as unknown as Stream;
}

const reader = (addr: string, since: string, transport = "TCP"): Reader =>
  ({ type: "rtspSession", id: addr, remote_addr: addr, since, bytes_sent: 1, transport });

const video = {
  id: 7, rel_path: "ALPR/gate, north.mp4", filename: "gate, north.mp4", fps: 25, width: 1920, height: 1080,
  video_codec: "h264", keyframe_interval_s: 4.16, duration_s: 90,
} as unknown as Video;

describe("frameHealth", () => {
  it("is null when the stream is not running or has no progress", () => {
    expect(frameHealth(mkStream({ status: "STOPPED" }), 25, NOW)).toBeNull();
    expect(frameHealth(mkStream({}, null), 25, NOW)).toBeNull();
  });

  it("is healthy at full fps, real-time speed and a fresh report", () => {
    const h = frameHealth(mkStream({}, { fps: 24.6 }), 25, NOW)!;
    expect(h.state).toBe("healthy");
    expect(h.fps).toBe(24.6);
  });

  it("is slow below 90% of the video's fps or below 0.97x speed", () => {
    const low = frameHealth(mkStream({}, { fps: 20 }), 25, NOW)!;
    expect(low.state).toBe("slow");
    expect(low.reason).toMatch(/slower than real time/);
    expect(low.reason).toMatch(/20.0 fps of 25 expected/);
    const lag = frameHealth(mkStream({}, { speed: 0.9 }), 25, NOW)!;
    expect(lag.state).toBe("slow");
    expect(lag.reason).toMatch(/speed 0.90x/);
  });

  it("skips the fps comparison when the video's fps is unknown", () => {
    expect(frameHealth(mkStream({}, { fps: 5 }), null, NOW)!.state).toBe("healthy");
    expect(frameHealth(mkStream({}, { fps: 5, speed: 0.5 }), undefined, NOW)!.state).toBe("slow");
  });

  it("is frozen when the last progress report is older than 10 s", () => {
    const h = frameHealth(mkStream({}, { updated_at: ago(23) }), 25, NOW)!;
    expect(h.state).toBe("frozen");
    expect(h.reason).toMatch(/No new frames for 23 s/);
    expect(frameHealth(mkStream({}, { updated_at: ago(9) }), 25, NOW)!.state).toBe("healthy");
  });
});

describe("readers", () => {
  it("strips ports from IPv4 and bracketed IPv6", () => {
    expect(stripPort("192.168.29.109:58588")).toBe("192.168.29.109");
    expect(stripPort("[fe80::1]:554")).toBe("fe80::1");
    expect(stripPort("fe80::1")).toBe("fe80::1");
  });

  it("lists each reader and groups duplicates with the earliest start", () => {
    const t1 = "2026-11-19T09:05:00Z", t2 = "2026-11-19T10:00:00Z";
    const hm = (iso: string) => { const d = new Date(iso); return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`; };
    const lines = readerLines([
      reader("10.0.0.5:1000", t2), reader("10.0.0.5:1001", t1), reader("10.0.0.5:1002", t2), reader("10.0.0.5:1003", t2),
      reader("10.0.0.9:2000", t2, "UDP"),
    ]);
    expect(lines).toEqual([`10.0.0.5 ×4 · TCP · since ${hm(t1)}`, `10.0.0.9 · UDP · since ${hm(t2)}`]);
    expect(readerLines(undefined)).toEqual([]);
  });
});

describe("edge CSV", () => {
  it("quotes fields with commas, quotes or line breaks", () => {
    expect(csvField("plain")).toBe("plain");
    expect(csvField('a "b", c')).toBe('"a ""b"", c"');
    expect(csvField("x\ny")).toBe('"x\ny"');
    expect(csvField(null)).toBe("");
  });

  it("builds one row per stream with the video's fps and keyframe interval", () => {
    const csv = buildEdgeCsv(
      [mkStream({ id: 2, name: "cam-10" }), mkStream({ id: 3, name: "cam-2", video_id: 99, source_rel_path: "x.mp4", status: "STOPPED", output_resolution: null })],
      [video], (s) => `rtsp://10.0.0.1:8554/${s.name}`,
    );
    expect(csv.split("\r\n")).toEqual([
      "stream,rtsp_url,status,resolution,fps,codec,keyframe_s,video",
      "cam-2,rtsp://10.0.0.1:8554/cam-2,STOPPED,,,h264,,x.mp4",
      'cam-10,rtsp://10.0.0.1:8554/cam-10,RUNNING,1280x720,25.00,h264,4.2,"ALPR/gate, north.mp4"',
      "",
    ]);
  });

  it("names the file after the analytics code, else a slug of its name", () => {
    expect(edgeCsvFilename("VehicleALPR", "ALPR")).toBe("ALPR-streams.csv");
    expect(edgeCsvFilename("Not in any analytics")).toBe("not-in-any-analytics-streams.csv");
  });
});

describe("conversion summary and keyframe hint", () => {
  const job = (state: Conversion["state"], progress = 0, eta_s: number | null = null) =>
    ({ id: state, video_id: 1, source: `${state}.mp4`, target: `${state} (h264).mp4`, state, progress, eta_s, error: null }) as Conversion;

  it("is null when nothing is queued or running", () => {
    expect(conversionSummary([job("done", 1), job("failed")])).toBeNull();
    expect(conversionSummary(null)).toBeNull();
    expect(conversionSummary({} as unknown as Conversion[])).toBeNull();
  });

  it("counts queued and running jobs and shows the running one's progress and ETA", () => {
    const s = conversionSummary([job("queued"), job("running", 0.454, 130), job("done", 1)])!;
    expect(s.active).toBe(2);
    expect(s.percent).toBe(45);
    expect(s.title).toMatch(/running.mp4 → running \(h264\).mp4 · 45% · ETA 2m 10s/);
    expect(s.title).toMatch(/queued.mp4 → queued \(h264\).mp4 · queued/);
    expect(conversionSummary([job("queued")])!.percent).toBeNull();
  });

  it("hints at long keyframe gaps only above 2.5 s", () => {
    expect(keyframeHint({ keyframe_interval_s: 2.5 })).toBeNull();
    expect(keyframeHint({ keyframe_interval_s: null })).toBeNull();
    const h = keyframeHint({ keyframe_interval_s: 10 })!;
    expect(h.text).toBe("Keyframe every 10 s");
    expect(h.title).toBe("Edges wait up to 10 s for the first picture after connecting. Make a camera copy (keyframe every 2 s).");
    expect(keyframeHint({ keyframe_interval_s: 4.16 })!.text).toBe("Keyframe every 4.2 s");
  });
});

// ------------------------------------------------------------------ components
function mockFetch(routes: Record<string, unknown>, fail: string[] = []) {
  const fn = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url).split("?")[0];
    if (fail.includes(path)) return new Response("{}", { status: 500 });
    const body = path in routes ? routes[path] : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  });
  globalThis.fetch = fn as unknown as typeof fetch;
  return fn;
}

function wrap(ui: React.ReactNode) {
  return render(
    <ToastProvider><MemoryRouter><RigDataProvider user={admin} config={config}>
      <UploadQueueProvider onUploaded={() => {}}>{ui}</UploadQueueProvider>
    </RigDataProvider></MemoryRouter></ToastProvider>,
  );
}

describe("stream cards and rows", () => {
  beforeEach(() => localStorage.clear());
  const fresh = () => new Date(Date.now() - 1000).toISOString();
  const cards = () => [
    mkStream({ id: 1, name: "cam-a", runtime: { ...mkStream().runtime, viewer_count: 3,
      readers: [reader("10.0.0.5:1", ago(30)), reader("10.0.0.5:2", ago(30)), reader("10.0.0.7:3", ago(30))],
      progress: { ...mkStream().runtime.progress!, updated_at: fresh() } } }),
    mkStream({ id: 2, name: "cam-b" }, { updated_at: new Date(Date.now() - 30000).toISOString() }),
    mkStream({ id: 3, name: "cam-c", status: "STOPPED" }, null),
  ];

  it("shows a frame-health chip on running cards and readers in the viewer tooltip", async () => {
    mockFetch({ "/api/videos": [video] });
    wrap(<StreamsTable streams={cards()} user={admin} onSelect={() => {}} onChanged={() => {}} mediamtxDown={false} />);
    await waitFor(() => expect(screen.getAllByRole("region").length).toBeGreaterThan(0)); // grouped once analytics load
    const a = screen.getByRole("article", { name: "Stream cam-a, RUNNING" });
    expect(within(a).getByTestId("frame-health")).toHaveTextContent("25.0 fps");
    expect(within(a).getByTestId("frame-health")).toHaveAttribute("data-state", "healthy");
    expect(within(a).getByTestId("viewers").getAttribute("title")).toMatch(/10\.0\.0\.5 ×2 · TCP · since \d\d:\d\d\n10\.0\.0\.7 · TCP/);
    const b = screen.getByRole("article", { name: "Stream cam-b, RUNNING" });
    expect(within(b).getByTestId("frame-health")).toHaveAttribute("data-state", "frozen");
    expect(within(b).getByTitle(/No new frames for 30 s/)).toBeInTheDocument();
    const c = screen.getByRole("article", { name: "Stream cam-c, STOPPED" });
    expect(within(c).queryByTestId("frame-health")).toBeNull();
  });

  it("shows the same chip and tooltip in the table view", async () => {
    localStorage.setItem("rig02.streams.view", "table");
    mockFetch({ "/api/videos": [{ ...video, fps: 30 }] });
    wrap(<StreamsTable streams={cards()} user={admin} onSelect={() => {}} onChanged={() => {}} mediamtxDown={false} />);
    const row = () => screen.getByRole("row", { name: /Stream cam-a, RUNNING/ }); // re-query: sections appear once analytics load
    // 25 fps from a 30 fps video: slow
    await waitFor(() => expect(within(row()).getByTestId("frame-health")).toHaveAttribute("data-state", "slow"));
    expect(within(row()).getByTestId("frame-health")).toHaveTextContent("25.0 fps · slow");
    expect(within(row()).getByTestId("viewers").getAttribute("title")).toMatch(/10\.0\.0\.5 ×2/);
  });
});

function ConvProbe() {
  const s = useConversionSummary(50, 50);
  return <div data-testid="conv">{s ? `${s.active} · ${s.percent}%` : "none"}</div>;
}

describe("conversion queue polling", () => {
  it("summarises active jobs and survives errors", async () => {
    const fn = mockFetch({ "/api/videos/conversions": [
      { id: "a", video_id: 1, source: "a.mp4", target: "a (h264).mp4", state: "running", progress: 0.45, eta_s: 60, error: null },
    ] });
    render(<ConvProbe />);
    await waitFor(() => expect(screen.getByTestId("conv")).toHaveTextContent("1 · 45%"));
    mockFetch({}, ["/api/videos/conversions"]);
    await act(async () => { await new Promise((r) => setTimeout(r, 120)); });
    expect(screen.getByTestId("conv")).toHaveTextContent("1 · 45%"); // last state kept on errors
    mockFetch({ "/api/videos/conversions": [] });
    await waitFor(() => expect(screen.getByTestId("conv")).toHaveTextContent("none"));
    expect(fn).toHaveBeenCalled();
  });
});

describe("library keyframe hint", () => {
  it("shows an amber chip only for videos with keyframes further apart than 2.5 s", async () => {
    mockFetch({});
    const base = { ...video, status: "READY", size_bytes: 1e6, display_width: 1920, display_height: 1080, shape: "16:9",
      is_16_9: true, compat_mode: "copy", compat_reasons: [], compat_warnings: [], thumbnail_url: null, streams_using: 0,
      analytics: [], updated_at: "2026-11-01T00:00:00Z" } as unknown as Video;
    wrap(<VideoLibrary videos={[{ ...base, id: 1, rel_path: "slow.mp4", filename: "slow.mp4", keyframe_interval_s: 4.16 },
      { ...base, id: 2, rel_path: "ok.mp4", filename: "ok.mp4", keyframe_interval_s: 2 }]}
      loading={false} user={admin} onChanged={() => {}} onCreateStream={() => {}} />);
    const chips = await screen.findAllByTestId("problem-chip");
    expect(chips).toHaveLength(1);
    expect(chips[0]).toHaveTextContent("Keyframe every 4.2 s");
    expect(chips[0].getAttribute("title")).toMatch(/edges wait up to 4.2 s/);
  });
});

describe("export for edge", () => {
  it("downloads a CSV of an analytics' streams named after its code", async () => {
    const tree = [{ id: 1, name: "Traffic", sort: 0, analytics: [
      { id: 4, name: "VehicleALPR", code: "ALPR", description: null, sort: 0, slots: 1, slot_ids: [9], streams: 1, running: 1, problems: 0, viewers: 0 },
    ] }];
    mockFetch({ "/api/analytics": tree, "/api/streams": [mkStream({ slot_id: 9, name: "alpr-1" })],
      "/api/videos": [{ ...video, analytics: [{ id: 4, name: "VehicleALPR", slot_id: 9 }] }] });
    const blobs: Blob[] = [];
    const create = vi.fn((b: Blob) => { blobs.push(b); return "blob:x"; });
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: vi.fn() });
    let saved = "";
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { saved = this.download; });
    const u = userEvent.setup();
    wrap(<OverviewPage />);
    const tile = await screen.findByRole("article", { name: "Analytics VehicleALPR" });
    await u.click(await within(tile).findByRole("button", { name: "More for VehicleALPR" }));
    await u.click(screen.getByRole("menuitem", { name: /Export VehicleALPR for edge/ }));
    expect(saved).toBe("ALPR-streams.csv");
    const text = await new Promise<string>((res) => { const r = new FileReader(); r.onload = () => res(String(r.result)); r.readAsText(blobs[0]); });
    expect(text).toMatch(/^stream,rtsp_url,status,resolution,fps,codec,keyframe_s,video\r\n/);
    expect(text).toMatch(/alpr-1,rtsp:\/\/[^,]+\/cam-a,RUNNING,1280x720,25\.00,h264,4\.2,"ALPR\/gate, north\.mp4"/);
    click.mockRestore();
  });
});
