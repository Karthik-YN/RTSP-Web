import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AnalyticsNav } from "../components/AppShell";
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

describe("Analytics taskbar", () => {
  it("lists categories with live/total counts and filters by search", async () => {
    mockFetch({ "/api/analytics": tree, "/api/streams": [] });
    const u = userEvent.setup();
    wrap(<AnalyticsNav />);
    expect(await screen.findByText("videoANPR")).toBeInTheDocument();
    expect(screen.getByText("Traffic")).toBeInTheDocument();
    expect(screen.getByText("2/3")).toBeInTheDocument(); // live/total
    expect(screen.getByTitle("1 stream(s) need attention")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /PPE/ })).toHaveAttribute("href", "/analytics/21");

    await u.type(screen.getByLabelText("Find an analytics"), "dwell");
    expect(screen.getByText("VehicleDwell")).toBeInTheDocument();
    expect(screen.queryByText("videoANPR")).not.toBeInTheDocument();
    expect(screen.queryByText("People")).not.toBeInTheDocument();
  });

  it("folds a category", async () => {
    mockFetch({ "/api/analytics": tree, "/api/streams": [] });
    const u = userEvent.setup();
    wrap(<AnalyticsNav />);
    await u.click(await screen.findByRole("button", { name: /Traffic/ }));
    expect(screen.queryByText("videoANPR")).not.toBeInTheDocument();
    expect(screen.getByText("PPE")).toBeInTheDocument();
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
    // empty placeholder offers upload and library
    expect(screen.getByRole("button", { name: /Upload from this device/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Choose from library/ })).toBeInTheDocument();
    // stream row: name, viewers (peak in the title), preview watchers separately
    const row = screen.getByRole("link", { name: "ppe-1" }).closest("li")!;
    expect(within(row).getByTitle(/2 edge\/RTSP viewer\(s\) now · peak today 3/)).toBeInTheDocument();
    expect(within(row).getByTitle(/Watching the preview/)).toHaveTextContent("1");

    await u.click(screen.getByRole("button", { name: /Run RTSP/ }));
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
    wrap(<AssignDialog v={linked} tree={tree} onClose={() => {}} onDone={onDone} />);
    const anpr = screen.getByRole("checkbox", { name: /videoANPR/ });
    expect(anpr).toBeChecked();
    expect(anpr).toBeDisabled();
    expect(screen.getByRole("button", { name: /Add to/ })).toBeDisabled();
    await u.click(screen.getByRole("checkbox", { name: /PPE/ }));
    await u.click(screen.getByRole("button", { name: /Add to 1 analytics/ }));
    await waitFor(() => expect(onDone).toHaveBeenCalledWith(["PPE"]));
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
    expect(screen.queryByRole("button", { name: /Run RTSP/ })).toBeNull();
    expect(screen.getByRole("link", { name: /In library/ })).toHaveAttribute("href", "/library?analytics=21");
  });

  it("jumps to the analytics search with /", async () => {
    localStorage.clear(); // an earlier test folds a category
    mockFetch({ "/api/analytics": tree, "/api/streams": [] });
    const u = userEvent.setup();
    wrap(<AnalyticsNav />);
    await screen.findByText("videoANPR");
    await u.keyboard("/");
    expect(screen.getByLabelText("Find an analytics")).toHaveFocus();
    expect(screen.getByLabelText("Find an analytics")).toHaveValue("");
  });
});
