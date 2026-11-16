import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "./common";
import { UploadQueueProvider } from "./UploadQueue";
import { RigDataProvider } from "../hooks/useRigData";
import { PAGE, VideoLibrary } from "./VideoLibrary";
import { applyLibraryFilters, NO_FILTERS, uploaders, videoNotes } from "../lib/libraryFilters";
import type { AppConfig, User, Video } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = {
  version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554,
  output: { width: 1280, height: 720, default: "standard", sizes: [], require_16_9: true },
} as unknown as AppConfig;

function vid(id: number, name: string, extra: Partial<Video> = {}): Video {
  return {
    id, rel_path: name, filename: name, size_bytes: id * 1e6, status: "READY", error: null, container: "mp4",
    duration_s: id * 10, width: 1920, height: 1080, display_width: 1920, display_height: 1080, shape: "16:9", is_16_9: true,
    fps: 25, fps_text: "25", is_vfr: false, video_codec: "h264", video_profile: "High", pix_fmt: "yuv420p", bit_depth: 8,
    audio_codec: null, bitrate_bps: 4e6, keyframe_interval_s: 2, compat_mode: "copy", compat_reasons: [], compat_warnings: [],
    thumbnail_url: null, streams_using: 0, updated_at: "2026-11-01T00:00:00Z", last_seen_at: "", analytics: [],
    uploaded_by: null, uploaded_at: null, ...extra,
  } as Video;
}

const fire = vid(1, "Fire.mp4", { analytics: [{ id: 16, name: "FireSmoke", slot_id: 1 }] });
const gate = vid(2, "gate.mp4", { uploaded_by: "alice", uploaded_at: "2026-11-10T00:00:00Z", keyframe_interval_s: 10 });
const phone = vid(3, "phone clip.mp4", { is_16_9: false, shape: "portrait", display_width: 1080, display_height: 1920,
  uploaded_by: "bob", uploaded_at: "2026-11-12T00:00:00Z" });

describe("library filters and sort", () => {
  const all = [fire, gate, phone];
  const names = (f = {}) => applyLibraryFilters(all, { ...NO_FILTERS, ...f }).map((v) => v.filename);

  it("sorts by name (natural), newest, size and length", () => {
    expect(applyLibraryFilters([vid(1, "cam10.mp4"), vid(2, "cam2.mp4")], NO_FILTERS).map((v) => v.filename))
      .toEqual(["cam2.mp4", "cam10.mp4"]);
    expect(names({ sort: "newest" })).toEqual(["phone clip.mp4", "gate.mp4", "Fire.mp4"]);
    expect(names({ sort: "size" })).toEqual(["phone clip.mp4", "gate.mp4", "Fire.mp4"]);
    expect(names({ sort: "duration" })[0]).toBe("phone clip.mp4");
  });

  it("filters by text (name or uploader), shape, uploader, analytics and notes", () => {
    expect(names({ q: "ALICE" })).toEqual(["gate.mp4"]);
    expect(names({ shape: "other" })).toEqual(["phone clip.mp4"]);
    expect(names({ shape: "16:9" })).toEqual(["Fire.mp4", "gate.mp4"]);
    expect(names({ uploader: "admin" })).toEqual(["Fire.mp4"]); // copied onto the server: shown as admin
    expect(names({ q: "smoke" })).toEqual(["Fire.mp4"]); // search also matches analytics tags
    expect(names({ uploader: "bob" })).toEqual(["phone clip.mp4"]);
    expect(names({ analytics: "16" })).toEqual(["Fire.mp4"]);
    expect(names({ analytics: "none" })).toEqual(["gate.mp4", "phone clip.mp4"]);
    expect(names({ attention: true })).toEqual(["gate.mp4"]);
    expect(videoNotes(gate)[0]).toMatch(/Keyframes only every 10.0 s/);
    expect(uploaders(all)).toEqual(["admin", "alice", "bob"]);
  });
});

const tree = [{ id: 2, name: "People", sort: 0, analytics: [
  { id: 21, name: "PPE", description: null, sort: 0, slots: 0, slot_ids: [], streams: 0, running: 0, problems: 0, viewers: 0 },
] }];

function mockFetch() {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url).split("?")[0];
    const body = path === "/api/analytics" ? tree : path === "/api/analytics/assign" ? { created: [{}, {}, {}], skipped: 0 } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

