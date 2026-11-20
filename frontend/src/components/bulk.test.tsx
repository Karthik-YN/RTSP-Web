import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "./common";
import { RigDataProvider } from "../hooks/useRigData";
import { StreamsTable } from "./StreamsTable";
import type { AppConfig, Stream, User } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const viewer: User = { username: "v", role: "viewer", can_write: false, is_admin: false, auth_enabled: true };
const config = { version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554 } as unknown as AppConfig;

const mk = (id: number, name: string, status: string, viewers = 0) => ({
  id, name, rtsp_url: `rtsp://rig02:8554/${name}`, status, desired_state: status === "RUNNING" ? "RUNNING" : "STOPPED",
  slot_id: null, video_id: 1, source_rel_path: "gate.mp4", note: null, owner: null, protected: false, playback_mode: "loop",
  effective_start_s: 0, end_offset_s: null, output_mode: "copy", version: 1, last_stop_at: null, last_start_at: null,
  runtime: { viewer_count: viewers, viewer_peak_today: viewers, preview_watchers: 0, loop_count: 0 },
}) as unknown as Stream;

const streams = [mk(1, "cam-a", "RUNNING", 2), mk(2, "cam-b", "RUNNING"), mk(3, "cam-c", "STOPPED")];
let calls: { path: string; body: unknown }[] = [];

function show(user = admin) {
  calls = [];
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url).split("?")[0];
    if (init?.method === "POST") calls.push({ path, body: JSON.parse(String(init.body)) });
    const body = path === "/api/streams/bulk" ? { ok: 1, failed: 0, results: [{ id: 1, ok: true }] } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  return render(
    <ToastProvider><MemoryRouter><RigDataProvider user={user} config={config}>
      <StreamsTable streams={streams} user={user} onSelect={() => {}} onChanged={() => {}} mediamtxDown={false} />
    </RigDataProvider></MemoryRouter></ToastProvider>,
  );
}

