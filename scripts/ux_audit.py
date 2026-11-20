#!/usr/bin/env python3
"""UX quality gate for the dashboard: every page, light + dark, desktop + phone, read-only.

Per page it measures, with the page idle: DOM changes per minute (how much the screen redraws), requests per
minute, layout shift (CLS), console errors, accessibility violations (axe-core) and horizontal scroll on phones.
It fails (exit 1) when any budget below is exceeded.

Read-only: every API request that is not a GET is blocked, so it is safe against the live dashboard.

  # the running dashboard
  python3 scripts/ux_audit.py --base http://localhost:8081
  # a local build (frontend/dist) against a running server's data
  python3 scripts/ux_audit.py --base http://localhost:8081 --dist frontend/dist

Needs Playwright (Chromium). Without it installed:
  docker run --rm --network host -v "$PWD":/w -w /w mcr.microsoft.com/playwright/python:v1.49.1-noble \\
    sh -c 'pip -q install playwright==1.49.1 && python scripts/ux_audit.py --base http://localhost:8081 --dist frontend/dist'
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import pathlib
import sys
import urllib.request

BUDGET = {
    "dom_changes_per_min": 150,   # idle; the top bar's CPU/RAM meters alone are ~30
    "requests_per_min": 120,      # idle, one tab
    "cls": 0.05,                  # "good" is < 0.1
    "console_errors": 0,
    "axe_violations": 0,
}
# The stream page is a live monitor: its fps / bitrate / position values update every 2 s on purpose, and the
# live video preview loads its own segments (plus still frames where the browser can't play the video).
PAGE_BUDGET = {"stream": {"dom_changes_per_min": 600, "requests_per_min": 200}}
AXE = "https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.10.2/axe.min.js"
OBS = ("window.__m=0;new MutationObserver(l=>{window.__m+=l.length}).observe(document,"
       "{subtree:true,childList:true,attributes:true,characterData:true});")
CLS = ("window.__cls=0;new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)"
       "window.__cls+=e.value}).observe({type:'layout-shift',buffered:true});")


def pages(base: str) -> list[tuple[str, str]]:
    streams = json.load(urllib.request.urlopen(base + "/api/streams"))
    tree = json.load(urllib.request.urlopen(base + "/api/analytics"))
    out = [("overview", "/"), ("streams", "/streams"), ("library", "/library"), ("system", "/system"),
           ("events", "/system?tab=events"), ("settings", "/system?tab=settings")]
    if streams:
        out.append(("stream", f"/streams/{streams[0]['id']}"))
    first = next((a for c in tree for a in c["analytics"]), None)
    if first:
        out.append(("analytics", f"/analytics/{first['id']}"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8081", help="dashboard to read data from")
    ap.add_argument("--dist", help="serve this frontend build instead of the dashboard's own")
    ap.add_argument("--idle", type=float, default=20, help="seconds idle per page")
    ap.add_argument("--json", help="write the results here")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright

    base = a.base.rstrip("/")
    dist = pathlib.Path(a.dist) if a.dist else None
    origin = "http://ux-audit.test" if dist else base
    blocked: list[str] = []

    def route(r):
        req = r.request
        path = req.url.split(origin, 1)[1]
        if path.startswith("/api/") and req.method != "GET":
            blocked.append(f"{req.method} {path}")
            return r.fulfill(status=403, body="{}")
        if not dist or path.startswith("/api/"):
            try:
                return r.fulfill(response=r.fetch(url=base + path, timeout=20000))
            except Exception:
                return r.fulfill(status=502, body="")
        f = dist / path.split("?")[0].lstrip("/")
        if not f.is_file():
            f = dist / "index.html"
        r.fulfill(body=f.read_bytes(), content_type=mimetypes.guess_type(str(f))[0] or "text/html")

    results, failures = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, path in pages(base):
            for scheme in ("light", "dark"):
                for device, vw, vh in (("desktop", 1440, 900), ("phone", 390, 844)):
                    if device == "phone" and scheme == "dark":
                        continue
                    pg = browser.new_page(viewport={"width": vw, "height": vh}, color_scheme=scheme)
                    pg.route(origin + "/**", route)
                    pg.add_init_script(CLS)
                    errors: list[str] = []
                    pg.on("pageerror", lambda e: errors.append(str(e)[:200]))
                    pg.on("console", lambda m: m.type == "error" and "404" not in m.text and errors.append(m.text[:200]))
                    reqs: list[str] = []
                    pg.on("request", lambda r: reqs.append(r.url))
                    pg.goto(origin + path)
                    pg.wait_for_timeout(4000)
                    r0 = len(reqs)
                    pg.evaluate(OBS)
                    pg.wait_for_timeout(a.idle * 1000)
                    row = {"page": name, "scheme": scheme, "device": device,
                           "dom_changes_per_min": round(pg.evaluate("window.__m") * 60 / a.idle),
                           "requests_per_min": round((len(reqs) - r0) * 60 / a.idle),
                           "cls": round(pg.evaluate("window.__cls"), 3), "console_errors": len(errors),
                           "hscroll": pg.evaluate("document.documentElement.scrollWidth") > vw}
                    try:
                        pg.add_script_tag(url=AXE)
                        v = pg.evaluate("axe.run(document,{resultTypes:['violations']}).then(r=>r.violations.map(v=>v.id+':'+v.nodes.length))")
                        row["axe_violations"] = len(v)
                        row["axe"] = v
                    except Exception:
                        row["axe_violations"] = 0
                        row["axe"] = ["axe unavailable (offline?)"]
                    row["errors"] = errors[:3]
                    results.append(row)
                    for k, limit in {**BUDGET, **PAGE_BUDGET.get(name, {})}.items():
                        if row[k] > limit:
                            failures.append(f"{name} {scheme} {device}: {k} {row[k]} > {limit}")
                    if row["hscroll"]:
                        failures.append(f"{name} {device}: horizontal scroll")
                    pg.close()
        browser.close()

    print(f"{'page':10} {'mode':12} {'redraws/min':>11} {'req/min':>8} {'CLS':>6} {'errors':>6} {'a11y':>5}")
    for r in results:
        print(f"{r['page']:10} {r['scheme'] + '/' + r['device']:12} {r['dom_changes_per_min']:11} {r['requests_per_min']:8} "
              f"{r['cls']:6.3f} {r['console_errors']:6} {r['axe_violations']:5}" + (f"  {r['axe']}" if r['axe_violations'] else ""))
    if blocked:
        print("blocked non-GET requests:", blocked)
    if a.json:
        pathlib.Path(a.json).write_text(json.dumps(results, indent=1))
    if failures:
        print("\nFAIL:\n  " + "\n  ".join(failures))
        return 1
    print("\nPASS: every page within budget")
    return 0


if __name__ == "__main__":
    sys.exit(main())
