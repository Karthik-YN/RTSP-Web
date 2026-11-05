# Deployment guide (Proxmox VM)

> Nothing has been installed on the Proxmox server yet; every step below is for you to run there. The installer has been executed end to end in a **Debian 12 container with systemd** (`scripts/test_linux.sh install` and `tls`): install, HTTPS, health check, a real stream, backend restart without interrupting the stream, MediaMTX kill and republish, polkit restart and backups all passed. The full test suite also passes on Debian 12 (FFmpeg 5.1) and Ubuntu 24.04 (FFmpeg 6.1). A real VM is still the final check.

## 1. Create the VM

| Setting | Recommendation | Why |
|---|---|---|
| Type | **VM** (KVM), not LXC | Clean systemd, polkit and process isolation; no nesting or AppArmor surprises with FFmpeg |
| OS | **Debian 12** or **Ubuntu 24.04** (amd64) | FFmpeg 5.1 / 6.1 with libx264; Node ≥ 18 for the frontend build |
| CPU | 4 vCPU (type `host`) | 16 stream-copy publishers use little CPU; each 1080p transcode needs about one core. Measure with `scripts/load_test.sh isolated` |
| RAM | 4 GB | Each copy publisher uses about 15–30 MB RSS |
| Disk | OS 20 GB + separate data disk for `/data/rtsp-manager/videos` sized for the footage | Keeps uploads from filling the root FS |
| Network | virtio, bridged to the office LAN (`vmbr0`) | Laptops and edges must reach port 8554 |

**LXC alternative:** unprivileged Debian 12 container with `nesting=1`. Bind-mount the video dataset (`mp0: /tank/videos,mp=/data/rtsp-manager/videos`) and make it writable by the container's mapped `rtspmgr` uid (e.g. 100000 + uid). systemd and polkit work in unprivileged LXC with nesting. Keep `ProtectSystem` etc. If a hardening directive fails inside the container (e.g. `PrivateDevices`), remove it from the unit and note it here. No GPU or devices are needed.

### Proxmox settings checklist

| Setting | Where | Why |
|---|---|---|
| **Start at boot** = yes (`onboot: 1`) | VM → Options | The VM must come back after a host reboot or power cut |
| Start/shutdown order, e.g. `order=2,up=30` | VM → Options | Start after storage and network VMs; give it time to stop cleanly |
| **QEMU Guest Agent** enabled + `apt install qemu-guest-agent` in the VM | VM → Options | Clean shutdowns and consistent backups from the host |
| Backup job (Proxmox Backup Server or vzdump), daily, mode *snapshot* | Datacenter → Backup | Whole-VM recovery; the app's own nightly backup (§5) covers state only |
| Exclude the video data disk from VM backups if it is large (`backup=0` on that disk) and back up footage separately | VM → Hardware → disk | Multi-TB footage does not belong in nightly VM backups |
| CPU type `host`, NIC `virtio`, disks `VirtIO SCSI` with `discard=on` | VM → Hardware | Performance; SSD trim |
| Firewall at the Proxmox level, if used, mirrors §3 | VM → Firewall | Defence in depth |
| `apt install unattended-upgrades` in the VM | VM shell | Security patches without manual work (does not touch MediaMTX, which is pinned) |

## 2. Fixed address and name (required)

The RTSP URLs pasted into the portal contain the server's address, so **it must never change**:

1. In Proxmox, note the VM NIC MAC address (Hardware → Network Device).
2. Either create a **DHCP reservation** for that MAC on the office DHCP server/router, **or** configure a static address on the VM (`/etc/network/interfaces` on Debian, netplan on Ubuntu) outside the DHCP pool.
3. Create a DNS name (e.g. `rig02.<office-domain>`) on the office DNS server/router pointing to that address, and use it in `RTSPM_PUBLIC_RTSP_HOST`. If no DNS is available, use the reserved IP.
4. Check from a laptop on the Vidatt Wi-Fi: `ping rig02.<office-domain>`.

