import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { ToastProvider } from "./common";
import { RigDataProvider } from "../hooks/useRigData";
import { UploadQueueProvider, useUploads } from "./UploadQueue";
import { NameChips, NamingGuideButton, RenameDialog } from "./Naming";
import { buildName, guessParts, nameErrors, nextNumber, parseName, slug } from "../lib/naming";
import type { AppConfig, NamingConvention, User, Video } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = { version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554 } as unknown as AppConfig;
const convention: NamingConvention = {
  format: "WHAT_WHERE_NN[_tag].ext", pattern: "", where_max: 14, tags: ["720p", "web"], mode: "enforce",
  codes: [
    { code: "ALPR", analytics_id: 4, analytics: "VehicleALPR", category: "Traffic" },
    { code: "FALL", analytics_id: 7, analytics: "SlipAndFall", category: "People" },
    { code: "MISC", analytics_id: null, analytics: "Not decided yet", category: null },
  ],
  places: [{ where: "toll-s1", videos: 1, codes: ["ALPR"] }],
  examples: [{ name: "ALPR_toll-s1_01.mp4", reads: "ALPR · toll plaza camera s1 · clip 1" }],
};
const lib = [{ id: 1, filename: "ALPR_toll-s1_01.mp4", rel_path: "ALPR_toll-s1_01.mp4", size_bytes: 1,
  name_parts: { what: "ALPR", where: "toll-s1", nn: 1, tags: [] } }] as unknown as Video[];
let calls: { method: string; path: string; body: unknown }[] = [];

