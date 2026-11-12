"""Phase 8: RTSP addresses for users on other subnets, and the address-change watcher."""

import socket
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services import network_info
from app.services.network_info import (AddressWatcher, host_from_header, rtsp_hosts, url_host,
                                       valid_host)


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.mark.parametrize("value,expected", [
    ("rig02.lan:8080", "rig02.lan"), ("10.1.2.3", "10.1.2.3"), ("10.1.2.3:8080", "10.1.2.3"),
    ("[fd00::5]:8080", "fd00::5"), ("fd00::5", "fd00::5"), ("", None), (None, None),
    ("evil.com/x:80", None), ("a b:80", None), ("user@host:80", None),
])
def test_host_header_is_reduced_to_a_bare_valid_host(value, expected):
    assert host_from_header(value) == expected


def test_valid_host_rejects_anything_but_names_and_ips():
    assert valid_host("rig02.company.lan") and valid_host("192.168.29.105") and valid_host("::1")
    for bad in ("", "rtsp://x", "x/y", "x:80", "-bad.lan", "a..b", "x" * 300):
        assert not valid_host(bad), bad
    assert url_host("fd00::5") == "[fd00::5]" and url_host("10.0.0.1") == "10.0.0.1"


def test_choices_are_canonical_then_alternates_then_the_browser_host():
    s = _settings(public_rtsp_host="rig02.lan", rtsp_alt_hosts="10.8.0.5, RIG02.lan ,bad/host")
    assert s.rtsp_alt_hosts == ["10.8.0.5", "RIG02.lan", "bad/host"]  # CSV from the env file
    hosts = rtsp_hosts(s, "192.168.29.105")
    assert hosts == [{"host": "rig02.lan", "kind": "canonical"}, {"host": "10.8.0.5", "kind": "alternate"},
                     {"host": "192.168.29.105", "kind": "browser"}]  # duplicate + invalid dropped
    assert [h["host"] for h in rtsp_hosts(s, "10.8.0.5")] == ["rig02.lan", "10.8.0.5"]  # no repeat
    assert [h["kind"] for h in rtsp_hosts(s, "localhost")] == ["canonical", "alternate"]  # loopback skipped
    assert [h["kind"] for h in rtsp_hosts(s, "127.0.0.1")] == ["canonical", "alternate"]
    # a Cloudflare-proxied hostname carries web traffic only, never RTSP
    assert [h["kind"] for h in rtsp_hosts(s, "rig02.example.com", via_tunnel=True)] == ["canonical", "alternate"]


def test_public_url_brackets_ipv6():
    s = _settings(public_rtsp_host="rig02.lan", public_rtsp_port=8554)
    assert s.public_rtsp_url("cam1") == "rtsp://rig02.lan:8554/cam1"
    assert s.public_rtsp_url("cam1", "10.8.0.5") == "rtsp://10.8.0.5:8554/cam1"
    assert s.public_rtsp_url("cam1", "fd00::5") == "rtsp://[fd00::5]:8554/cam1"


def _fake_ifaces(monkeypatch, ips):
    def addrs():
        out = {"lo0": [SimpleNamespace(family=socket.AF_INET, address="127.0.0.1")]}
        for i, ip in enumerate(ips):
            fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
            out[f"en{i}"] = [SimpleNamespace(family=fam, address=ip)]
        out["en9"] = [SimpleNamespace(family=socket.AF_INET6, address="fe80::1%en9")]  # link-local, ignored
        return out
    monkeypatch.setattr(network_info.psutil, "net_if_addrs", addrs)


class _Events:
    def __init__(self):
        self.items = []

    def add(self, kind, message, level="INFO", **kw):
        self.items.append((kind, level, message))


def test_interface_addresses_skip_loopback_and_link_local(monkeypatch):
    _fake_ifaces(monkeypatch, ["192.168.29.105", "10.0.0.9", "fd00::5"])
    assert network_info.interface_addresses() == ["10.0.0.9", "192.168.29.105", "fd00::5"]


