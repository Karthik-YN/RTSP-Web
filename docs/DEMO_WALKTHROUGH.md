# RIG-02 demo walkthrough

A complete guide to understanding RIG-02 and presenting it to the team. Read Part 1 to know how it works, rehearse Part 4 once, and keep Part 6 open during the demo.

**Contents**

1. How it works, in plain terms
2. Where to run the demo
3. Preparation (the day before and 15 minutes before)
4. The demo script (about 25 minutes)
5. Optional extras if you have time
6. If something goes wrong during the demo
7. Questions people will ask
8. After the demo

---

## Part 1. How it works, in plain terms

### The problem it solves

Testers need recorded videos to behave like live IP cameras. Until now that meant running FFmpeg by hand on a laptop: streams died when the laptop slept, nobody knew which file was on which URL, and starting 2 minutes before an incident meant cutting the file first.

RIG-02 moves all of that onto one server with a web page. Pick a video, pick a start time, get an `rtsp://` URL, paste it into the portal.

### The pieces

```
 Browser ──────────▶ Backend (FastAPI, Python) ──── keeps the list of streams in SQLite
                        │ starts and watches
                        ▼
                     Runner ──▶ FFmpeg           one pair per stream; reads the video
                                  │ publishes    file at real-time speed
                                  ▼
 VLC / 5.x edge ◀──── MediaMTX (port 8554)       the RTSP server cameras are read from
```

| Piece | What it does | Analogy |
|---|---|---|
| **Dashboard** (React) | The web page people use | The remote control |
| **Backend** (FastAPI) | Validates requests, remembers what should be running, watches everything, restarts what fails | The manager |
| **SQLite database** | Stores videos, streams, who did what, status history | The manager's notebook |
| **FFmpeg** | Reads a video file at 1x speed and sends it out as a live stream. One process per stream | The camera |
| **Runner** | A tiny helper around each FFmpeg that writes its log and progress to disk, so the stream doesn't depend on the backend staying up | The camera's battery |
| **MediaMTX** | Receives what FFmpeg publishes and serves it to anyone who connects on port 8554 | The TV tower |

### What happens when someone clicks "Create & start"

1. The browser sends the video, name, start, end and mode to the backend.
2. The backend validates everything: the name is legal and unused, the times are inside the file, the codec is usable, capacity remains.
3. It finds the nearest **keyframe** at or before the requested start and end (more on this below).
4. It decides between **copy** (compatible H.264, almost no CPU) and **transcode** (H.265, VP9, AV1, 10-bit become H.264, costing CPU).
5. It saves the stream with desired state RUNNING and starts a Runner and FFmpeg.
6. FFmpeg publishes to MediaMTX. The backend asks MediaMTX "is this path really live?" and only then shows **Live**. A running process alone is never treated as proof.

### Four ideas worth understanding

**Desired state versus actual status.** Every stream has what people *want* (RUNNING or STOPPED) and what is *happening* (Live, Starting, Restarting, Error, and so on). All recovery is one rule: make what is happening match what people want. That's why streams come back after a crash or reboot, and why a stream someone stopped stays stopped.

**Keyframes.** Video is stored as occasional complete pictures (keyframes) with small changes in between. A copied stream can only begin on a keyframe. So if you ask for 00:00:05 and the nearest earlier keyframe is at 00:00:00, it starts at 00:00:00. The dialog shows the real start and end before you publish. Your camera clip `000003.mp4` has keyframes 10 seconds apart, so it demonstrates this nicely.

**Seamless loops.** The ticket's suggested `-stream_loop` command turned out unreliable: it can't loop a segment with an end time, and older FFmpeg loops back to the wrong place. Instead RIG-02 writes a small playlist repeating the segment, cut exactly on keyframes. Result: in a 30-minute test, all 53,947 frames arrived, none duplicated, and all 149 loops were counted.

**Publishers outlive the backend.** Because of the Runner, restarting or upgrading the backend doesn't touch running streams. The new backend re-attaches to them. Tested: a viewer stayed connected through a full backend restart with a maximum gap of 80 ms.

### What each failure looks like

| Failure | What the system does | Measured |
|---|---|---|
| MediaMTX killed | Detects it, restarts it, republishes every stream | Detected 1.6 s, streams back 2.9 s |
| FFmpeg crashes | Restarts it with increasing delays; Error after 10 failures in a row | |
| Source file deleted or replaced | Stops that stream with a clear message; others unaffected | 0.6 s |
| Backend restarts | Streams keep playing and are re-adopted (with MediaMTX running separately, as on the server) | Max gap 80 ms |
| Server reboots | Everything that should run comes back; stopped stays stopped | Not yet measured on the VM |

---

## Part 2. Where to run the demo

