"""API responses are gzip-compressed (dashboard over the internet); live preview media is not."""


def test_json_is_compressed_and_live_media_is_not(rig_factory):
    rig = rig_factory(["h264_25fps_audio.mp4"], mediamtx=False)
    with rig:
        c = rig.client
        vid = rig.video_id("h264_25fps_audio.mp4")
        for i in range(3):
            c.post("/api/streams", json={"name": f"gz{i}", "video_id": vid, "start_now": False})
        r = c.get("/api/streams", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200 and r.headers.get("content-encoding") == "gzip"
        assert len(r.json()) == 3  # the client decodes it transparently
        sid = r.json()[0]["id"]
        live = c.get(f"/api/streams/{sid}/live/index.m3u8", headers={"Accept-Encoding": "gzip"})
        assert "content-encoding" not in live.headers
