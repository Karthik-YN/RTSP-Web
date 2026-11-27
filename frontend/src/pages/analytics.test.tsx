import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { quickHash } from "../lib/contentHash";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { OverviewPage } from "./OverviewPage";
import { AssignDialog, filterByAnalytics, SuggestionsDialog } from "../components/LibraryAnalytics";
import { ToastProvider } from "../components/common";
import { UploadQueueProvider } from "../components/UploadQueue";
import { RigDataProvider } from "../hooks/useRigData";
import type { AppConfig, Stream, User, Video } from "../types/api";
import { AnalyticsPage } from "./AnalyticsPage";

const user: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = { version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554 } as AppConfig;

const tree = [
  { id: 1, name: "Traffic", sort: 0, analytics: [
    { id: 11, name: "videoANPR", description: null, sort: 0, slots: 1, slot_ids: [5], streams: 3, running: 2, problems: 1, viewers: 1 },
    { id: 12, name: "VehicleDwell", description: null, sort: 1, slots: 0, slot_ids: [], streams: 0, running: 0, problems: 0, viewers: 0 },
  ] },
  { id: 2, name: "People", sort: 1, analytics: [
    { id: 21, name: "PPE", description: null, sort: 0, slots: 0, slot_ids: [], streams: 0, running: 0, problems: 0, viewers: 0 },
  ] },
];

const video = {
  id: 7, rel_path: "gate.mp4", filename: "gate.mp4", size_bytes: 5e6, status: "READY", duration_s: 90, width: 1920,
  height: 1080, fps: 25, video_codec: "h264", compat_mode: "copy", compat_reasons: [], thumbnail_url: null,
} as unknown as Video;

const stream = {
  id: 3, name: "ppe-1", rtsp_url: "rtsp://rig02:8554/ppe-1", status: "RUNNING", desired_state: "RUNNING", slot_id: 9,
  last_stop_at: null, runtime: { viewer_count: 2, viewer_peak_today: 3, preview_watchers: 1 },
} as unknown as Stream;

const detail = {
  id: 21, name: "PPE", description: "Helmet and vest checks", category: { id: 2, name: "People" },
  streams: 1, running: 1, problems: 0, viewers: 2,
  slots: [
    { id: 8, analytics_id: 21, title: "Empty slot", description: null, sort: 0, video: null, streams: [] },
    { id: 9, analytics_id: 21, title: "Warehouse gate", description: "Day shift, 6 workers", sort: 1, video, streams: [stream] },
  ],
};

function mockFetch(routes: Record<string, unknown>) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url).split("?")[0];
    const body = path in routes ? routes[path] : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

function wrap(ui: React.ReactNode, at = "/", cfg: AppConfig = config) {
  return render(
    <ToastProvider>
      <MemoryRouter initialEntries={[at]}>
        <RigDataProvider user={user} config={cfg}>
          <UploadQueueProvider onUploaded={() => {}}>{ui}</UploadQueueProvider>
        </RigDataProvider>
      </MemoryRouter>
    </ToastProvider>,
  );
}

// streams of videoANPR (slot 5) and one stream that is in no analytics
const anprLive = { id: 31, name: "anpr-1", status: "RUNNING", desired_state: "RUNNING", slot_id: 5, video_id: 7,
  source_rel_path: "gate.mp4", runtime: { viewer_count: 1 } } as unknown as Stream;
const anprDown = { id: 32, name: "anpr-2", status: "ERROR", desired_state: "RUNNING", slot_id: 5, video_id: 7,
  source_rel_path: "gate.mp4", runtime: { viewer_count: 0 } } as unknown as Stream;
const loose = { id: 33, name: "old-cam", status: "STOPPED", desired_state: "STOPPED", slot_id: null, video_id: 8,
  source_rel_path: "old.mp4", runtime: { viewer_count: 0 } } as unknown as Stream;
const inAnpr = { ...video, analytics: [{ id: 11, name: "videoANPR", slot_id: 5 }] } as unknown as Video;
const unassigned = { ...video, id: 8, filename: "old.mp4", rel_path: "old.mp4", analytics: [] } as unknown as Video;
const overviewRoutes = { "/api/analytics": tree, "/api/streams": [anprLive, anprDown, loose], "/api/videos": [inAnpr, unassigned] };

