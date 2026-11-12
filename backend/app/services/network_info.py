"""Addresses people use to reach the rig, and a watcher for address changes.

RTSP URLs are built from a host *name*, never stored. Users on other subnets
(VPN, other office LANs) may need a different address than the canonical
``public_rtsp_host``, so the API offers a short validated list to choose from:

1. the canonical host (``RTSPM_PUBLIC_RTSP_HOST``), ideally a DNS name;
2. extra hosts from ``RTSPM_RTSP_ALT_HOSTS`` (for example a VPN-side IP);
3. the host the browser used to reach this dashboard, when it is a usable LAN
   name or IP (not loopback, not a Cloudflare-proxied public hostname, since
   RTSP does not travel through a web tunnel).

Only hosts from this list are accepted for URL exports, so a request can never
make the API print an arbitrary host.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import re
import socket

import psutil

from ..config import Settings

log = logging.getLogger(__name__)

_HOSTNAME = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")
LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def valid_host(host: str) -> bool:
    """A bare DNS name or IP literal (no port, scheme, path or user info)."""
    if not host:
        return False
    return _ip(host) is not None or bool(_HOSTNAME.match(host))


def is_loopback(host: str) -> bool:
    ip = _ip(host)
    return (ip is not None and ip.is_loopback) or host.lower().rstrip(".") in LOOPBACK_NAMES


def url_host(host: str) -> str:
    """Host as written inside a URL (IPv6 literals in brackets)."""
    ip = _ip(host)
    return f"[{ip.compressed}]" if isinstance(ip, ipaddress.IPv6Address) else host


def host_from_header(value: str | None) -> str | None:
    """'name:8080' / '[fe80::1]:8080' / '10.0.0.5' -> bare host, or None."""
    if not value:
        return None
    value = value.strip()
    if value.startswith("["):
        host = value[1:value.find("]")] if "]" in value else ""
    else:
        host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    return host if valid_host(host) else None


def interface_addresses() -> list[str]:
    """Current non-loopback, non-link-local IPs of this machine, IPv4 first."""
    v4: set[str] = set()
    v6: set[str] = set()
    with contextlib.suppress(OSError):
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family not in (socket.AF_INET, socket.AF_INET6):
                    continue
                ip = _ip(a.address.split("%", 1)[0])
                if ip is None or ip.is_loopback or ip.is_link_local or ip.is_multicast:
                    continue
                (v4 if ip.version == 4 else v6).add(ip.compressed)
    return sorted(v4, key=lambda s: tuple(int(p) for p in s.split("."))) + sorted(v6)


def rtsp_hosts(settings: Settings, request_host: str | None = None,
               via_tunnel: bool = False) -> list[dict]:
    """Ordered, de-duplicated choices: [{host, kind}] with kind canonical|alternate|browser."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(host: str, kind: str) -> None:
        key = host.lower().rstrip(".")
        if key and key not in seen and valid_host(host):
            seen.add(key)
            out.append({"host": host, "kind": kind})

    add(settings.public_rtsp_host, "canonical")
    for h in settings.rtsp_alt_hosts:
        add(h, "alternate")
    if request_host and not via_tunnel and not is_loopback(request_host):
        add(request_host, "browser")
    return out


def resolve(host: str) -> list[str]:
    """IPs a name resolves to on this machine (sorted); [] when it does not resolve."""
    ip = _ip(host)
    if ip is not None:
        return [ip.compressed]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({i[4][0].split("%", 1)[0] for i in infos})


class AddressWatcher:
    """Notices when this machine's IPs, or what the canonical name resolves to, change.

    A change means URLs saved on the edge or by remote users may stop working,
    so it is reported as a ``network.address_changed`` event (and webhook alert).
    """

    def __init__(self, settings: Settings, events, interval_s: float = 60.0):
        self.s = settings
        self.events = events
        self.interval_s = interval_s
        self.addresses: list[str] = []
        self.canonical_ips: list[str] = []
        self._task: asyncio.Task | None = None

    def snapshot(self) -> tuple[list[str], list[str]]:
        return interface_addresses(), resolve(self.s.public_rtsp_host)

    def check(self) -> str | None:
        """Take a new snapshot; returns the change message (and records the event) or None."""
        addrs, canon = self.snapshot()
        first = not self.addresses and not self.canonical_ips
        old_addrs, old_canon = self.addresses, self.canonical_ips
        self.addresses, self.canonical_ips = addrs, canon
        if first:
            return None
        parts = []
        if addrs != old_addrs and self.s.address_watch_interfaces:
            parts.append(f"server addresses {', '.join(old_addrs) or 'none'} -> {', '.join(addrs) or 'none'}")
        if canon != old_canon and not is_loopback(self.s.public_rtsp_host):
            parts.append(f"{self.s.public_rtsp_host} now resolves to {', '.join(canon) or 'nothing'}"
                         f" (was {', '.join(old_canon) or 'nothing'})")
        if not parts:
            return None
        msg = "Network address changed: " + "; ".join(parts)
        level = "WARNING" if self.canonical_unreachable() else "INFO"
        self.events.add("network.address_changed", msg, level=level,
                        data={"addresses": addrs, "canonical_ips": canon})
        log.warning(msg)
        return msg

    def canonical_unreachable(self) -> bool:
        """The canonical host is a raw IP that is no longer on any interface of this machine."""
        ip = _ip(self.s.public_rtsp_host)
        if not self.s.address_watch_interfaces or ip is None or ip.is_loopback or not self.addresses:
            return False
        return ip.compressed not in self.addresses

    def warnings(self) -> list[str]:
        if self.canonical_unreachable():
            return [f"RTSP URLs use {self.s.public_rtsp_host}, which is no longer an address of this server "
                    f"(now: {', '.join(self.addresses)}). URLs saved on the edge will not connect. Give the "
                    "server a fixed address (DHCP reservation) or a DNS name, then set RTSPM_PUBLIC_RTSP_HOST."]
        return []

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self.check)

    def start(self) -> None:
        with contextlib.suppress(Exception):
            self.check()
        self._task = asyncio.create_task(self.run(), name="address-watcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