def test_watcher_reports_an_ip_change_and_warns_when_urls_point_at_an_old_ip(monkeypatch):
    _fake_ifaces(monkeypatch, ["192.168.29.105"])
    ev = _Events()
    w = AddressWatcher(_settings(public_rtsp_host="192.168.29.105"), ev)
    assert w.check() is None and ev.items == [] and w.warnings() == []  # first look is the baseline
    assert w.check() is None  # nothing changed
    _fake_ifaces(monkeypatch, ["192.168.29.140"])  # DHCP handed out a new address
    msg = w.check()
    assert msg and "192.168.29.105 -> 192.168.29.140" in msg
    assert ev.items[-1][:2] == ("network.address_changed", "WARNING")
    assert "no longer an address of this server" in w.warnings()[0]


def test_watcher_ignores_container_interfaces_when_told_to(monkeypatch):
    _fake_ifaces(monkeypatch, ["172.18.0.2"])  # inside a Docker bridge network
    ev = _Events()
    w = AddressWatcher(_settings(public_rtsp_host="192.168.29.105", address_watch_interfaces=False), ev)
    w.check()
    _fake_ifaces(monkeypatch, ["172.18.0.3"])
    assert w.check() is None and w.warnings() == [] and ev.items == []


def test_watcher_follows_a_dns_name_and_stays_quiet_when_it_still_resolves(monkeypatch):
    _fake_ifaces(monkeypatch, ["10.0.0.9"])
    answers = {"rig02.lan": ["10.0.0.9"]}
    monkeypatch.setattr(network_info, "resolve", lambda h: answers.get(h, []))
    ev = _Events()
    w = AddressWatcher(_settings(public_rtsp_host="rig02.lan"), ev)
    w.check()
    _fake_ifaces(monkeypatch, ["10.0.0.20"])
    answers["rig02.lan"] = ["10.0.0.20"]  # IT updated the DNS record
    msg = w.check()
    assert "rig02.lan now resolves to 10.0.0.20 (was 10.0.0.9)" in msg
    assert ev.items[-1][1] == "INFO" and w.warnings() == []  # a name keeps the edge's URLs valid


def test_config_and_url_export_offer_only_known_hosts(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False, rtsp_alt_hosts="10.8.0.5")
    with rig:
        c = rig.client
        cfg = c.get("/api/config", headers={"host": "192.168.29.105:8080"}).json()
        assert [h["host"] for h in cfg["rtsp_hosts"]] == ["rig.test", "10.8.0.5", "192.168.29.105"]
        assert isinstance(cfg["server_addresses"], list)
        # through Cloudflare without an Access token: refused before any address is shown
        assert c.get("/api/config", headers={"host": "rig02.example.com", "cf-ray": "x"}).status_code == 403

        vid = c.get("/api/videos").json()[0]["id"]
        sid = c.post("/api/streams", json={"name": "cam01", "video_id": vid, "start_now": False}).json()["id"]
        gid = c.post("/api/groups", json={"name": "g", "stream_ids": [sid]}).json()["id"]
        port = rig.ports["rtsp"]
        assert c.get(f"/api/groups/{gid}/urls").text == f"rtsp://rig.test:{port}/cam01\n"
        assert c.get(f"/api/groups/{gid}/urls?host=10.8.0.5").text == f"rtsp://10.8.0.5:{port}/cam01\n"
        assert c.get(f"/api/groups/{gid}/urls?host=192.168.29.105&format=csv",
                     headers={"host": "192.168.29.105:8080"}).text.endswith(
            f"cam01,rtsp://192.168.29.105:{port}/cam01\n")
        # a host that is not one of ours is refused, never echoed into URLs
        r = c.get(f"/api/groups/{gid}/urls?host=attacker.example")
        assert r.status_code == 400
        aid = c.get("/api/analytics").json()[0]["analytics"][0]["id"]
        assert c.get(f"/api/analytics/{aid}/urls?host=attacker.example").status_code == 400
        assert c.get(f"/api/analytics/{aid}/urls?host=10.8.0.5").status_code == 200
