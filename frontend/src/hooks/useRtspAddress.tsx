import { createContext, useContext, useMemo, useState, type ReactNode } from "react";
import type { AppConfig, RtspHost } from "../types/api";

/**
 * Which server address RTSP URLs are shown and copied with.
 *
 * People on another subnet or the VPN may reach the server under a different
 * name/IP than the canonical one. The server offers a short, validated list
 * (`config.rtsp_hosts`); the choice is remembered per browser.
 */
export interface RtspAddress {
  host: string | null;
  hosts: RtspHost[];
  /** True when the chosen host is not the canonical one. */
  custom: boolean;
  setHost: (host: string) => void;
  /** Rewrites an rtsp://host:port/name URL to the chosen host. */
  url: (u: string) => string;
  /** `host` query value for URL exports (undefined for the canonical host). */
  param: string | undefined;
}

const KEY = "rig02.rtsp.host";
const DEFAULT: RtspAddress = { host: null, hosts: [], custom: false, setHost: () => {}, url: (u) => u, param: undefined };
const Ctx = createContext<RtspAddress>(DEFAULT);

export function urlHost(h: string): string {
  return h.includes(":") && !h.startsWith("[") ? `[${h}]` : h;
}

export function withHost(u: string, host: string): string {
  return u.replace(/^rtsp:\/\/(\[[^\]]*\]|[^/:]+)/i, `rtsp://${urlHost(host)}`);
}

function readStored(): string | null {
  try { return localStorage.getItem(KEY); } catch { return null; }
}

export function RtspAddressProvider({ config, children }: { config: AppConfig; children: ReactNode }) {
  const hosts = useMemo<RtspHost[]>(
    () => config.rtsp_hosts?.length ? config.rtsp_hosts : [{ host: config.public_rtsp_host, kind: "canonical" }],
    [config],
  );
  const [stored, setStored] = useState<string | null>(readStored);
  const canonical = hosts[0].host;
  const host = hosts.find((h) => h.host === stored)?.host ?? canonical;
  const value = useMemo<RtspAddress>(() => ({
    host, hosts, custom: host !== canonical,
    setHost: (h: string) => {
      setStored(h);
      try { if (h === canonical) localStorage.removeItem(KEY); else localStorage.setItem(KEY, h); } catch { /* private mode */ }
    },
    url: (u: string) => (host === canonical ? u : withHost(u, host)),
    param: host === canonical ? undefined : host,
  }), [host, hosts, canonical]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRtspAddress(): RtspAddress {
  return useContext(Ctx);
}

export const HOST_KIND_LABEL: Record<RtspHost["kind"], string> = {
  canonical: "Main address",
  alternate: "Other network",
  browser: "Address you're using",
};