describe("Overview (main page)", () => {
  beforeEach(() => localStorage.clear());

  it("shows each category as a section with its analytics, counts and problems", async () => {
    mockFetch(overviewRoutes);
    wrap(<OverviewPage />);
    const tile = await screen.findByRole("article", { name: "Analytics videoANPR" });
    expect(within(tile).getByText("1/2")).toBeInTheDocument(); // live/total
    expect(within(tile).getByText("1 video")).toBeInTheDocument();
    expect(within(tile).getByTitle("1 stream(s) need attention")).toBeInTheDocument();
    expect(within(tile).getByRole("link", { name: "Open" })).toHaveAttribute("href", "/analytics/11");
    // analytics without videos or streams fold into one line instead of empty tiles
    expect(screen.queryByRole("article", { name: "Analytics VehicleDwell" })).toBeNull();
    expect(screen.getByText(/1 empty analytics: VehicleDwell/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "People" })).toBeInTheDocument();
    expect(screen.getByText("1 need attention")).toBeInTheDocument();
  });

  it("finds analytics by name, code or video, and filters by category", async () => {
    mockFetch(overviewRoutes);
    const u = userEvent.setup();
    wrap(<OverviewPage />);
    await screen.findByRole("article", { name: "Analytics videoANPR" });
    await u.type(screen.getByLabelText("Find analytics, video or stream"), "gate.mp4");
    expect(screen.getByRole("article", { name: "Analytics videoANPR" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "People" })).toBeNull();
    await u.clear(screen.getByLabelText("Find analytics, video or stream"));
    await u.click(screen.getByRole("tab", { name: "People" }));
    expect(screen.queryByRole("article", { name: "Analytics videoANPR" })).toBeNull();
    expect(screen.getByText(/1 empty analytics: PPE/)).toBeInTheDocument();
  });

  it("folds a category and remembers it; lists what is in no analytics", async () => {
    mockFetch(overviewRoutes);
    const u = userEvent.setup();
    const { unmount } = wrap(<OverviewPage />);
    await screen.findByRole("article", { name: "Analytics videoANPR" });
    await u.click(screen.getByRole("button", { name: "Traffic" }));
    expect(screen.queryByRole("article", { name: "Analytics videoANPR" })).toBeNull();
    // "Not in any analytics" starts folded
    const none = screen.getByRole("button", { name: "Not in any analytics" });
    expect(screen.getByText("1 videos, 1 streams")).toBeInTheDocument();
    await u.click(none);
    expect(screen.getByRole("link", { name: /old-cam/ })).toHaveAttribute("href", "/streams/33");
    expect(screen.getByRole("link", { name: /1 videos in the library/ })).toHaveAttribute("href", "/library?analytics=none");
    unmount();
    wrap(<OverviewPage />);
    await screen.findByRole("button", { name: "Traffic" });
    expect(screen.queryByRole("article", { name: "Analytics videoANPR" })).toBeNull(); // still folded
  });

  it("starts all stopped streams of a category with one click", async () => {
    mockFetch({ ...overviewRoutes, "/api/streams/bulk": { ok: 1, failed: 0, results: [{ id: 32, ok: true }] } });
    const u = userEvent.setup();
    wrap(<OverviewPage />);
    const tile = await screen.findByRole("article", { name: "Analytics videoANPR" });
    await u.click(within(tile).getByRole("button", { name: /Start 1/ }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith("/api/streams/bulk",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ stream_ids: [32], action: "start" }) })));
  });

  it("jumps to the search with /", async () => {
    mockFetch(overviewRoutes);
    const u = userEvent.setup();
    wrap(<OverviewPage />);
    await screen.findByRole("article", { name: "Analytics videoANPR" });
    await u.keyboard("/");
    expect(screen.getByLabelText("Find analytics, video or stream")).toHaveFocus();
    expect(screen.getByLabelText("Find analytics, video or stream")).toHaveValue("");
  });
});