| | **A. Your Mac** | **B. The Proxmox VM** |
|---|---|---|
| Setup effort | None, works today | Install first (`docs/DEPLOYMENT.md`) |
| Fixed address | No, your Wi-Fi IP can change | Yes |
| Depends on your laptop staying awake | Yes | No |
| HTTPS | No | Yes (`--tls self-signed`) |
| "Backend restart doesn't drop streams" demo | Only with the extra setup in Part 5 | Yes |
| Reboot demo | No | Yes |
| Convincing for the team | Good for showing features | Best: this is the real thing |

**Recommendation:** if the VM is ready, use it. Otherwise use your Mac and say plainly: "this is running on my laptop today; the server install is the next step."

The rest of this guide assumes the Mac, and notes where the VM differs.

---

## Part 3. Preparation

### The day before

**1. Check that everyone can reach you.** Ask one colleague to open `http://<your-ip>:8080` from their laptop on the same Wi-Fi. Find your IP with:

```bash
ipconfig getifaddr en0
```

If it times out, the Wi-Fi is probably blocking laptop-to-laptop traffic. Use a wired network, a phone hotspot, or the VM.

**2. Pick the videos.** Keep to a few, and include these from `storage/videos/`:

| File | Use in the demo |
|---|---|
| `000003.mp4` | Real camera footage, main example (10 s keyframes, "long GOP" badge) |
| `h264_2997.mp4` | Test pattern with a running clock, for showing real-time playback |
| `hevc.mp4` | Shows "Needs transcode" |
| `corrupt.mp4` | Shows how a bad file is reported |

If you have a real store recording with a burnt-in clock, add it. Real footage lands better than test patterns.

**3. Install VLC** on your laptop and ask attendees to have it ready.

**4. Rehearse Part 4 once from start to finish.** Time it.

### 15 minutes before

**1. Keep the Mac awake and plugged in.** In a separate terminal:

```bash
caffeinate -d
```

**2. Start the app.** Stop any running preview first, then in a terminal (use your current IP):

```bash
cd /Users/karthiknandakumar/rtsp-stream-manager
RTSPM_PUBLIC_RTSP_HOST=192.168.68.116 \
RTSPM_DISK_RESERVE_PERCENT=1 \
RTSPM_MEM_WARN_PERCENT=95 \
scripts/start.sh
```

| Setting | Why |
|---|---|
| `RTSPM_PUBLIC_RTSP_HOST` | Copied URLs use your IP, so they work on other laptops |
| `RTSPM_DISK_RESERVE_PERCENT=1`, `RTSPM_MEM_WARN_PERCENT=95` | Hide the disk and memory warning banners your Mac would otherwise show. Laptop-only: don't use these on the server |

**3. Clean the stage.** Delete leftover streams so the demo begins from the empty state. If you want the "getting started" screen, there must be zero streams.

**4. Run the checklist:**

- [ ] Top bar shows **All systems normal**
- [ ] `http://<your-ip>:8080` opens from a second device
- [ ] VLC plays a test stream from a second device
- [ ] Theme set to Light (projectors wash out dark themes)
- [ ] Notifications off, unrelated windows closed
- [ ] Terminal open and ready for the failure demos, font enlarged
- [ ] Portal open in a browser tab, if you're showing the edge

---

## Part 4. The demo script

About 25 minutes. Text in quotes is a suggested line, not a script to read.

### 1. The problem (2 min)

> "Today, to play a recording as a camera, someone runs FFmpeg on their laptop. When it sleeps the stream dies, nobody knows which file is on which URL, and starting at a specific moment means cutting the video by hand. RIG-02 moves this onto one server with a web page. My goal is to show that anyone can publish a camera in under a minute."

Show the architecture diagram from Part 1 for 30 seconds. Don't go deeper yet.

### 2. Tour of the dashboard (2 min)

Open the dashboard: there's no login, and you land on **Streams** with the getting-started guide.

- Point to the top bar: "All systems normal", CPU, RAM, disk, stream capacity.
- Point to the sidebar: Streams, Video library, Events.

### 3. The video library (3 min)

Open **Video library**.

- "Files get here by upload, or by copying them into a folder on the server."
- Show a card: thumbnail, duration, resolution, frame rate, codec.
- Point out **Edge-ready (copy)** on an H.264 clip versus **Needs transcode** on `hevc.mp4`. Hover to show the reason.
- Scroll to **files cannot be streamed** and open it: `corrupt.mp4` is reported with the reason instead of failing later.

Optionally click **Upload video** and upload a small clip to show the progress bar.

### 4. Publish a camera in under a minute (4 min). This is the key moment.

Say "I'll time this", and start a phone stopwatch.

