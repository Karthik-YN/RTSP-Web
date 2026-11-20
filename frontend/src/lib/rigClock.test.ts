import { formatClock, formatDate, measureRigClock } from "./rigClock";

describe("rig clock", () => {
  it("formats IST (UTC+5:30) and UTC with milliseconds", () => {
    const t = Date.UTC(2026, 10, 20, 13, 24, 0, 288); // 13:24:00.288 UTC
    expect(formatClock(t, "UTC")).toBe("13:24:00.288");
    expect(formatClock(t, "IST")).toBe("18:54:00.288");
    expect(formatClock(t, "IST", false)).toBe("18:54:00");
    expect(formatDate(Date.UTC(2026, 10, 20, 20, 0), "IST")).toBe("2026-11-21"); // past midnight in India
  });

  it("measures the server's clock like NTP (half the round trip), and falls back to the Date header", async () => {
    const realNow = Date.now;
    let fake = 1_000_000;
    Date.now = () => fake;
    globalThis.fetch = vi.fn(async () => {
      fake += 40; // 40 ms to reach the server
      const body = JSON.stringify({ epoch_ms: fake + 5000 }); // the server is 5 s ahead
      fake += 40; // 40 ms back
      return new Response(body, { status: 200, headers: { "Content-Type": "application/json" } });
    }) as typeof fetch;
    const c = await measureRigClock(true);
    expect(c!.source).toBe("server");
    expect(Math.round(c!.offsetMs)).toBe(5000);
    expect(c!.errorMs).toBe(40);
    // older server: no /api/system/time -> HTTP Date header, whole seconds
    globalThis.fetch = vi.fn(async () => new Response("", { status: 404, headers: { date: new Date(fake + 5000).toUTCString() } })) as typeof fetch;
    const d = await measureRigClock(true);
    expect(d!.source).toBe("date-header");
    expect(Math.abs(d!.offsetMs - 5000)).toBeLessThan(1500);
    Date.now = realNow;
  });
});