describe("AnalyticsPage", () => {
  it("shows placeholders, their streams and viewers, and suggests the next camera names", async () => {
    mockFetch({ "/api/analytics": tree, "/api/analytics/21": detail, "/api/streams": [stream] });
    const u = userEvent.setup();
    wrap(<Routes><Route path="/analytics/:id" element={<AnalyticsPage />} /></Routes>, "/analytics/21");
    expect(await screen.findByRole("heading", { name: "PPE" })).toBeInTheDocument();
    expect(screen.getByText("Helmet and vest checks")).toBeInTheDocument();
    expect(screen.getByText("Day shift, 6 workers")).toBeInTheDocument();
    // upload straight from the page; no "placeholder" step any more
    expect(screen.getByRole("button", { name: "Upload video" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Add from shared library/ })).toBeInTheDocument();
    expect(screen.queryByText(/Add video placeholder/)).not.toBeInTheDocument();
    // an older empty card still offers upload and library
    expect(screen.getByRole("button", { name: /Upload from this device/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Choose from library/ })).toBeInTheDocument();
    // stream row: name, viewers (peak in the title), preview watchers separately
    const row = screen.getByRole("link", { name: "ppe-1" }).closest("li")!;
    expect(within(row).getByTitle(/2 edge\/RTSP viewer\(s\) now · peak today 3/)).toBeInTheDocument();
    expect(within(row).getByTitle(/Watching the preview/)).toHaveTextContent("1");

    await u.click(screen.getByRole("button", { name: /^New stream$/ }));
    const dialog = await screen.findByRole("dialog");
    const count = within(dialog).getByLabelText("Number of cameras");
    await u.clear(count);
    await u.type(count, "3");
    await waitFor(() => expect(within(dialog).getByText("ppe-4")).toBeInTheDocument());
    expect(within(dialog).getByText("ppe-2")).toBeInTheDocument(); // ppe-1 is taken
    expect(within(dialog).queryByText("ppe-1")).not.toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /Create 3 streams/ })).toBeEnabled();
  });
});

describe("AnalyticsPage uploads", () => {
  const libVideo = { ...video, id: 40, rel_path: "crowd.mp4", filename: "crowd.mp4", size_bytes: 1234 } as Video;
  const empty = { ...detail, slots: [], streams: 0, running: 0, viewers: 0 };

  it("adds a video from the shared library to this analytics", async () => {
    mockFetch({ "/api/analytics": tree, "/api/analytics/21": empty, "/api/streams": [], "/api/videos": [libVideo, video],
      "/api/analytics/assign": { created: [{ slot_id: 50, analytics_id: 21, analytics_name: "PPE", video_id: 40 }], skipped: 0 } });
    const u = userEvent.setup();
    wrap(<Routes><Route path="/analytics/:id" element={<AnalyticsPage />} /></Routes>, "/analytics/21");
    expect(await screen.findByTestId("analytics-empty")).toHaveTextContent("No videos for PPE yet");
    await u.click(screen.getAllByRole("button", { name: /Add from shared library/ })[0]);
    const dialog = await screen.findByRole("dialog");
    await u.click(await within(dialog).findByText("crowd.mp4"));
    await waitFor(() => expect(posts("/api/analytics/assign")).toEqual([{ video_ids: [40], analytics_ids: [21] }]));
  });

  it("a dropped file whose CONTENT is already in the library is added without uploading, whatever its name", async () => {
    const bytes = new Uint8Array(1234).map((_, i) => i % 251);
    const sameContent = { ...libVideo, filename: "totally-different-name.mp4", rel_path: "totally-different-name.mp4",
      original_name: null, size_bytes: 1234, quick_hash: await quickHash(new Blob([bytes])) };
    mockFetch({ "/api/analytics": tree, "/api/analytics/21": empty, "/api/streams": [], "/api/videos": [sameContent],
      "/api/analytics/assign": { created: [{ slot_id: 51, analytics_id: 21, analytics_name: "PPE", video_id: 40 }], skipped: 0 } });
    const u = userEvent.setup();
    wrap(<Routes><Route path="/analytics/:id" element={<AnalyticsPage />} /></Routes>, "/analytics/21");
    await screen.findByTestId("analytics-empty");
    await waitFor(() => expect((globalThis.fetch as unknown as { mock: { calls: [string][] } }).mock.calls
      .some(([url]) => String(url).startsWith("/api/videos"))).toBe(true));
    await new Promise((r) => setTimeout(r, 50)); // let the library list arrive
    const file = new File([bytes], "crowd.mp4", { type: "video/mp4" });
    await u.upload(screen.getByTestId("analytics-upload-input"), file);
    await waitFor(() => expect(posts("/api/analytics/assign")).toEqual([{ video_ids: [40], analytics_ids: [21] }]));
    expect(await screen.findByText(/Duplicate video: 'crowd.mp4' is already in the library as 'totally-different-name.mp4'/)).toBeInTheDocument();
    expect(posts("/api/videos/uploads")).toEqual([]);
  });

  it("hides upload for viewers", async () => {
    mockFetch({ "/api/analytics": tree, "/api/analytics/21": empty, "/api/streams": [] });
    const viewer: User = { username: "v", role: "viewer", can_write: false, is_admin: false, auth_enabled: true };
    render(
      <ToastProvider><MemoryRouter initialEntries={["/analytics/21"]}>
        <RigDataProvider user={viewer} config={config}><UploadQueueProvider onUploaded={() => {}}>
          <Routes><Route path="/analytics/:id" element={<AnalyticsPage />} /></Routes>
        </UploadQueueProvider></RigDataProvider>
      </MemoryRouter></ToastProvider>,
    );
    expect(await screen.findByTestId("analytics-empty")).toHaveTextContent("An operator or admin can upload");
    expect(screen.queryByRole("button", { name: "Upload video" })).not.toBeInTheDocument();
  });
});

