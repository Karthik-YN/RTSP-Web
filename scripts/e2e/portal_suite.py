"""End-to-end: the RTSP Studio portal in a real browser (Playwright, Chromium), desktop and phone.

  readonly (safe on live): every page opens without errors or failed API calls; what the pages show matches the
                           API (stream count, RTSP URLs, viewer chips); a stream page shows the live preview (or still
                           frames: headless Chromium has no H.264); top-bar rig clock = server time; the New stream
                           dialog checks with the server and says Ready (nothing is created).
  full (preview only):     creates e2e-ui-1 through the New stream dialog (segment, play once/loop, clock UTC), checks
                           the success URL, the stream page, Stop and Start from the page. The edge suite deletes e2e-*.

python portal_suite.py --base http://192.168.29.105:8082 --mode full --json out.json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time

from playwright.sync_api import sync_playwright

R: list[dict] = []


def rec(area, check, target, status, detail=""):
    R.append(dict(area=area, check=check, target=target, status=status, detail=detail))
    print(f"  [{status:4}] {area:8} {check:34} {target:26} {detail}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--mode", choices=["readonly", "full"], default="readonly")
    ap.add_argument("--json")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    t_start = time.time()
    print(f"== RTSP Studio portal suite: {base} ({a.mode}) ==")
    with sync_playwright() as p:
        b = p.chromium.launch()
        api = p.request.new_context(base_url=base)
        streams = api.get("/api/streams").json()
        running = [s for s in streams if s["status"] == "RUNNING" and not s["name"].startswith("e2e-")]
        cfg = api.get("/api/config").json()

        for dev, vp in (("desktop", {"width": 1440, "height": 900}), ("phone", {"width": 390, "height": 844})):
            ctx = b.new_context(viewport=vp, is_mobile=dev == "phone", has_touch=dev == "phone")
            pg = ctx.new_page()
            errs: list[str] = []
            bad: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)[:120]))
            pg.on("console", lambda m: errs.append(m.text[:120]) if m.type == "error" and "Failed to load resource" not in m.text else None)
            pg.on("response", lambda r: bad.append(f"{r.status} {r.url.replace(base, '')}") if "/api/" in r.url and r.status >= 500 else None)
            pages = ["/", "/streams", "/library", "/system", "/system?tab=events", "/system?tab=settings", "/clock"]
            an = api.get("/api/analytics").json()
            aid = next((x.get("id") for x in (an if isinstance(an, list) else an.get("items", an.get("analytics", [])))
                        if isinstance(x, dict) and x.get("id")), None) if an else None
            if aid:
                pages.append(f"/analytics/{aid}")
            if running:
                pages.append(f"/streams/{running[0]['id']}")
            for path in pages:
                n0, b0 = len(errs), len(bad)
                t0 = time.monotonic()
                pg.goto(base + path, wait_until="domcontentloaded")
                try:
                    pg.wait_for_selector("main h1, main h2", timeout=10000)
                    ok = True
                except Exception:
                    ok = False
                load = time.monotonic() - t0
                pg.wait_for_timeout(2500)
                st = "PASS" if ok and len(errs) == n0 and len(bad) == b0 else "FAIL"
                if st == "PASS" and load > 3:
                    st = "WARN"
                over = pg.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
                if over and st == "PASS":
                    st = "WARN"
                rec("portal", f"page opens ({dev})", path, st,
                    f"content in {load:.1f} s" + (f" · errors: {errs[n0:][:2]}" if len(errs) > n0 else "")
                    + (f" · API: {bad[b0:][:2]}" if len(bad) > b0 else "") + (" · sideways scroll" if over else ""))
            ctx.close()

        # what the Streams page shows = the API
        pg = b.new_page(viewport={"width": 1440, "height": 900})
        pg.goto(base + "/streams")
        pg.wait_for_timeout(4000)
        body = pg.inner_text("body")
        names = [s["name"] for s in streams]
        missing = [n for n in names if n not in body]
        rec("portal", "every stream listed", f"{len(names)} streams", "PASS" if not missing else "FAIL",
            f"missing: {missing[:5]}" if missing else "all names shown")
        html = pg.content()
        urls = [s["rtsp_url"] for s in streams]
        shown = sum(1 for u in urls if u in html)
        rec("portal", "RTSP URLs on the page = API", f"{len(urls)} URLs", "PASS" if shown >= len(urls) * 0.9 else "WARN",
            f"{shown} found in the page (copy buttons / links)")

        # top-bar rig clock vs server time
        chip = pg.locator("a[href='/clock']").first
        txt = chip.inner_text() if chip.count() else ""
        m = re.search(r"(\d{2}):(\d{2})", txt)
        now = api.get("/api/system/time").json()["epoch_ms"] / 1000 + 19800
        ist = dt.datetime.fromtimestamp(now, dt.timezone.utc)
        if m:
            diff = abs((int(m[1]) * 60 + int(m[2])) - (ist.hour * 60 + ist.minute))
            rec("clock", "top-bar clock = server IST", txt.strip()[:20], "PASS" if diff <= 1 or diff >= 1439 else "FAIL",
                f"server {ist:%H:%M} IST")
        pg.goto(base + "/clock")
        pg.wait_for_timeout(2500)
        rc = pg.get_by_test_id("rig-clock").inner_text()
        rec("clock", "Rig clock page (IST + UTC, ms)", "/clock", "PASS" if "IST" in rc and "UTC" in rc and re.search(r"\.\d{3}", rc) else "FAIL",
            rc.replace("\n", " ")[:70])

        # stream page: live preview (video playing, or still frames in headless Chromium without H.264)
        if running:
            s = running[0]
            pg.goto(f"{base}/streams/{s['id']}")
            state = "none"
            for _ in range(30):
                pg.wait_for_timeout(1000)
                t = pg.evaluate("(() => { const v = document.querySelector('video[aria-label=\"Live preview\"]'); return v ? v.currentTime : -1 })()")
                if t and t > 1:
                    state = f"video playing ({t:.1f} s)"
                    break
                if pg.get_by_test_id("preview-stills").count():
                    state = "still frames (headless Chromium has no H.264; real browsers play the video)"
                    break
            rec("portal", "stream page live preview", s["name"], "PASS" if state != "none" else "FAIL", state)
            body = pg.inner_text("body")
            rec("portal", "stream page shows its RTSP URL", s["name"], "PASS" if s["rtsp_url"] in body or s["rtsp_url"] in pg.content() else "FAIL", s["rtsp_url"])

        # New stream dialog: server check says Ready (nothing created here)
        pg.goto(base + "/streams")
        pg.wait_for_timeout(2500)
        pg.get_by_role("button", name="New stream").first.click()
        d = pg.get_by_role("dialog")
        try:
            pg.wait_for_function("document.querySelector('[data-testid=create-status]')?.textContent.startsWith('Ready')", timeout=15000)
            rec("portal", "New stream dialog checks + Ready", "dialog", "PASS", d.get_by_test_id("create-status").inner_text())
        except Exception:
            rec("portal", "New stream dialog checks + Ready", "dialog", "FAIL", d.get_by_test_id("create-status").inner_text())
        pg.keyboard.press("Escape")

        if a.mode == "full":
            full(pg, api, base, cfg)
        b.close()
    tally = {k: sum(1 for r in R if r["status"] == k) for k in ("PASS", "WARN", "FAIL", "SKIP")}
    print(f"== {tally} in {time.time() - t_start:.0f} s ==")
    if a.json:
        json.dump(dict(base=base, mode=a.mode, tally=tally, results=R), open(a.json, "w"), indent=1)
    return 1 if tally["FAIL"] else 0


def full(pg, api, base, cfg):
    name = "e2e-ui-1"
    pg.goto(base + "/streams")
    pg.wait_for_timeout(2500)
    pg.get_by_role("button", name="New stream").first.click()
    d = pg.get_by_role("dialog")
    d.get_by_label("Stream name").fill(name)
    d.get_by_label("Start time").fill("00:00:02")
    d.get_by_label("End time").fill("00:00:20")
    if d.get_by_test_id("clock-field").count():
        cf = d.get_by_test_id("clock-field")  # click the visible labels, as a user does
        cf.get_by_text("On", exact=True).click()
        cf.get_by_text("UTC", exact=True).click()
        if not (d.get_by_role("radio", name="On", exact=True).is_checked() and d.get_by_role("radio", name="UTC", exact=True).is_checked()):
            rec("create", "clock On + UTC selectable", name, "FAIL", "clicking the labels did not select them")
    posted = []
    pg.on("request", lambda r: posted.append(r.post_data_json) if r.method == "POST" and r.url.endswith("/api/streams") else None)
    pg.wait_for_function("document.querySelector('[data-testid=create-status]')?.textContent.startsWith('Ready')", timeout=20000)
    pg.get_by_role("button", name="Create & start").click()
    try:
        pg.wait_for_selector("[data-testid=created-url]", timeout=20000)
        url = pg.get_by_test_id("created-url").inner_text()
        exp = cfg["rtsp_url_template"].replace("{name}", name)
        body = posted[0] if posted else {}
        rec("create", "create from the UI dialog", name, "PASS" if url == exp else "FAIL", url)
        rec("create", "dialog sends what was chosen", name,
            "PASS" if body.get("start") == "00:00:02" and body.get("end") == "00:00:20" and body.get("mode") == "loop"
            and body.get("clock") in (True, None) and body.get("clock_tz") in ("UTC", None) and body.get("output_size") == "original" else "FAIL",
            json.dumps({k: body.get(k) for k in ("start", "end", "mode", "clock", "clock_tz", "output_size", "lead_in_mode")}))
    except Exception as e:  # noqa: BLE001
        rec("create", "create from the UI dialog", name, "FAIL", str(e)[:100])
        return
    pg.get_by_role("button", name="Done").click()
    s = next((x for x in api.get("/api/streams").json() if x["name"] == name), None)
    if not s:
        rec("create", "stream exists after create", name, "FAIL")
        return
    ok = False
    for _ in range(40):
        x = api.get(f"/api/streams/{s['id']}").json()
        if x["status"] == "RUNNING":
            ok = True
            break
        time.sleep(1)
    rec("create", "UI-created stream runs", name, "PASS" if ok else "FAIL", f"status {x['status']}, clock {x.get('clock')} {x.get('clock_tz')}")
    pg.goto(f"{base}/streams/{s['id']}")
    pg.wait_for_timeout(3000)
    pg.get_by_role("button", name="Stop", exact=True).first.click()
    cb = pg.get_by_role("button", name=re.compile("^Stop"))
    if pg.get_by_role("dialog").count():  # confirmation
        pg.get_by_role("dialog").get_by_role("button", name=re.compile("Stop")).last.click()
    st = None
    for _ in range(30):
        st = api.get(f"/api/streams/{s['id']}").json()["status"]
        if st == "STOPPED":
            break
        time.sleep(1)
    rec("control", "Stop from the stream page", name, "PASS" if st == "STOPPED" else "FAIL", f"status {st}")
    pg.wait_for_timeout(1500)
    pg.get_by_role("button", name="Start", exact=True).first.click()
    for _ in range(40):
        st = api.get(f"/api/streams/{s['id']}").json()["status"]
        if st == "RUNNING":
            break
        time.sleep(1)
    rec("control", "Start from the stream page", name, "PASS" if st == "RUNNING" else "FAIL", f"status {st}")


if __name__ == "__main__":
    sys.exit(main())
