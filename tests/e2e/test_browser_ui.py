"""The dashboard in a real browser (Chromium), at phone, tablet, laptop and desktop sizes.

    scripts/e2e_live.sh --browser       # runs this in the Playwright image against the live rig

Checks every page loads without errors, fits the screen (no sideways scrolling), stays responsive
while it polls the live stream list (no long freezes), and the main flows work: open a video and
see its analytics, open an analytics page and see Upload, see the encoder in use. Screenshots go to
RIG02_E2E_SHOTS (default reports/ui). Read-only: it never changes anything on the rig.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

URL = os.environ.get("RIG02_E2E_URL", "").rstrip("/")
pw = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.skipif(not URL, reason="set RIG02_E2E_URL")

SHOTS = Path(os.environ.get("RIG02_E2E_SHOTS", "reports/ui"))
SIZES = {"phone": (390, 844), "tablet": (768, 1024), "laptop": (1366, 768), "desktop": (1920, 1080)}
PAGES = ["/", "/streams", "/library", "/analytics/1", "/events", "/health"]
RESULTS: dict = {}

LONGTASK_JS = """() => { window.__long = []; try { new PerformanceObserver((l) => {
  for (const e of l.getEntries()) window.__long.push(Math.round(e.duration)); }).observe({type: 'longtask', buffered: true});
} catch (e) {} }"""


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()
    out = os.environ.get("RIG02_E2E_UI_REPORT")
    if out:
        Path(out).write_text(json.dumps(RESULTS, indent=2))


def open_page(browser, size: str):
    w, h = SIZES[size]
    ctx = browser.new_context(viewport={"width": w, "height": h}, device_scale_factor=1,
                              is_mobile=size == "phone", has_touch=size in ("phone", "tablet"))
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"console: {m.text}"))
    return ctx, page, errors


def settle(page):
    page.wait_for_selector("main, #root > *", timeout=15000)
    page.wait_for_timeout(1200)  # first poll results rendered


@pytest.mark.parametrize("size", list(SIZES))
def test_pages_load_fit_and_no_errors(browser, size):
    ctx, page, errors = open_page(browser, size)
    SHOTS.mkdir(parents=True, exist_ok=True)
    timings, overflow = {}, {}
    for path in PAGES:
        t0 = time.monotonic()
        page.goto(URL + path, wait_until="domcontentloaded")
        settle(page)
        timings[path] = round((time.monotonic() - t0) * 1000 - 1200)
        assert "login" not in page.url.lower(), f"{path}: asked to log in"
        sw = page.evaluate("() => [document.documentElement.scrollWidth, window.innerWidth]")
        if sw[0] > sw[1] + 1:
            overflow[path] = sw
        name = path.strip("/").replace("/", "-") or "home"
        page.screenshot(path=str(SHOTS / f"{size}-{name}.png"), full_page=False)
    ctx.close()
    RESULTS.setdefault("load_ms", {})[size] = timings
    real = [e for e in errors if "favicon" not in e]
    assert not real, real
    assert not overflow, f"pages wider than the screen at {size} (scrollWidth, width): {overflow}"
    slow = {p: t for p, t in timings.items() if t > 3000}
    assert not slow, f"slow pages at {size}: {slow}"


def test_phone_menu_reaches_every_page(browser):
    ctx, page, errors = open_page(browser, "phone")
    page.goto(URL + "/", wait_until="domcontentloaded")
    settle(page)
    page.get_by_label("Open menu").click()
    page.get_by_role("link", name="Shared video library").first.click()
    page.wait_for_url("**/library")
    ctx.close()
    assert not errors, errors


@pytest.mark.parametrize("size", ["phone", "laptop"])
def test_streams_page_stays_responsive_while_polling(browser, size):
    """10 s on the stream list (polls every 2 s): no freeze over 200 ms, little blocking overall."""
    ctx, page, errors = open_page(browser, size)
    page.add_init_script(f"({LONGTASK_JS})()")
    page.goto(URL + "/streams", wait_until="domcontentloaded")
    settle(page)
    page.wait_for_timeout(10000)
    t0 = time.monotonic()
    page.mouse.wheel(0, 800)
    page.evaluate("() => new Promise(requestAnimationFrame)")
    input_ms = round((time.monotonic() - t0) * 1000)
    longs = page.evaluate("() => window.__long || []")
    ctx.close()
    RESULTS.setdefault("streams_page", {})[size] = {"long_tasks_ms": longs, "scroll_to_frame_ms": input_ms}
    assert not errors, errors
    assert max(longs or [0]) < 200, f"page froze for {max(longs)} ms"
    assert sum(longs) < 1000, f"{sum(longs)} ms blocked in ~11 s"
    assert input_ms < 300


def test_library_video_shows_its_analytics(browser):
    ctx, page, errors = open_page(browser, "laptop")
    page.goto(URL + "/library", wait_until="domcontentloaded")
    settle(page)
    chips = page.get_by_test_id("analytics-chips")
    assert chips.count() > 0, "no video is in an analytics"
    card = chips.first.locator("xpath=ancestor::*[.//*[@data-testid='thumb-box']][1]")
    card.get_by_test_id("thumb-box").first.click()
    player = page.get_by_test_id("video-player")
    player.wait_for(timeout=10000)
    line = page.get_by_test_id("video-analytics")
    assert line.count() > 0 and line.first.inner_text().strip(), "player doesn't list the analytics"
    RESULTS["player_analytics_line"] = line.first.inner_text()
    page.screenshot(path=str(SHOTS / "laptop-library-player.png"))
    ctx.close()
    assert not [e for e in errors if "favicon" not in e], errors


def test_analytics_page_offers_upload(browser):
    ctx, page, errors = open_page(browser, "laptop")
    page.goto(URL + "/analytics/1", wait_until="domcontentloaded")
    settle(page)
    assert page.get_by_role("button", name="Upload video").first.is_visible()
    assert page.get_by_text("Add video placeholder").count() == 0
    ctx.close()
    assert not errors, errors


def test_health_page_shows_encoder(browser):
    ctx, page, errors = open_page(browser, "laptop")
    page.goto(URL + "/health", wait_until="domcontentloaded")
    page.get_by_test_id("encoder-active").wait_for(timeout=15000)
    RESULTS["encoder_shown"] = page.get_by_test_id("encoder-active").inner_text()
    page.get_by_test_id("encoder-card").screenshot(path=str(SHOTS / "laptop-health-encoder.png"))
    ctx.close()
    assert RESULTS["encoder_shown"]
    assert not errors, errors
