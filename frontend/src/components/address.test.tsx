import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { RtspAddressProvider, useRtspAddress, withHost } from "../hooks/useRtspAddress";
import { RtspAddressPicker } from "./AppShell";
import type { AppConfig } from "../types/api";

const config = {
  version: "1.0.0", public_rtsp_host: "rig02.lan", public_rtsp_port: 8554,
  rtsp_url_template: "rtsp://rig02.lan:8554/{name}",
  rtsp_hosts: [
    { host: "rig02.lan", kind: "canonical" },
    { host: "10.8.0.5", kind: "alternate" },
    { host: "192.168.29.105", kind: "browser" },
  ],
} as AppConfig;

function Url() {
  const addr = useRtspAddress();
  return <span data-testid="url">{addr.url("rtsp://rig02.lan:8554/cam-1")}|{addr.param ?? "-"}</span>;
}

describe("RTSP address picker", () => {
  beforeEach(() => localStorage.clear());

  it("rewrites shown/copied URLs to the chosen address and remembers it", async () => {
    const user = userEvent.setup();
    const { unmount } = render(<RtspAddressProvider config={config}><RtspAddressPicker port={8554} /><Url /></RtspAddressProvider>);
    expect(screen.getByTestId("url")).toHaveTextContent("rtsp://rig02.lan:8554/cam-1|-");

    // the hint offers the address this dashboard was opened with
    await user.click(screen.getByText(/You opened this page via 192.168.29.105/));
    expect(screen.getByTestId("url")).toHaveTextContent("rtsp://192.168.29.105:8554/cam-1|192.168.29.105");
    expect(screen.getByText(/Not the main address/)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("RTSP address"), "10.8.0.5");
    expect(screen.getByTestId("url")).toHaveTextContent("rtsp://10.8.0.5:8554/cam-1|10.8.0.5");
    unmount();

    // remembered per browser
    render(<RtspAddressProvider config={config}><Url /></RtspAddressProvider>);
    expect(screen.getByTestId("url")).toHaveTextContent("rtsp://10.8.0.5:8554/cam-1");
  });

  it("falls back to the main address when the remembered one is no longer offered", () => {
    localStorage.setItem("rig02.rtsp.host", "10.9.9.9");
    render(<RtspAddressProvider config={config}><Url /></RtspAddressProvider>);
    expect(screen.getByTestId("url")).toHaveTextContent("rtsp://rig02.lan:8554/cam-1|-");
  });

  it("shows a plain line when there is only one address", () => {
    const one = { ...config, rtsp_hosts: [{ host: "rig02.lan", kind: "canonical" }] } as AppConfig;
    render(<RtspAddressProvider config={one}><RtspAddressPicker port={8554} /></RtspAddressProvider>);
    expect(screen.getByText("rtsp://rig02.lan:8554")).toBeInTheDocument();
    expect(screen.queryByLabelText("RTSP address")).toBeNull();
  });

  it("swaps hosts including IPv6 literals", () => {
    expect(withHost("rtsp://rig02.lan:8554/a", "fd00::5")).toBe("rtsp://[fd00::5]:8554/a");
    expect(withHost("rtsp://[fd00::5]:8554/a", "rig02.lan")).toBe("rtsp://rig02.lan:8554/a");
  });
});

describe("Cloudflare Access session expiry", () => {
  it("is recognised when the health probe is answered with a redirect", async () => {
    const { accessSessionExpired } = await import("../services/api");
    globalThis.fetch = vi.fn(async () => ({ type: "opaqueredirect", status: 0 }) as Response) as typeof fetch;
    expect(await accessSessionExpired()).toBe(true);
    globalThis.fetch = vi.fn(async () => new Response("{}", { status: 200 })) as typeof fetch;
    expect(await accessSessionExpired()).toBe(false);
    globalThis.fetch = vi.fn(async () => { throw new TypeError("offline"); }) as typeof fetch;
    expect(await accessSessionExpired()).toBe(false);
  });
});