describe("Streams bulk controls", () => {
  it("selects and deselects every shown stream from one checkbox", async () => {
    const u = userEvent.setup();
    show();
    const all = screen.getByRole("checkbox", { name: "Select all 3 shown" });
    await u.click(all);
    expect(screen.getAllByText("3 selected").length).toBeGreaterThan(0);
    expect(screen.getByRole("checkbox", { name: "Select cam-b" })).toBeChecked();
    await u.click(screen.getAllByRole("button", { name: /Deselect all/ })[0]);
    expect(screen.getByRole("checkbox", { name: "Select cam-b" })).not.toBeChecked();
  });

  it("stops every live stream shown after a confirm that warns about readers", async () => {
    const u = userEvent.setup();
    show();
    const bar = screen.getByLabelText("Bulk actions");
    await u.click(within(bar).getByRole("button", { name: /Stop 2/ }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("2 readers are watching right now");
    expect(calls.filter((c) => c.path === "/api/streams/bulk")).toHaveLength(0);
    await u.click(within(dialog).getByRole("button", { name: "Stop 2" }));
    await waitFor(() => expect(calls).toContainEqual({ path: "/api/streams/bulk", body: { stream_ids: [1, 2], action: "stop" } }));
  });

  it("starts the stopped ones at once, without asking", async () => {
    const u = userEvent.setup();
    show();
    await u.click(within(screen.getByLabelText("Bulk actions")).getByRole("button", { name: /Start 1/ }));
    await waitFor(() => expect(calls).toContainEqual({ path: "/api/streams/bulk", body: { stream_ids: [3], action: "start" } }));
  });

  it("shows no bulk controls or checkboxes to viewers", () => {
    show(viewer);
    expect(screen.queryByLabelText("Bulk actions")).toBeNull();
    expect(screen.queryAllByRole("checkbox", { name: /^Select/ })).toHaveLength(0); // "Group by analytics" is a view setting
  });
});

const tree = [{ id: 1, name: "Traffic", sort: 0, analytics: [
  { id: 4, name: "VehicleALPR", code: "ALPR", description: null, sort: 0, slots: 1, slot_ids: [9], streams: 2, running: 1, problems: 0, viewers: 2 },
] }];
const inAlpr = [{ ...mk(11, "alpr-1", "RUNNING", 2), slot_id: 9 }, { ...mk(12, "alpr-2", "STOPPED"), slot_id: 9 }] as Stream[];
const looseOne = mk(13, "toll_s1", "RUNNING");

function showPage(list: Stream[]) {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url).split("?")[0];
    if (init?.method === "POST") calls.push({ path, body: JSON.parse(String(init.body)) });
    const body = path === "/api/analytics" ? tree : path === "/api/streams/bulk" ? { ok: 1, failed: 0, results: [] } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
  calls = [];
  return render(
    <ToastProvider><MemoryRouter><RigDataProvider user={admin} config={config}>
      <StreamsTable streams={list} user={admin} onSelect={() => {}} onChanged={() => {}} mediamtxDown={false} onClone={() => {}} />
    </RigDataProvider></MemoryRouter></ToastProvider>,
  );
}

describe("Streams page layout", () => {
  beforeEach(() => localStorage.clear());

  it("groups streams by analytics, with streams in no analytics last, and folds a section", async () => {
    const u = userEvent.setup();
    showPage([looseOne, ...inAlpr]);
    const alpr = await screen.findByRole("region", { name: "Streams of VehicleALPR" });
    const none = screen.getByRole("region", { name: "Streams of Not in any analytics" });
    expect(alpr.compareDocumentPosition(none) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(alpr).getByText("ALPR")).toBeInTheDocument();
    expect(within(alpr).getByText("live of 2", { exact: false })).toBeInTheDocument();
    expect(within(alpr).getAllByRole("article")).toHaveLength(2);
    // section start/stop acts on that section only
    await u.click(within(alpr).getByRole("button", { name: /Start 1/ }));
    await waitFor(() => expect(calls).toContainEqual({ path: "/api/streams/bulk", body: { stream_ids: [12], action: "start" } }));
    await u.click(within(alpr).getByRole("button", { name: "VehicleALPR" }));
    expect(within(alpr).queryAllByRole("article")).toHaveLength(0);
    expect(within(none).getAllByRole("article")).toHaveLength(1);
  });

  it("switches to a table and remembers it; rare actions live in the ⋯ menu", async () => {
    const u = userEvent.setup();
    const { unmount } = showPage([looseOne, ...inAlpr]);
    await screen.findByRole("region", { name: "Streams of VehicleALPR" });
    await u.click(screen.getByRole("button", { name: "Table view" }));
    expect(screen.getAllByRole("table")).toHaveLength(2);
    expect(screen.getByRole("row", { name: /Stream alpr-1, RUNNING/ })).toBeInTheDocument();
    unmount();
    showPage([looseOne, ...inAlpr]);
    await waitFor(() => expect(screen.getAllByRole("table")).toHaveLength(2)); // remembered
    // Stop is one click; Restart / Clone / Delete are in the menu
    const row = screen.getByRole("row", { name: /Stream alpr-1, RUNNING/ });
    expect(within(row).getByRole("button", { name: "Stop" })).toBeInTheDocument();
    expect(within(row).queryByRole("button", { name: "Delete" })).toBeNull();
    await u.click(within(row).getByRole("button", { name: "More actions for alpr-1" }));
    expect(screen.getByRole("menuitem", { name: /Restart/ })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /Delete/ })).toBeInTheDocument();
    await u.click(screen.getByRole("menuitem", { name: /Delete/ }));
    expect(await screen.findByRole("dialog")).toHaveTextContent("Delete alpr-1?");
  });

  it("sorts names naturally (cam-2 before cam-10)", async () => {
    showPage([mk(21, "cam-10", "RUNNING"), mk(22, "cam-2", "RUNNING"), mk(23, "Cam-1", "RUNNING")]);
    await waitFor(() => expect(screen.getAllByRole("article").map((a) => a.getAttribute("aria-label")?.split(",")[0]))
      .toEqual(["Stream Cam-1", "Stream cam-2", "Stream cam-10"]));
  });

  it("can show one flat list instead of sections", async () => {
    const u = userEvent.setup();
    showPage([looseOne, ...inAlpr]);
    await screen.findByRole("region", { name: "Streams of VehicleALPR" });
    await u.click(screen.getByRole("checkbox", { name: "Group by analytics" }));
    expect(screen.queryByRole("region", { name: /Streams of/ })).toBeNull();
    expect(screen.getAllByRole("article")).toHaveLength(3);
    // flat list: each card shows its analytics instead
    expect(screen.getAllByRole("link", { name: "VehicleALPR" })).toHaveLength(2);
  });
});

describe("links into the Streams page", () => {
  it("opens on the tab named in ?filter= (from the Overview's numbers)", () => {
    calls = [];
    globalThis.fetch = vi.fn(async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })) as typeof fetch;
    render(
      <ToastProvider><MemoryRouter initialEntries={["/streams?filter=stopped"]}><RigDataProvider user={admin} config={config}>
        <StreamsTable streams={streams} user={admin} onSelect={() => {}} onChanged={() => {}} mediamtxDown={false} />
      </RigDataProvider></MemoryRouter></ToastProvider>,
    );
    expect(screen.getAllByRole("article").map((a) => a.getAttribute("aria-label")?.split(",")[0])).toEqual(["Stream cam-c"]);
  });
});