function show(videos: Video[]) {
  mockFetch();
  return render(
    <ToastProvider>
      <MemoryRouter>
        <RigDataProvider user={admin} config={config}>
          <UploadQueueProvider onUploaded={() => {}}>
            <VideoLibrary videos={videos} loading={false} user={admin} onChanged={() => {}} onCreateStream={() => {}} />
          </UploadQueueProvider>
        </RigDataProvider>
      </MemoryRouter>
    </ToastProvider>,
  );
}

describe("VideoLibrary", () => {
  beforeEach(() => localStorage.clear());

  it("filters from the toolbar, counts, and clears", async () => {
    const u = userEvent.setup();
    show([fire, gate, phone]);
    expect(screen.getByTestId("library-count")).toHaveTextContent("3 videos");
    await u.selectOptions(screen.getByLabelText("Filter by shape"), "other");
    expect(screen.getByTestId("library-count")).toHaveTextContent("1 of 3 videos");
    expect(screen.getAllByTestId("thumb-box")).toHaveLength(1);
    await u.type(screen.getByLabelText("Search videos"), "zzz");
    expect(screen.getByTestId("no-match")).toBeInTheDocument();
    await u.click(screen.getAllByRole("button", { name: "Clear filters" })[0]);
    expect(screen.getAllByTestId("thumb-box")).toHaveLength(3);
    // one "notes" chip instead of a row of warning pills
    expect(screen.getByRole("button", { name: "1 note about gate.mp4" })).toHaveTextContent("1 note");
    await u.click(screen.getByRole("button", { name: /Needs attention \(1\)/ }));
    expect(screen.getAllByTestId("thumb-box")).toHaveLength(1);
  });

  it("switches to a compact list and remembers it", async () => {
    const u = userEvent.setup();
    const { unmount } = show([fire, gate]);
    await u.click(screen.getByRole("button", { name: "List view" }));
    expect(screen.getAllByTestId("video-row")).toHaveLength(2);
    expect(screen.queryByTestId("thumb-box")).toBeNull();
    expect(within(screen.getAllByTestId("video-row")[1]).getByText("by alice")).toBeInTheDocument();
    unmount();
    show([fire]);
    expect(screen.getAllByTestId("video-row")).toHaveLength(1);
  });

  it(`renders ${PAGE} at a time`, async () => {
    const u = userEvent.setup();
    show(Array.from({ length: PAGE + 5 }, (_, i) => vid(i + 1, `clip${i + 1}.mp4`)));
    expect(screen.getAllByTestId("thumb-box")).toHaveLength(PAGE);
    await u.click(screen.getByRole("button", { name: "Show more (5 left)" }));
    expect(screen.getAllByTestId("thumb-box")).toHaveLength(PAGE + 5);
  });

  it("selects several videos and adds them to analytics together", async () => {
    const u = userEvent.setup();
    show([fire, gate, phone]);
    await u.click(screen.getByRole("button", { name: "Select" }));
    await u.click(screen.getByRole("checkbox", { name: "Select gate.mp4" }));
    await u.click(screen.getByRole("checkbox", { name: "Select phone clip.mp4" }));
    const bar = screen.getByTestId("selection-bar");
    expect(bar).toHaveTextContent("2 selected");
    await u.click(within(bar).getByRole("button", { name: /Select all 3/ }));
    expect(bar).toHaveTextContent("3 selected");
    const add = within(bar).getByRole("button", { name: /Add to analytics/ });
    await waitFor(() => expect(add).toBeEnabled()); // once the analytics tree has loaded
    await u.click(add);
    expect(screen.getByRole("dialog", { name: "Add 3 videos to analytics" })).toBeInTheDocument();
    await u.click(screen.getByRole("checkbox", { name: /PPE/ }));
    await u.click(screen.getByRole("button", { name: /Add to 1 analytics/ }));
    await waitFor(() => expect(screen.queryByTestId("selection-bar")).toBeNull()); // done: selection ends
    const calls = (globalThis.fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls
      .filter(([u2, i]) => String(u2) === "/api/analytics/assign" && i?.method === "POST");
    expect(calls.map(([, i]) => JSON.parse(String(i!.body)))).toEqual([{ video_ids: [1, 2, 3], analytics_ids: [21] }]);
  });
});
