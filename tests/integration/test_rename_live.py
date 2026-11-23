"""Renaming a video while its streams are looping: nothing restarts, nothing errors, the RTSP feed keeps going.

(Compare test_edge_cases.py::test_ec07_source_renamed_then_restart_follows_the_file: a plain ``mv`` on the
server puts the stream in ERROR. The rename API is the safe way.)
"""

import time

from helpers import rtsp_packets, wait_for


def test_rename_while_streaming_keeps_streams_running(rig_factory):
    with rig_factory(["h264_2997.mp4"]) as rig:
        c = rig.client
        # start 15 s of a 20 s clip: a loop point every ~6 s, so loops happen after the rename
        a = rig.create("cam-a", "h264_2997.mp4", start="00:00:15")
        b = rig.create("cam-b", "h264_2997.mp4", start="00:00:15")
        rig.wait_status(a["id"], "RUNNING", 20)
        rig.wait_status(b["id"], "RUNNING", 20)
        before = {s["id"]: rig.stream(s["id"]) for s in (a, b)}
        vid = rig.video_id("h264_2997.mp4")

        r = c.post(f"/api/videos/{vid}/rename", json={"name": "ALPR_toll-s1_01.mp4"})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["streams"]) == ["cam-a", "cam-b"]
        assert not (rig.video_dir / "h264_2997.mp4").exists()

        # read the feed across at least two loop points after the rename
        packets, _, _ = rtsp_packets(rig.url("cam-a"), 14)
        assert len(packets) > 300, "RTSP feed stopped after the rename"

        for sid, old in before.items():
            now = rig.stream(sid)
            assert now["status"] == "RUNNING", now.get("last_error")
            assert now["pid"] == old["pid"] and now["restart_count"] == old["restart_count"]
            assert now["runtime"]["loop_count"] >= old["runtime"]["loop_count"] + 2
            assert now["source_rel_path"] == "ALPR_toll-s1_01.mp4"
            assert now["name"] == old["name"]  # RTSP URL unchanged

        # past the source watchdog interval, still healthy; a restart uses the new name
        time.sleep(2)
        r = c.post(f"/api/streams/{a['id']}/restart?wait=true")
        assert r.status_code == 200 and r.json()["status"] == "RUNNING"
        wait_for(lambda: rig.stream(b["id"])["status"] == "RUNNING", 5, desc="cam-b still running")
