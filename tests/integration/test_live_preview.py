"""Dashboard live preview: HLS proxied through the backend."""

import pytest

from helpers import wait_for

pytestmark = pytest.mark.integration


def test_live_preview_over_hls_proxy(rig_factory):
    with rig_factory(["h264_25fps_audio.mp4"]) as rig:
        c = rig.client
        s = rig.create("preview_me", "h264_25fps_audio.mp4")
        rig.wait_status(s["id"], "RUNNING")
        base = f"/api/streams/{s['id']}/live"

        def playlist():
            r = c.get(f"{base}/index.m3u8")
            return r if r.status_code == 200 else None

        r = wait_for(playlist, 20, interval=0.5, desc="HLS playlist")
        assert r.text.startswith("#EXTM3U")
        variant = next(ln for ln in r.text.splitlines() if ln and not ln.startswith("#"))
        v = wait_for(lambda: (x := c.get(f"{base}/{variant}")).status_code == 200 and "#EXTINF" in x.text
                     and x, 20, interval=0.5, desc="media playlist")
        media = [ln for ln in v.text.splitlines() if ln and not ln.startswith("#")]
        seg = c.get(f"{base}/{media[-1]}")
        assert seg.status_code == 200 and len(seg.content) > 1000

        # the preview must not look like a camera viewer
        rt = rig.stream(s["id"])["runtime"]
        assert rt["viewer_count"] == 0 and rt["viewer_peak_today"] == 0
        assert rt["preview_watchers"] == 1  # counted separately

        assert c.get(f"{base}/..%2F..%2Fsecret").status_code == 404  # no path tricks
        c.post(f"/api/streams/{s['id']}/stop")
        assert c.get(f"{base}/index.m3u8").status_code == 409  # not live


def test_live_preview_requires_login(rig_factory):
    with rig_factory([], mediamtx=False, auth=True) as rig:
        assert rig.client.get("/api/streams/1/live/index.m3u8").status_code == 401