1. On `000003.mp4`, click **Stream this**.
2. Show that the name is suggested. Change it to `store_entrance`.
3. Click a thumbnail in the frame strip to jump to a moment. Click **Set as start**.
4. Show the green summary: "Will publish stream copy from … to …".
5. Point out the keyframe warning: "this camera has a keyframe every 10 seconds, so start times snap to the nearest one, and it tells you exactly where."
6. Click **Create & start**, then **Copy URL**. Stop the stopwatch.

Open VLC (**Media → Open Network Stream**), paste the URL, press Play.

> "That's a live RTSP camera, from a recording, in about 20 seconds."

If you have the portal open, add it as a camera now (see `docs/EDGE_VALIDATION.md`, "Standard procedure"). If the edge isn't validated yet, say so: "the edge test is the next step."

### 5. Everyone joins (3 min)

Put this on the screen:

```
Dashboard:  http://<your-ip>:8080
Stream:     rtsp://<your-ip>:8554/store_entrance   (VLC → Open Network Stream)
```

Give people a minute. Then show:

- The **Viewers** tile rising as people connect.
- Click the stream card, open the **Viewers** tab: each person's address appears.
- "There's no login: anyone on the network has full access, so please don't press Stop or Delete on someone else's stream."

Each viewer costs about 3 Mbit/s for this clip. A room of 20 on the same Wi-Fi is about 60 Mbit/s, so if playback stutters, ask a few people to close VLC.

### 6. Real time, loops, and the details page (3 min)

Create a second stream from `h264_2997.mp4` with start `00:00:04`, end `00:00:15`, Loop.

Open it in VLC:

> "The numbers in the picture are a clock. A stopwatch next to it would keep time: this plays at exactly 1x. Watch the moment it loops. There's no freeze or jump, and the loop counter goes up."

Open the stream page and walk through:

- **Overview**: playback position, loops completed, bitrate, availability bar, the exact FFmpeg command.
- **Logs**: the last lines from FFmpeg. "No more guessing what happened."
- **Take snapshot**: reads one frame back over RTSP, which proves the stream end to end.

### 7. Working together safely (3 min)

- On the `store_entrance` page, in **Note**, type "Client demo until 17:00, please don't stop", and **Save note**. It shows on the card for everyone.
- Stop the second stream yourself, and click **Undo** in the message that appears.
- Open **Events**: every action and when it happened.

> "Two people can't create the same name, and if someone changes a stream while you're looking at an old view, the server refuses rather than overwriting it."

### 8. Breaking it on purpose (4 min). The most memorable part.

Keep VLC playing where people can see it.

**a) Kill the RTSP server.** In the terminal:

```bash
pkill -9 -f "deployment/mediamtx.yml"
```

Watch the dashboard: a red "MediaMTX is down" banner, streams showing Restarting, then Live again within seconds. VLC may need Play pressed again (a real edge retries on its own, which is one thing the edge test will confirm).

> "Nobody touched anything. It noticed in under 2 seconds and every stream was back in about 3."

**b) Pull a file out from under a stream.** Only use a copy made for this purpose:

```bash
cd /Users/karthiknandakumar/rtsp-stream-manager/storage/videos
cp h264_2997.mp4 demo_copy.mp4
```

In the dashboard press **Refresh** in the library after about 10 seconds, create a stream from `demo_copy.mp4`, then:

```bash
rm demo_copy.mp4
```

The stream goes to **Error**: "Source file was deleted or moved." Other streams carry on, and the page stays up.

### 9. What's next (1 min)

> "This is tested on macOS, Debian and Ubuntu, with 16 streams and 32 viewers using about 12% of one CPU core. What's left: install it on the Proxmox server, measure there, and run the validation on the real 5.x edge. Until that's done, I won't claim the edge works."

Then take questions (Part 7).

---

## Part 5. Optional extras if you have time

**Groups for a full demo set.** On **Streams**, click **Select**, tick several streams, choose **Add to group… → + New group…**, name it "Store demo". The chip gives **Start all**, **Stop all**, **Restart all** and **Copy URLs**, which copies every RTSP URL for pasting into the portal.

**Clone.** The copy icon on a stream card opens the dialog prefilled, for a second stream with a different start time. Two streams from one file run independently.

**Validation.** In the create dialog type a start beyond the end of the file, or a name with a space: the message appears and **Create & start** stays disabled.

**Dark mode.** User menu → Theme → Dark.

**Backend restart without dropping streams.** This needs MediaMTX running on its own, as it does on the server. On the Mac:

```bash
# terminal 1
mediamtx /Users/karthiknandakumar/rtsp-stream-manager/deployment/mediamtx.yml
```

```bash
# terminal 2
cd /Users/karthiknandakumar/rtsp-stream-manager
RTSPM_MEDIAMTX_CONTROL=external RTSPM_PUBLIC_RTSP_HOST=192.168.68.116 scripts/start.sh
```

