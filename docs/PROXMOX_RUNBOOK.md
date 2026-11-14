# Proxmox runbook: deploy, validate and measure RIG-02 on the VM

Everything that runs (tests, Docker builds, load tests) runs **on the Proxmox VM**, not on a laptop. Each phase says what to send back. Results go into `docs/ACCEPTANCE_REPORT.md`; until then, those rows stay NOT RUN.

## The VM

| | |
|---|---|
| Address | `192.168.29.105`. It must never change, because it is inside every RTSP URL given to the edge |
| Tailscale | `100.102.25.112` |
| Needs | Docker Engine 24+ with Compose v2.24+, `git`, `curl`, `python3`. FFmpeg, MediaMTX and Node are inside the images |

**Ports:** an existing RTSP service (`/opt/rtsp-rig`, not RIG-02) already uses **8080 and 8554**. Leave it alone and run RIG-02 on **8081** (dashboard) and **8555** (RTSP). Validation uses its own test containers on 18080 and 18554. The commands below use 8081 and 8555. On a server where 8080 and 8554 are free, leave `RIG02_HTTP_PORT`/`RIG02_RTSP_PORT` unset and use those ports instead.

| Phase | What | Time | Changes the VM? |
|---|---|---|---|
| 0 | Proxmox host settings | 10 min | VM settings only |
| 1 | Pre-flight check | 5 min | No |
| 2 | Docker access, video disk | 15 min | Yes |
| 3 | Copy and verify the code | 5 min | Yes |
| 4 | Configure `.env` | 5 min | Yes |
| 5 | Full validation (`test_linux.sh all`) | 45–60 min | Test containers only |
| 6 | Back up, then deploy | 10 min | Yes |
| 7 | Smoke test from a laptop | 20 min | Test data |
| 8 | Health test | 5 min | No |
| 9 | Capacity ramp | 30–40 min | Test streams |
| 10 | Reboot recovery | 15 min | Reboots the VM |
| 11 | Login (optional) | 10 min | Yes |
| 12 | Edge, Tailscale, Cloudflare | varies | Yes |
| 13 | Operations: upgrade, rollback, backups | — | — |

**Gates:**
- Don't deploy (phase 6) until phase 5 passes.
- Don't give URLs to the edge team until phases 8 and 10 pass.

## 0. Proxmox host settings (Proxmox web UI)

Check these; change only what is missing. The reasons are in DEPLOYMENT.md §1.

- **Options → Start at boot:** yes, with start order e.g. `order=2,up=30`.
- **Options → QEMU Guest Agent:** on, and `sudo apt install -y qemu-guest-agent` in the VM.
- **Hardware:**
  - CPU type `host`, 4 vCPU or more;
  - 4 GB RAM or more;
  - NIC `virtio` on `vmbr0`;
  - disks VirtIO SCSI with `discard=on`.
- **Video storage:** a separate data disk sized for the footage. Set `backup=0` on it if it is large, and back up the footage separately.
- **Datacenter → Backup:** a daily snapshot backup of the VM.
- **Fixed address:** a DHCP reservation for the VM's MAC address, or a static IP (DEPLOYMENT.md §2).

**Send back:** CPU type, vCPUs, RAM, disks, and whether Start at boot is on.

## 1. Pre-flight check (read-only)

```bash
hostnamectl; nproc; free -h; df -h / /var/lib/docker 2>/dev/null; lsblk
docker version --format '{{.Server.Version}}'; docker compose version
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
sudo ss -ltnp | grep -E ':(8080|8081|8554|8555|18080|18554)\b'
ls -la ~/rig02 2>/dev/null && (cd ~/rig02 && git log --oneline -1 2>/dev/null)
docker volume ls | grep rig02
timedatectl show -p NTPSynchronized --value
tailscale ip -4; sudo ufw status 2>/dev/null
```

**What to look for:**
- 8080 and 8554 belong only to the existing service;
- 8081, 8555, 18080 and 18554 are free;
- whether `~/rig02` exists and is a git checkout. This decides fresh install or upgrade in phase 3.

**Send back:** the whole output.

## 2. Docker access and the video disk

Run Docker without sudo. Log out and back in afterwards:

```bash
sudo usermod -aG docker "$USER"
```

Mount the data disk. Replace `/dev/sdb` with the disk `lsblk` shows:

```bash
sudo mkfs.ext4 -L rig02videos /dev/sdb          # ONLY if the disk is new and empty
sudo mkdir -p /srv/rig02
echo 'LABEL=rig02videos /srv/rig02 ext4 defaults,noatime 0 2' | sudo tee -a /etc/fstab
sudo mount -a && sudo mkdir -p /srv/rig02/videos && sudo chown -R 10001:10001 /srv/rig02/videos
df -h /srv/rig02
```

The container runs as uid 10001. After phase 3, create `~/rig02/docker-compose.override.yml`. This keeps the tracked `docker-compose.yml` unchanged for upgrades:

```yaml
services:
  rig02:
    volumes:
      - /srv/rig02/videos:/data/videos
```

The database, thumbnails, previews and backups stay in the `rig02_rig02-data` volume.

## 3. Get the code onto the VM

The repository has no remote. It travels as a git bundle with a checksum file (HANDOVER.md). From the Mac:

```bash
scp rig02-<date>.bundle SHA256SUMS <user>@192.168.29.105:~
```

On the VM:

```bash
shasum -a 256 -c SHA256SUMS 2>/dev/null || sha256sum -c SHA256SUMS   # the bundle: OK
```

**Fresh install:**

```bash
git clone -b feature/live-preview-and-launcher ~/rig02-<date>.bundle ~/rig02
cd ~/rig02 && git log --oneline -1          # the commit named in SHA256SUMS
```

**Upgrade of an existing `~/rig02` git checkout:**

```bash
cd ~/rig02 && git status --short            # clean, apart from untracked .env / override file
git pull ~/rig02-<date>.bundle feature/live-preview-and-launcher
git log --oneline -1
```

**`~/rig02` exists but is not a git checkout:** use the release zip instead (`unzip -o rig02.zip -d ~`). It contains no `.env`, so the existing one is kept.

**Keep the folder name `rig02`.** Docker names the data volume after the folder (`rig02_rig02-data`), so a different folder name starts with an empty database.

## 4. Configure

```bash
cd ~/rig02
[ -f .env ] || cp .env.example .env
nano .env
```

```
RTSPM_PUBLIC_RTSP_HOST=192.168.29.105      # or the DNS name IT gives the VM
RTSPM_RTSP_ALT_HOSTS=100.102.25.112        # Tailscale: copyable URLs that work from any network
RIG02_HTTP_PORT=8081
RIG02_RTSP_PORT=8555
RTSPM_AUTH_ENABLED=false                   # turned on in phase 11
# RTSPM_ALERT_WEBHOOK_URL=https://...      # optional: Teams/Slack alerts
```

Leave the other defaults:
- 1280×720 standard output;
- `RTSPM_REQUIRE_16_9=true`;
- `RTSPM_MAX_ACTIVE_STREAMS=20`. Phase 9 sets the real limit.

If `ufw` is active:

```bash
sudo ufw allow 8081/tcp && sudo ufw allow 8555/tcp
```

## 5. Full validation (about 45–60 min; run it before deploying)

Run it in `tmux` so an SSH disconnect doesn't stop it:

```bash
sudo apt install -y tmux; tmux new -s val
cd ~/rig02 && scripts/test_linux.sh all 2>&1 | tee ~/rig02-validate.log
# detach: Ctrl-b d · reattach: tmux attach -t val
```

What `all` runs, in order (it stops at the first failure):

1. The backend test suite on Debian 12 (FFmpeg 5.1), then on Ubuntu 24.04 (FFmpeg 6.1).
2. `install.sh` in a systemd container:
   - health check and a real stream;
   - a backend restart that keeps the publisher running;
   - MediaMTX killed and republished;
   - the lead-in slate, polkit and backup.
3. The same install with nginx and HTTPS.
4. The `python3 app.py` launcher.
5. The production Docker image, on ports 18080 and 18554:
   - health test, including "only RTSP exposed";
   - runs as non-root;
   - upload, a stream read over RTSP, and the slate;
   - container restart: both streams come back;
   - data kept in the volume.

