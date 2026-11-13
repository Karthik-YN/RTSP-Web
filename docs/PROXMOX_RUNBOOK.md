# Proxmox runbook: deploy, validate and measure RIG-02 on the VM

Everything that executes (tests, Docker builds, load tests) runs **on the Proxmox VM**, not on a laptop. Each step says what to send back. Results go into `docs/ACCEPTANCE_REPORT.md`; until then those rows stay NOT RUN.

The VM (`192.168.29.105` today) needs only **Docker Engine 24+ with Compose v2.24+**, `git`, `curl` and `python3`. FFmpeg, MediaMTX and Node are inside the images.

## 0. Look before installing (the VM already runs Docker)

Nothing here changes the VM:

```bash
hostnamectl; nproc; free -h; df -h / /var/lib/docker 2>/dev/null
docker version --format '{{.Server.Version}}'; docker compose version
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
sudo ss -ltnp | grep -E ':(8080|8554|18080|18554)\b' || echo "ports free"
timedatectl show -p NTPSynchronized --value
```

**Send back:** the whole output. If 8080 or 8554 is taken by an existing service, set `RIG02_HTTP_PORT` / `RIG02_RTSP_PORT` in `.env` (step 2) instead of stopping that service.

## 1. Get the code onto the VM

The repository has no remote yet, so copy it as a git bundle. On the Mac, after committing:

```bash
git bundle create ~/rig02.bundle --all
scp ~/rig02.bundle <user>@192.168.29.105:~
```

Then on the VM:

```bash
git clone ~/rig02.bundle ~/rig02 && cd ~/rig02 && git log --oneline -1
```

To update later: make a new bundle, then run `git pull ~/rig02.bundle <branch>` on the VM.

## 2. Configure

```bash
cp .env.example .env
nano .env
#   RTSPM_PUBLIC_RTSP_HOST=192.168.29.105     (or the DNS name IT gives the VM)
#   RTSPM_RTSP_ALT_HOSTS=100.102.25.112       (the Tailscale address: any network)
#   RIG02_HTTP_PORT=8080  RIG02_RTSP_PORT=8554 (only if the defaults are taken)
```

## 3. Full validation (about 45–60 min; run it before deploying)

```bash
cd ~/rig02
scripts/test_linux.sh all 2>&1 | tee ~/rig02-validate.log
```

What `all` runs, in order (it stops at the first failure):

1. The backend test suite on Debian 12 (FFmpeg 5.1), then on Ubuntu 24.04 (FFmpeg 6.1).
2. `install.sh` in a systemd container: health check, a real stream, a backend restart with the publisher kept, MediaMTX killed and republished, the lead-in slate, polkit and backup.
3. The same install with nginx and HTTPS.
4. The `python3 app.py` launcher.
5. The production Docker image, on ports 18080 and 18554:
   - health test, including "only RTSP exposed";
   - runs as non-root;
   - upload, a stream read over RTSP, and the slate;
   - container restart: both streams come back;
   - data kept in the volume.

The systemd tests need `--privileged` containers, which is normal on a VM. Each step can also be run on its own: `scripts/test_linux.sh tests|install|tls|launcher|docker`.

**Send back:** `~/rig02-validate.log` (or just the summary lines: `passed`/`failed`, `INSTALL-TEST-PASSED`, `LAUNCHER-TEST-PASSED`, `DOCKER-TEST-PASSED`).

## 4. Deploy

```bash
docker compose up -d --build
docker compose ps                    # rig02 … (healthy) within ~40 s
```

Open `http://192.168.29.105:8080`, or the port you chose.

Make it run continuously, through reboots:

```bash
sudo systemctl enable docker tailscaled   # start on boot (the container has restart: unless-stopped)
tailscale ip -4                           # 100.102.25.112 here = reachable from every network
```

From a laptop on **another** network (the 1 Wi-Fi, home, phone data) with Tailscale signed in, open `http://100.102.25.112:8080` and play `rtsp://100.102.25.112:8554/<name>` in VLC. Details: DEPLOYMENT.md "From any network with Tailscale".

## 5. Health test

In the dashboard, open **Health → Run health test**, then **Report** to download it. Or from the shell:

```bash
curl -s -X POST http://127.0.0.1:8080/api/health/test | python3 -m json.tool
```

Expected:

- **PASS:** MediaMTX, recovery, "Internal ports not exposed", versions, folders, database, backup, headroom.
- **SKIP:** "Clock" inside the container (the VM's own clock counts; check `timedatectl` in step 0).

**Send back:** the downloaded report.

## 6. Capacity (sets the real stream limit)

1. Upload one representative test video in the dashboard: the codec, resolution and bitrate the edge will really get. Note its id (Video library → the URL, or `curl -s http://127.0.0.1:8080/api/videos`).
2. Run the ramp **inside the container**. Starts are 2 s apart, so 100 streams take about 4 min per level to start; the whole run takes about 30–40 min:

   ```bash
   docker compose exec rig02 rig02-loadtest --video-id <ID> \
       --ramp 10,20,40,60,80,100 --readers 2 --step-minutes 3 --apply
   ```

   * Readers run on the VM too, so this is slightly pessimistic, because the edge's reading costs the VM nothing. For an exact network figure, run the readers from another Linux machine with FFmpeg instead:

     ```bash
     python3 scripts/load_test.py --url http://192.168.29.105:8080 --video-id <ID> --ramp … --readers 2
     ```

   * `--apply` saves 80% of the last good level as the stream limit. You can see and change it on the Health page.
   * Ctrl-C at any time deletes the `loadtest_*` streams.
3. Copy the report out:

   ```bash
   docker compose cp rig02:/data/state/reports ./reports && ls reports
   ```

**Send back:** `reports/loadtest-ramp-*.json`.

## 7. Reboot recovery (the ticket's 2-minute target)

1. Create 16 streams; the analytics page's "Run RTSP" with 16 cameras is quickest.
2. Then:

```bash
sudo reboot
# after it is back:
curl -s http://127.0.0.1:8080/api/system/recovery | python3 -m json.tool
```

`all_ready_after_boot_s` must be ≤ 120. Also check that the dashboard opens again on `http://100.102.25.112:8080` without anyone logging in to the server.

**Send back:** that JSON.

## 8. Internet access (Cloudflare)

Follow `docs/DEPLOYMENT.md` §4c with whoever owns the company Cloudflare account. In short:

1. Create the tunnel, with the public hostname pointing to `http://rig02:8080`.
2. Create an Access application with the company e-mail policy, and copy its AUD tag.
3. Put `TUNNEL_TOKEN`, `RTSPM_CF_ACCESS_TEAM` and `RTSPM_CF_ACCESS_AUD` in `.env`.
4. Start it:

```bash
docker compose --profile cloudflare up -d
```

Check from a phone on mobile data:

- The hostname asks for the company e-mail, then the dashboard opens.
- Events show actions "by <your e-mail>".
- Upload a short video from the phone; it appears in the library.
- With WARP: VLC on a laptop outside the office plays `rtsp://192.168.29.105:8554/<name>`.

**Send back:** which of these worked.

## 9. Edge

`docs/EDGE_VALIDATION.md` (E-01 …) and `docs/SPIKE_LEADIN.md` (slate seen by the 5.x edge, no dedupe across cameras).