function posts(path: string) {
  return (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
    .filter(([u, init]) => String(u) === path && init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init!.body)));
}

describe("Library and analytics", () => {
  const linked = { ...video, analytics: [{ id: 11, name: "videoANPR", slot_id: 5 }] } as Video;

  it("filters the library by analytics", () => {
    const other = { ...video, id: 8, analytics: [] } as Video;
    expect(filterByAnalytics([linked, other], "")).toHaveLength(2);
    expect(filterByAnalytics([linked, other], "11").map((v) => v.id)).toEqual([7]);
    expect(filterByAnalytics([linked, other], "none").map((v) => v.id)).toEqual([8]);
  });

  it("adds a video to the chosen analytics; ones it is already in are fixed", async () => {
    mockFetch({ "/api/analytics/assign": { created: [{ slot_id: 30, analytics_id: 21, analytics_name: "PPE", video_id: 7 }], skipped: 0 } });
    const onDone = vi.fn();
    const u = userEvent.setup();
    wrap(<AssignDialog videos={[linked]} tree={tree} onClose={() => {}} onDone={onDone} />);
    const anpr = screen.getByRole("checkbox", { name: /videoANPR/ });
    expect(anpr).toBeChecked();
    expect(anpr).toBeDisabled();
    expect(screen.getByRole("button", { name: /Add to/ })).toBeDisabled();
    await u.click(screen.getByRole("checkbox", { name: /PPE/ }));
    await u.click(screen.getByRole("button", { name: /Add to 1 analytics/ }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(["PPE"], 1));
    expect(posts("/api/analytics/assign")).toEqual([{ video_ids: [7], analytics_ids: [21] }]);
  });

  it("links only the suggestions left ticked", async () => {
    mockFetch({ "/api/analytics/assign": { created: [{}], skipped: 0 } });
    const items = [
      { video_id: 1, rel_path: "Fire.mp4", analytics_id: 40, analytics_name: "FireSmoke", word: "fire" },
      { video_id: 2, rel_path: "smoke2.mp4", analytics_id: 40, analytics_name: "FireSmoke", word: "smoke" },
      { video_id: 3, rel_path: "PPE gate.mp4", analytics_id: 21, analytics_name: "PPE", word: "ppe" },
    ];
    const onDone = vi.fn();
    const u = userEvent.setup();
    wrap(<SuggestionsDialog items={items} onClose={() => {}} onDone={onDone} />);
    expect(within(screen.getByTestId("suggestions")).getAllByRole("checkbox")).toHaveLength(3);
    await u.click(screen.getByRole("checkbox", { name: /smoke2\.mp4/ }));
    await u.click(screen.getByRole("button", { name: "Add 2 selected" }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(2));
    expect(posts("/api/analytics/assign")).toEqual([
      { video_ids: [1], analytics_ids: [40] }, { video_ids: [3], analytics_ids: [21] },
    ]);
  });

  it("offers the 16:9 copy instead of Run for a portrait video", async () => {
    const portrait = { ...video, is_16_9: false, shape: "portrait", copy_16x9_id: 44 } as Video;
    const d = { ...detail, slots: [{ ...detail.slots[1], video: portrait, streams: [] }] };
    mockFetch({ "/api/analytics": tree, "/api/analytics/21": d, "/api/streams": [] });
    const cfg = { ...config, output: { width: 1280, height: 720, default: "standard", sizes: [], require_16_9: true } } as AppConfig;
    wrap(<Routes><Route path="/analytics/:id" element={<AnalyticsPage />} /></Routes>, "/analytics/21", cfg);
    const box = await screen.findByTestId("slot-not-16-9");
    expect(within(box).getByRole("button", { name: /Use the 16:9 copy/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^New stream$/ })).toBeNull();
    expect(screen.getByRole("link", { name: /In library/ })).toHaveAttribute("href", "/library?analytics=21");
  });
});

describe("TagButton", () => {
  it("is hidden outside the app data (lone dialogs, tests) instead of crashing", async () => {
    const { TagButton } = await import("../components/LibraryAnalytics");
    const { container } = render(<TagButton videoIds={[1]} />);
    expect(container.innerHTML).toBe("");
  });
});