The systemd tests need `--privileged` containers, which is normal on a VM. Each step can also be run on its own: `scripts/test_linux.sh tests|install|tls|launcher|docker`. None of this touches the deployed RIG-02 or the existing service on 8080/8554.

**Send back:** `~/rig02-validate.log`, or at least the `passed`/`failed` lines and `INSTALL-TEST-PASSED`, `LAUNCHER-TEST-PASSED`, `DOCKER-TEST-PASSED`.

## 6. Back up, then deploy

**Upgrade only:** take a rollback point first:

```bash
cd ~/rig02
docker tag rig02-rtsp-manager:latest rig02-rtsp-manager:before-$(git rev-parse --short HEAD@{1}) 2>/dev/null
docker run --rm -v rig02_rig02-data:/data -v "$HOME":/backup debian:12 \
  tar czf /backup/rig02-data-$(date +%F).tgz -C /data state
```

Deploy:

```bash
docker compose up -d --build
docker compose ps                                        # rig02 … (healthy) within ~40 s
curl -fsS "http://127.0.0.1:8081/api/health?strict=true" && echo OK
docker compose logs --tail 50 rig02                      # no errors; the database migrates itself
sudo ss -ltnp | grep -E ':(8080|8554)\b'                 # the existing service must still be listening
```

**Make it run continuously, through reboots.** The container has `restart: unless-stopped`:

```bash
sudo systemctl enable docker tailscaled
```

**Send back:** the `ps` line and the last 50 log lines.

## 7. Smoke test from a laptop on the office network

Open `http://192.168.29.105:8081`.

1. **Upload:** upload a real 1080p H.264 clip. The card shows "Uploaded by …" with the device.
2. **720p copy:** "Make 720p copy" finishes. **Stream** the copy: it shows copy mode at 1280×720.
3. **VLC:** play `rtsp://192.168.29.105:8555/<name>`. It must play and keep looping past the end of the clip.
4. **Portrait clip:** it is refused for streaming. Make a 16:9 copy with blurred bars, then stream that.
5. **Long video:** a video over 1 h plays a 2-minute preview within a few seconds.
6. **Analytics:**
   - "Suggested analytics" links clips;
   - running 4 cameras from an analytics page works;
   - "Copy all" gives 4 URLs on port **8555**.
7. **Phone:** the dashboard has no sideways scrolling, and dialogs open full-screen.

Delete the test streams afterwards.

**Send back:** which items passed, with a screenshot of anything odd.

## 8. Health test

In the dashboard, open **Health → Run health test**, then **Report** to download it. Or from the shell:

```bash
curl -s -X POST http://127.0.0.1:8081/api/health/test | python3 -m json.tool
```

Expected:
- **PASS:** MediaMTX, recovery, "Internal ports not exposed", versions, folders, database, backup, headroom.
- **SKIP:** "Clock" inside the container. The VM's own clock counts; check `timedatectl` in phase 1.

**Send back:** the downloaded report.

## 9. Capacity (sets the real stream limit)

1. **Pick a test video.** It must be representative: the codec, resolution and bitrate the edge will really get. If streams will use 720p copies, use a 720p copy. Find its id:

   ```bash
   curl -s http://127.0.0.1:8081/api/videos | python3 -c 'import json,sys;[print(v["id"],v["filename"]) for v in json.load(sys.stdin)]'
   ```

2. **Run the ramp inside the container.** The container uses its own internal ports, so the host's 8081 doesn't matter. Starts are 2 s apart, so 100 streams take about 4 min per level to start; the whole run takes about 30–40 min.

   ```bash
   docker compose exec rig02 rig02-loadtest --video-id <ID> \
       --ramp 10,20,40,60,80,100 --readers 2 --step-minutes 3 --apply
   ```

   * The readers run on the VM too, so this is slightly pessimistic: the edge's reading costs the VM nothing. For an exact network figure, run the readers from another Linux machine with FFmpeg:

     ```bash
     python3 scripts/load_test.py --url http://192.168.29.105:8081 --rtsp-base rtsp://192.168.29.105:8555 --video-id <ID> --ramp … --readers 2
     ```

   * `--apply` saves 80% of the last good level as the stream limit. It can be seen and changed on the Health page.
   * Ctrl-C at any time deletes the `loadtest_*` streams.