Press Ctrl+C in terminal 2 while VLC plays: the stream continues with no backend at all. Start it again: the dashboard reconnects and the stream shows the same process ID. In this mode the MediaMTX-kill demo won't self-heal, because nothing manages MediaMTX, so pick one or the other on the Mac. On the VM both work.

**On the VM only:**

- Reboot demo: `sudo reboot`, and streams return by themselves. `GET /api/system/recovery` shows the seconds after boot.
- `sudo systemctl restart rtsp-manager` while VLC plays.
- HTTPS and the real Copy button.

---

## Part 6. If something goes wrong during the demo

Stay calm and say what you're doing. Recovering live is itself a demo.

| What you see | Do this |
|---|---|
| Attendees can't open the dashboard | Check your IP hasn't changed (`ipconfig getifaddr en0`). If the Wi-Fi blocks laptops from each other, continue on your screen only |
| Dashboard won't load for you | Check the terminal running `start.sh`; restart it. Streams that should run come back |
| "Copy URL" shows a prompt | Normal on plain HTTP in some browsers: copy from the prompt |
| VLC shows nothing for several seconds | `000003.mp4` has 10 s keyframes, so a new viewer can wait up to 10 s. Use `h264_2997.mp4` if you're short of time |
| VLC stops after the kill demo | Press Play again |
| Video stutters for everyone | Too many viewers on the Wi-Fi. Ask people to close VLC |
| A stream stays on Restarting | Open it → **Logs**, which usually names the cause. Then **Restart** |
| New file doesn't appear in the library | Files are held back about 5 seconds after copying. Press **Refresh** again |
| Everything is broken | Switch to the backup: screenshots or a screen recording of your rehearsal. Make one when you rehearse |

---

## Part 7. Questions people will ask

**Does it work with the 5.x edge?**
Not proven yet. It produces standard H.264 over RTSP, but edge compatibility has to be tested on the edge. VLC working isn't proof. The runbook is `docs/EDGE_VALIDATION.md`.

**How many streams can it run?**
On this Mac, 16 copy streams with 32 viewers used about 12% of one core for all publishers, with 0 dropped frames. The server must be measured separately. The default limit is 20 streams, 4 of them transcoded.

**Why is transcoding limited?**
One 1080p transcode used about 0.5 to 0.8 of a core here, around 40 times a copied stream.

**Why didn't it start at exactly the time I typed?**
Copied video can only start on a keyframe. It goes to the nearest one before your time, and shows you where. Most camera footage has them every 1 to 2 seconds. This clip's are 10 seconds apart.

**Does it survive a reboot?**
That's the design: anything that should be running comes back, stopped stays stopped, finished play-once streams stay finished. Tested for backend restarts; a real server reboot with 16 edge cameras is still to be measured.

**What happens to audio?**
It's always dropped, as the ticket requires.

**Can someone stop my stream during a client demo?**
Yes: there's no login, so anyone on the network can. Leave a note on the stream to tell people it's in use. If that ever becomes a problem, one setting (`RTSPM_AUTH_ENABLED=true`) turns on accounts, roles and protected streams.

**Can it delete my video files?**
Deleting a stream never touches the video. Videos can't be deleted while a stream uses them, and deleted files sit in a trash folder for 7 days first.

**What if the disk fills up?**
Uploads are refused before copying, showing the space needed and available. Files copied in directly over the network bypass that check, so keep an eye on the disk warning.

**Who can publish to the server?**
Only the server itself. A publish attempt from the network was refused with 401, and the MediaMTX control API isn't reachable from the network.

**Does a looping stream ever reconnect?**
Once every 7 days at most, when its playlist runs out and a fresh publisher starts. For very short clips the limit of 50,000 loops comes sooner: a 2-second clip reaches it in about 28 hours.

**What did you test?**
184 automated tests on macOS and Debian 12, and on Ubuntu 24.04 apart from a final test-only fix re-run on its own. Also the real installer under systemd, a 30-minute loop run and a 16-stream capacity run. Results are in `docs/ACCEPTANCE_REPORT.md`.

---

## Part 8. After the demo

**Clean up**

- Delete the demo streams, and `demo_copy.mp4` if it's still there.
- Stop `caffeinate` with Ctrl+C.

**Collect**

- Which videos and scenarios people want supported.
- Feature requests and anything confusing they saw.
- Who will own the Proxmox VM, its fixed address and its DNS name.

**Next steps, in order**

1. Push the repository to your Git server.
2. Get a fixed IP or DNS name from the network admin.
3. Install on the VM (`docs/DEPLOYMENT.md`), then run `scripts/load_test.sh isolated` there.
4. Run `docs/EDGE_VALIDATION.md` on the real edge, including the 30-minute loop and the 16-camera reboot.
5. Record the results in `docs/ACCEPTANCE_REPORT.md` and the ticket.