function mockFetch() {
  calls = [];
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    const path = String(url).split("?")[0];
    const method = init?.method ?? "GET";
    const body = init?.body && typeof init.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path, body });
    const out = path === "/api/naming" ? convention : path === "/api/videos" ? lib
      : path === "/api/videos/upload/check" ? { filename: body.filename, exists: false, ok: true }
        : path.endsWith("/rename") ? { video_id: 2, old: "a", new: body.name, streams: [], changed: true }
          : path === "/api/videos/uploads" ? { id: "s1", offset: 0 } : [];
    // the upload session is refused, so the queue stops before sending bytes (XHR is not mocked)
    const status = path === "/api/videos/uploads" ? 507 : 200;
    return new Response(JSON.stringify(status === 507 ? { detail: "test: no bytes" } : out),
      { status, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

function wrap(children: React.ReactNode) {
  return (
    <ToastProvider><MemoryRouter><RigDataProvider user={admin} config={config}>
      <UploadQueueProvider onUploaded={() => {}}>{children}</UploadQueueProvider>
    </RigDataProvider></MemoryRouter></ToastProvider>
  );
}

describe("naming rules (same as the server)", () => {
  it("accepts and reads convention names", () => {
    expect(nameErrors("ALPR_toll-s1_01.mp4")).toEqual([]);
    expect(parseName("JAM_dhaka_01_web.mp4")).toEqual({ what: "JAM", where: "dhaka", nn: 1, tags: ["web"] });
    expect(nameErrors("Copy of tirupathi test bench.mp4")[0]).toMatch(/no spaces/);
    expect(nameErrors("alpr_toll_01.mp4")[0]).toMatch(/UPPERCASE/);
    expect(nameErrors("ALPR_toll_01_final.mp4")[0]).toMatch(/Unknown tag/);
    expect(nameErrors("PPE_x_01.mp4", new Set(["ALPR"]))[0]).toMatch(/not an analytics code/);
  });

  it("numbers, builds and guesses", () => {
    expect(nextNumber("ALPR", "toll-s1", ["ALPR_toll-s1_01.mp4", "ALPR_toll-s1_02_720p.mp4", "x.mp4"])).toBe(3);
    expect(buildName("FALL", "office", 2, ["h264"], ".mp4")).toBe("FALL_office_02_h264.mp4");
    expect(slug("Toll Plaza S1 (east)")).toBe("toll-plaza-s1");
    expect(guessParts("fall office room h264.mp4", ["FALL", "ALPR"])).toEqual({ what: "FALL", where: "office-room", tags: ["h264"] });
    expect(guessParts("Copy of tirupathi test bench.mp4", ["ALPR"], "ALPR").where).toBe("tirupathi"); // word boundary
    expect(slug("cybertronics utopia")).toBe("cybertronics");
  });
});

describe("naming in the dashboard", () => {
  beforeEach(mockFetch);

  it("asks for WHAT and WHERE before uploading and sends the convention name", async () => {
    const u = userEvent.setup();
    function Drop() {
      const { enqueue } = useUploads();
      return <button onClick={() => enqueue([new File(["x"], "my clip.mp4", { type: "video/mp4" })])}>drop</button>;
    }
    render(wrap(<Drop />));
    await waitFor(() => expect(calls.some((c) => c.path === "/api/videos")).toBe(true));
    await u.click(screen.getByText("drop"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: /Upload/ })).toBeDisabled();
    await waitFor(() => expect(within(dialog).getAllByRole("option", { name: /ALPR — VehicleALPR/ }).length).toBeGreaterThan(0));
    await u.selectOptions(within(dialog).getByLabelText("WHAT (analytics)"), "ALPR");
    await u.clear(within(dialog).getByLabelText("WHERE (site or camera)"));
    await u.type(within(dialog).getByLabelText("WHERE (site or camera)"), "Toll S1");
    // the library already has ALPR_toll-s1_01: this one is 02
    await waitFor(() => expect(within(dialog).getByTestId("final-name")).toHaveTextContent("ALPR_toll-s1_02.mp4"));
    expect(within(dialog).queryByRole("button", { name: "Keep original names" })).toBeNull(); // enforce mode
    await u.click(within(dialog).getByRole("button", { name: /Upload/ }));
    await waitFor(() => expect(calls).toContainEqual(expect.objectContaining({
      path: "/api/videos/uploads", body: { filename: "ALPR_toll-s1_02.mp4", size: 1, original_name: "my clip.mp4" } })));
  });

  it("shows a name as chips, flags other names, and renames", async () => {
    const u = userEvent.setup();
    const odd = { id: 2, filename: "gate.mp4", rel_path: "gate.mp4", name_parts: null, analytics: [] } as unknown as Video;
    const onRename = vi.fn();
    const { unmount } = render(wrap(<><NameChips v={lib[0]} /><NameChips v={odd} onRename={onRename} /></>));
    expect(screen.getByTestId("name-chips")).toHaveTextContent("ALPRtoll-s1#01");
    await u.click(screen.getByRole("button", { name: "Rename" }));
    expect(onRename).toHaveBeenCalled();
    unmount();

    render(wrap(<RenameDialog v={odd} onClose={() => {}} />));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getAllByRole("option", { name: /FALL/ }).length).toBeGreaterThan(0));
    await u.selectOptions(within(dialog).getByLabelText("WHAT (analytics)"), "ALPR");
    await u.clear(within(dialog).getByLabelText("WHERE (site or camera)"));
    await u.type(within(dialog).getByLabelText("WHERE (site or camera)"), "gate5");
    expect(within(dialog).getByTestId("final-name")).toHaveTextContent("ALPR_gate5_01.mp4");
    await u.click(within(dialog).getByRole("button", { name: "Rename" }));
    await waitFor(() => expect(calls).toContainEqual(expect.objectContaining({
      path: "/api/videos/2/rename", body: { name: "ALPR_gate5_01.mp4", dry_run: false } })));
  });

  it("opens the Naming guide with the codes and checks a name as you type", async () => {
    const u = userEvent.setup();
    render(wrap(<NamingGuideButton />));
    await u.click(screen.getByRole("button", { name: /Naming guide/ }));
    const dialog = await screen.findByRole("dialog");
    await waitFor(() => expect(within(dialog).getByText("VehicleALPR")).toBeInTheDocument());
    await u.type(within(dialog).getByLabelText("Name to check"), "gate.mp4");
    expect(within(dialog).getByText(/three parts/)).toBeInTheDocument();
    await u.clear(within(dialog).getByLabelText("Name to check"));
    await u.type(within(dialog).getByLabelText("Name to check"), "FALL_office_01.mp4");
    expect(within(dialog).getByText(/Follows the convention/)).toBeInTheDocument();
  });

  it("uploads as before when the server has no naming API yet, but still shows the guide (built in)", async () => {
    vi.resetModules();
    const fresh = await import("./Naming");
    const queue = await import("./UploadQueue");
    const rig = await import("../hooks/useRigData");
    const common = await import("./common");
    calls = [];
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
      const path = String(url).split("?")[0];
      calls.push({ method: init?.method ?? "GET", path, body: init?.body ? JSON.parse(String(init.body)) : null });
      if (path === "/api/naming") return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404, headers: { "Content-Type": "application/json" } });
      if (path === "/api/videos/upload/check") return new Response(JSON.stringify({ filename: "my clip.mp4", exists: false, ok: true }), { status: 200, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify(path === "/api/videos/uploads" ? { detail: "stop" } : []),
        { status: path === "/api/videos/uploads" ? 507 : 200, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    const u = userEvent.setup();
    function Drop() {
      const { enqueue } = queue.useUploads();
      return <button onClick={() => enqueue([new File(["x"], "my clip.mp4", { type: "video/mp4" })])}>drop</button>;
    }
    render(
      <common.ToastProvider><MemoryRouter><rig.RigDataProvider user={admin} config={config}>
        <queue.UploadQueueProvider onUploaded={() => {}}><fresh.NamingGuideButton /><Drop /></queue.UploadQueueProvider>
      </rig.RigDataProvider></MemoryRouter></common.ToastProvider>,
    );
    await u.click(screen.getByText("drop"));
    await waitFor(() => expect(calls).toContainEqual(expect.objectContaining({
      path: "/api/videos/uploads", body: { filename: "my clip.mp4", size: 1 } })));
    expect(screen.queryByRole("dialog")).toBeNull();
    // the guide works from the built-in convention
    await u.click(screen.getByRole("button", { name: /Naming guide/ }));
    const guide = await screen.findByRole("dialog");
    await waitFor(() => expect(within(guide).getByText("VehicleALPR")).toBeInTheDocument());
    expect(within(guide).getAllByText("RLVD").length).toBeGreaterThan(0);
    expect(guide).toHaveTextContent("Uploads are not checked yet");
    expect(within(guide).queryByLabelText("Upload naming check")).toBeNull();
    await u.type(within(guide).getByLabelText("Name to check"), "ALPR_toll-s1_01.mp4");
    expect(within(guide).getByText(/Follows the convention/)).toBeInTheDocument();
    // and library cards do not call every video "not standard"
    const oldVideo = { id: 9, filename: "gate.mp4", rel_path: "gate.mp4" } as unknown as Video;
    const { container } = render(<fresh.NameChips v={oldVideo} onRename={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