3. **Copy the report out:**

   ```bash
   docker compose cp rig02:/data/state/reports ./reports && ls reports
   ```

**Send back:** `reports/loadtest-ramp-*.json`.

## 10. Reboot recovery (target: everything back within 2 min)

1. Create 16 streams. The analytics page's "Run RTSP" with 16 cameras is quickest.
2. Reboot, then check:

   ```bash
   sudo reboot
   # after it is back:
   curl -s http://127.0.0.1:8081/api/system/recovery | python3 -m json.tool
   ```

3. `all_ready_after_boot_s` must be ≤ 120.
4. VLC plays the streams again.
5. The dashboard opens on `http://100.102.25.112:8081` without anyone logging in to the server.
6. The existing service on 8080/8554 is back too.

Optional, in a maintenance window: reboot the Proxmox host to test Start at boot.

**Send back:** that JSON.

## 11. Login (optional; recommended once more people use it)

1. Set `RTSPM_AUTH_ENABLED=true` in `.env`, then:

   ```bash
   docker compose up -d
   docker compose exec rig02 rig02-users add <name> --role admin   # you type the password
   ```

2. Add colleagues on the **Users** page:
   - **operator:** upload and stream;
   - **viewer:** play and download only.
3. RTSP reads by the edge stay open; no RTSP password is needed.
4. Check that you can log in at both `http://192.168.29.105:8081` and `http://100.102.25.112:8081`, and that the session survives a page reload.

## 12. Edge, Tailscale and internet access

### Edge
Add cameras with `rtsp://192.168.29.105:8555/<name>`. Follow `docs/EDGE_VALIDATION.md` (E-01 …) and `docs/SPIKE_LEADIN.md`. They cover whether the 5.x edge sees the slate, that it doesn't dedupe across cameras, that it keeps the camera through a loop, and that it recovers after a RIG-02 restart.

### Tailscale
From a laptop on another network (another Wi-Fi, home, phone data) with Tailscale signed in, open `http://100.102.25.112:8081` and play `rtsp://100.102.25.112:8555/<name>` in VLC. Details are in DEPLOYMENT.md, "From any network with Tailscale".

### Cloudflare
Follow `docs/DEPLOYMENT.md` §4c with whoever owns the company Cloudflare account. In short:

1. Create the tunnel. Point its public hostname at `http://rig02:8080`; that is the container's own port, regardless of `RIG02_HTTP_PORT`.
2. Create an Access application with the company e-mail policy, and copy its AUD tag.
3. Put `TUNNEL_TOKEN`, `RTSPM_CF_ACCESS_TEAM` and `RTSPM_CF_ACCESS_AUD` in `.env`.
4. Start it:

   ```bash
   docker compose --profile cloudflare up -d
   ```

Check from a phone on mobile data:
- The hostname asks for the company e-mail, then the dashboard opens.
- Events show actions "by <your e-mail>".
- A short video uploaded from the phone appears in the library.
- With WARP: VLC on a laptop outside the office plays `rtsp://192.168.29.105:8555/<name>`.

**Send back:** which of these worked.

## 13. Operations

| Task | How |
|---|---|
| Logs | `docker compose logs -f rig02` (rotated, 5 × 20 MB) |
| Status | `docker compose ps`, plus the Health page (a quick check also runs every 10 min) |
| Upgrade | New bundle → `git pull <bundle> feature/live-preview-and-launcher` → rollback point (phase 6) → `docker compose up -d --build` |
| Rollback | `docker tag rig02-rtsp-manager:before-<commit> rig02-rtsp-manager:latest && docker compose up -d --no-build`. Restore the data `.tgz` only if the newer version migrated the database and the older one refuses it |
| Backups | Database: daily inside the app, 14 kept, in the volume. Whole VM: the Proxmox backup job. Footage on `/srv/rig02`: copy it separately |
| Disk | The dashboard and Health page warn early; delete unused copies from the library |

Restarting the container (an upgrade, a Docker restart) restarts the streams; readers reconnect within seconds. Plan upgrades outside demo hours.