The actual office IP or name is **not** known to this repository. Ask the network administrator.

## 3. Ports and firewall

| Port | Who | Expose to |
|---|---|---|
| 8554/tcp | RTSP (MediaMTX) | office LAN (edges, laptops) |
| 8080/tcp | dashboard (or 80/tcp with nginx) | office LAN |
| 9997/tcp, 9998/tcp, 8888/tcp | MediaMTX API / metrics / HLS for the dashboard preview | **loopback only** (enforced by config); the preview reaches browsers through the backend |
| 8000-8001/udp | RTP/RTCP | only if you enable UDP in `mediamtx.yml` (`rtspTransports: [tcp, udp]`) |

With ufw:

```bash
sudo ufw allow from 10.0.0.0/8 to any port 8554 proto tcp   # adjust to the office subnet
sudo ufw allow from 10.0.0.0/8 to any port 8080 proto tcp
sudo ufw enable
```

(`scripts/install.sh --configure-firewall` adds the two rules, without a source restriction, only if ufw is already active.) Check what is reachable with `scripts/healthcheck.sh`, which flags 9997/9998/MoQ/HLS/WebRTC if they are ever exposed.

## 4. Choose how to run it

| | **`python3 app.py`** (simple) | **`scripts/install.sh`** (permanent) |
|---|---|---|
| Setup | Copy the folder, run one command | Installer as root |
| Starts after a server reboot | No, unless you add the cron line below | Yes |
| Streams survive the app restarting | No: restart stops streams, then they come back | Yes: they keep playing |
| HTTPS, nightly backups, restricted service accounts | No | Yes |
| Good for | Trying it on the server, small team use | The real test rig (the ticket's reboot requirement) |

**Simple option.** On the VM:

```bash
sudo apt-get install -y python3-venv ffmpeg
cd /opt/rig02            # wherever you copied the folder
python3 app.py
```

Get the folder there with `git clone`, or from your Mac (leave out the local environment and data):

```bash
tar --exclude=backend/.venv --exclude=frontend/node_modules --exclude=tests/.fixtures --exclude=storage --exclude=bin --exclude=.git --exclude=.claude -czf ~/rig02.tar.gz -C /path/to/rtsp-stream-manager .
scp ~/rig02.tar.gz <user>@<server>:~
```

`frontend/dist` is included, so Node isn't needed on the server. To start it after a reboot (streams that were running come back with it), add to the crontab of the account that runs it (`crontab -e`):

```
@reboot cd /opt/rig02 && /usr/bin/python3 app.py --no-browser >> storage/logs/boot.log 2>&1
```

Use **one Linux account** for the folder: `storage/` belongs to whoever first ran it, so someone logged in as a different user can see that it's running, but can't start or stop it. Everyone else just uses the dashboard address.

## 4b. Install (permanent)

```bash
sudo apt-get update && sudo apt-get install -y git
git clone <this-repository> /tmp/rig02 && cd /tmp/rig02
sudo scripts/install.sh --host rig02.<office-domain>          # add --with-nginx for port 80
# recommended: HTTPS on 443 (encrypts passwords on the Wi-Fi; enables the Copy button's clipboard API)
sudo scripts/install.sh --host rig02.<office-domain> --with-nginx --tls self-signed
# or with the office's own certificate:  --tls /path/server.crt:/path/server.key
```

With `--tls self-signed`, each browser asks once to trust the certificate. To avoid that, issue a certificate from the office CA for the server's name and pass it with `--tls CRT:KEY`.

What `install.sh` does (idempotent; re-run it to upgrade):

1. installs `ffmpeg python3-venv curl rsync` (plus `nodejs npm` if `frontend/dist` is not prebuilt, and nginx with `--with-nginx`), and warns if FFmpeg < 5.1 or has no libx264;
2. creates the system users `mediamtx` and `rtspmgr` (no login shell);
3. creates `/opt/rtsp-manager` (code, root-owned), `/var/lib/rtsp-manager` (DB, logs, thumbnails, playlists; 0750 rtspmgr), `/data/rtsp-manager/videos` (2775 rtspmgr:rtspmgr: add team members to group `rtspmgr` so they can copy files in), and `/etc/rtsp-manager` (0750 root:rtspmgr);
4. copies the code, builds the frontend if needed, and creates the Python venv;
5. downloads MediaMTX `v1.21.2` for the VM architecture, **verifies its SHA-256** against the release checksums, and installs it to `/usr/local/bin/mediamtx` (an existing binary is kept unless `--upgrade-mediamtx` is given);
6. checks that this MediaMTX binary with our config exposes **only RTSP** (`scripts/check_mediamtx_exposure.py`); on `--upgrade-mediamtx` a failing new binary is rolled back automatically;
7. installs `/etc/mediamtx/mediamtx.yml`, `/etc/systemd/system/{mediamtx,rtsp-manager}.service` and `/etc/polkit-1/rules.d/50-rtsp-manager.rules`. **An existing file that differs is never overwritten**: ours is written as `*.rig02-new` and a warning is printed. Review it with `diff -u`;
8. writes `/etc/rtsp-manager/rtsp-manager.env` from `.env.example` with your host (only if missing);
9. leaves login off (direct access for everyone who can reach the server);
10. enables both services and the nightly backup timer, starts them, then runs `scripts/healthcheck.sh`.

**If MediaMTX is already installed for something else**, the installer keeps its binary and config. Our config is only written as `.rig02-new`. Decide with the owner of that service before replacing anything.

### Configuration

Edit `/etc/rtsp-manager/rtsp-manager.env` (all variables are documented in `.env.example` and `backend/app/config.py`), then `sudo systemctl restart rtsp-manager`. Important settings:

* `RTSPM_PUBLIC_RTSP_HOST`: the fixed name/IP used in URLs.
* `RTSPM_MAX_ACTIVE_STREAMS` / `RTSPM_MAX_TRANSCODE_STREAMS`: capacity limits; set them from your load test.
* `RTSPM_TRANSCODE_POLICY`, `RTSPM_COPY_*`: edge compatibility; set them from the edge validation.
* `RTSPM_LISTEN_HOST=127.0.0.1`: when nginx is in front.

### Access

By default there is **no login**: anyone who can reach the server has full access, including stopping and deleting streams, deleting videos and restarting MediaMTX. Keep the server on the office network only; don't forward it to the internet.

To require logins later, set `RTSPM_AUTH_ENABLED=true` in `/etc/rtsp-manager/rtsp-manager.env`, create the first admin, and restart. That also brings back roles (admin, operator, viewer), the Users page and protected streams:

```bash
sudo -u rtspmgr env RTSPM_ENV_FILE=/dev/null RTSPM_DATA_DIR=/var/lib/rtsp-manager /opt/rtsp-manager/backend/.venv/bin/python /opt/rtsp-manager/scripts/manage_users.py add admin --role admin
sudo systemctl restart rtsp-manager
```

## 5. Operate

```bash
sudo systemctl status mediamtx rtsp-manager          # state
sudo systemctl restart rtsp-manager                   # backend restarts; streams KEEP PLAYING and are re-adopted
sudo systemctl stop rtsp-manager                      # dashboard stops; streams keep playing
sudo -u rtspmgr /opt/rtsp-manager/scripts/stop_publishers.sh   # stop every stream (desired state kept)
sudo systemctl restart mediamtx                       # publishers drop and are republished automatically
journalctl -u rtsp-manager -f                         # backend log
journalctl -u mediamtx -f                             # MediaMTX log
sudo tail -f /var/lib/rtsp-manager/logs/streams/<id>.log   # one stream's FFmpeg log
scripts/healthcheck.sh --rtsp                         # services, ports, API, RTSP of every running stream
```

**Backups.** `rtsp-manager-backup.timer` runs `scripts/backup.sh` nightly at 02:30. It writes an online-consistent copy of the database plus the users, env and MediaMTX config to `/var/backups/rtsp-manager/<date>/` and keeps 14 days. Run it by hand with `sudo /opt/rtsp-manager/scripts/backup.sh`. Videos are user data and are not included. Restore:

```bash
sudo systemctl stop rtsp-manager
sudo -u rtspmgr cp /var/backups/rtsp-manager/<date>/rtsp-manager.db /var/lib/rtsp-manager/rtsp-manager.db
sudo -u rtspmgr cp /var/backups/rtsp-manager/<date>/users /var/lib/rtsp-manager/users
sudo systemctl start rtsp-manager      # streams that should run are republished
```

**Deleted videos** go to `/data/rtsp-manager/videos/.trash/` and are purged after `RTSPM_TRASH_DAYS` (7). To restore one, move it back into the videos folder and press Refresh.

**Upgrade.** `git pull` in your checkout and re-run `sudo scripts/install.sh`. Database migrations are applied automatically on start. Running streams are **not interrupted**: publishers keep playing and the new backend re-adopts them. A publisher whose command changed in the new version is replaced, with one brief reconnect. To upgrade MediaMTX, add `--upgrade-mediamtx --mediamtx-version vX.Y.Z`: the new binary is checksum-verified and exposure-checked, and rolled back if it would open extra ports.

**Alerts.** Set `RTSPM_ALERT_WEBHOOK_URL` to a Teams or Slack incoming webhook to be told about stream errors, lost source files, MediaMTX down/up, boot recovery results and resource warnings (rate-limited to once per 5 min per alert). **Prometheus**: scrape `https://rig02/api/metrics` with Basic auth (a `viewer` account is enough).

## 6. Verify after install

1. **Dashboard:** open `http://rig02.<office-domain>:8080/` from a laptop on the Wi-Fi. The health bar shows MediaMTX **UP**, and there is no "RTSPM_PUBLIC_RTSP_HOST is not set" warning.
2. **Library:** copy a clip to `/data/rtsp-manager/videos/` (or upload one) and press Refresh. The metadata and thumbnail appear.
3. **Publish:** New stream → pick the clip → Create & start → copy the URL.
4. **RTSP from a laptop:** `scripts/validate_rtsp.sh rtsp://rig02.<office-domain>:8554/<name>` must print `PASS`; `ffplay -rtsp_transport tcp <url>` or VLC show the picture (this is not proof of edge compatibility).
5. **Security:** from a laptop, `curl http://rig02:9997/v3/paths/list` must **fail** (connection refused); `scripts/healthcheck.sh` on the server shows no unexpected exposed ports.

## 7. Verify recovery

**MediaMTX crash (EC-09):**

```bash
sudo systemctl kill -s SIGKILL mediamtx     # systemd restarts it within ~1 s
# dashboard: red "MediaMTX is down" banner within 10 s, then streams RESTARTING -> RUNNING
journalctl -u rtsp-manager --since -2min | grep -Ei 'mediamtx|republish'
```

To test the backend's own recovery path (without systemd's restart), stop it instead: `sudo systemctl stop mediamtx`. The backend starts it again via polkit within about 5–10 s.

**Reboot (EC-10):** on a laptop, put the 16 URLs into `urls.txt` and run `python3 scripts/measure_recovery.py urls.txt`. Then `sudo reboot` the VM. Afterwards:

```bash
uptime -s                                   # boot time
curl -s -u admin:... http://127.0.0.1:8080/api/system/recovery | python3 -m json.tool
```

`all_ready_after_boot_s` must be < 120 and every stream `RUNNING`. Record the edge camera status by hand as in EDGE_VALIDATION E-10.
