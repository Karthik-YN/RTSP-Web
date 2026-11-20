import { act, render, screen } from "@testing-library/react";
import { RigDataProvider, streamsShape, useLiveStream, useRig } from "./useRigData";
import type { AppConfig, Stream, User } from "../types/api";

const admin: User = { username: "anonymous", role: "admin", can_write: true, is_admin: true, auth_enabled: false };
const config = { version: "1", public_rtsp_host: "rig02", public_rtsp_port: 8554 } as unknown as AppConfig;
const mk = (frame: number, status = "RUNNING") => ({ id: 1, name: "cam", status,
  runtime: { viewer_count: 0, progress: { fps: 25, frame, updated_at: new Date().toISOString() }, ingest_bps: frame * 10 } }) as unknown as Stream;

let streams: Stream[] = [mk(1)];
let cpu = 10;
function mockFetch() {
  globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
    const path = String(url).split("?")[0];
    const body = path === "/api/streams" ? streams : path === "/api/system" ? { cpu_percent: cpu } : [];
    return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  }) as typeof fetch;
}

const renders = { user: 0, list: 0, chip: 0 };
function UserOnly() { renders.user++; const { user } = useRig(); return <span>{user.username}</span>; }
function List() { renders.list++; const { streams: s } = useRig(); return <span data-testid="list">{s?.map((x) => x.status).join(",")}</span>; }
function Chip() { renders.chip++; const s = useLiveStream(1); return <span data-testid="chip">{s?.runtime.progress?.frame}</span>; }

describe("shared rig state redraws only what changed", () => {
  it("leaves the list alone while only counters move, and user-only parts alone entirely", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    streams = [mk(1)]; cpu = 10; mockFetch();
    render(<RigDataProvider user={admin} config={config}><UserOnly /><List /><Chip /></RigDataProvider>);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("chip")).toHaveTextContent("1");
    const base = { ...renders };
    // counters move and CPU changes: the chip updates, the list and the user-only part do not redraw
    for (let i = 2; i <= 4; i++) {
      streams = [mk(i)]; cpu += 5;
      await act(async () => { await vi.advanceTimersByTimeAsync(3100); });
    }
    expect(screen.getByTestId("chip")).toHaveTextContent("4");
    expect(renders.list).toBe(base.list);
    expect(renders.user).toBe(base.user);
    // a real change (status) redraws the list
    streams = [mk(5, "STOPPED")];
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(screen.getByTestId("list")).toHaveTextContent("STOPPED");
    expect(renders.list).toBeGreaterThan(base.list);
    expect(renders.user).toBe(base.user);
    vi.useRealTimers();
  });

  it("ignores counters when comparing lists", () => {
    expect(streamsShape([mk(1)])).toBe(streamsShape([mk(99)]));
    expect(streamsShape([mk(1)])).not.toBe(streamsShape([mk(1, "STOPPED")]));
  });
});

describe("connection problems are never silent", () => {
  it("marks the state as stale while the server fails, and clears it when it answers again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let failing = false;
    globalThis.fetch = vi.fn(async (url: RequestInfo | URL) => {
      const path = String(url).split("?")[0];
      if (failing && path === "/api/streams") return new Response(JSON.stringify({ detail: "boom" }), { status: 503, headers: { "Content-Type": "application/json" } });
      return new Response(JSON.stringify(path === "/api/streams" ? [mk(1)] : []), { status: 200, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    function Err() { const { appError } = useRig(); return <span data-testid="err">{appError ? `down ${appError.status}` : "ok"}</span>; }
    render(<RigDataProvider user={admin} config={config}><Err /></RigDataProvider>);
    await act(async () => { await vi.advanceTimersByTimeAsync(100); });
    expect(screen.getByTestId("err")).toHaveTextContent("ok");
    failing = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(screen.getByTestId("err")).toHaveTextContent("down 503");
    failing = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(2100); });
    expect(screen.getByTestId("err")).toHaveTextContent("ok");
    vi.useRealTimers();
  });
});
