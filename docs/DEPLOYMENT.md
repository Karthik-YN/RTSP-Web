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

### Users on other subnets or the VPN

The dashboard works from any address that reaches port 8080, because the UI talks to the API on the same address it was loaded from. Only the RTSP URLs contain a server address, and that address must be reachable from where the URL is used.

* **Best:** one DNS name that resolves on every subnet and over the VPN (an A-record on the company DNS, which the VPN hands out). Use it as `RTSPM_PUBLIC_RTSP_HOST`. Then nobody needs a different URL, and a later IP change only needs a DNS update. `.local` (mDNS) names do **not** work across subnets or the VPN.
* **If some networks reach the server under another IP or name** (for example a NAT or VPN-side address), list it in `RTSPM_RTSP_ALT_HOSTS=10.8.0.5,…`. The sidebar's **URLs use** picker then lets each person choose the address their copied URLs contain (remembered per browser). The picker also offers the address the page was opened with. URL exports accept `?host=` with one of these addresses only.
* **Routing:** RTSP runs over TCP only (UDP is rejected), so it crosses routers, VPNs and NAT. Between subnets, only TCP 8554 and 8080 must be allowed (see the table below).
* **The edge** should always get the main address, never a personal VPN address.

The backend checks every 60 s (`RTSPM_ADDRESS_CHECK_INTERVAL_S`) whether its own IPs, or what `RTSPM_PUBLIC_RTSP_HOST` resolves to, changed. A change is logged as `network.address_changed` (and sent to the alert webhook). If `RTSPM_PUBLIC_RTSP_HOST` is a raw IP that is no longer on the server, the dashboard shows a warning that the edge's URLs no longer connect.

#### Office with chained routers (several Wi-Fi networks behind each other)

Measured in the office on 2026-11-12 (`traceroute` from a laptop on the mesh Wi-Fi):

```
Internet ← 192.168.1.1 (ISP router) ← 192.168.29.1 (router) ← 192.168.68.1 (mesh Wi-Fi)
```

Each router does NAT, so connections only work **upwards**. A device reaches its own network and every network *above* it, never one *below* it:

| Server on | Reachable from 68 Wi-Fi | from 29 | from 1 |
|---|---|---|---|
| 192.168.68.x | yes | **no** | **no** |
| 192.168.29.x (the VM: 192.168.29.105) | yes | yes | **no** (unless port forwarding, below) |
| 192.168.1.x | yes | yes | yes |

No app setting can change this, but **Tailscale (next section) bypasses it**: the server's Tailscale address works from every network. Otherwise:

* Use the server's address on the **highest** network it is on as `RTSPM_PUBLIC_RTSP_HOST`. For the VM on 29 that is `192.168.29.105`, which works from the 68 and 29 Wi-Fis.
* For the **1 network** as well, choose one:
  * Put the VM's NIC (the Proxmox bridge) on the 1 network, with a DHCP reservation from 192.168.1.1; then every Wi-Fi reaches it.
  * Or, on the 192.168.29.1 router, forward TCP 8080 and 8554 to the VM. Users on 1 then use that router's WAN address (its 192.168.1.x IP); list it in `RTSPM_RTSP_ALT_HOSTS`.
  * Or flatten the network: set the mesh (and the 29 router) to access point or bridge mode, so all Wi-Fis are one network.
* A guest Wi-Fi with "client isolation" blocks devices from each other entirely; don't use it for the edge or for viewers.

To check from a Windows PC: `ipconfig | findstr IPv4`, then `Test-NetConnection <server> -Port 8554` (and `-Port 8080`). `TcpTestSucceeded : True` means VLC and the dashboard will work.

What to ask IT for:

1. A DHCP reservation (or a static IP) for the VM NIC's MAC address.
2. A DNS A-record, for example `rig02.<company-domain>`, resolvable from the office LANs and the VPN.
3. Firewall: TCP 8554 and 8080 from the office subnets and the VPN range to the VM.

### From any network with Tailscale (recommended for the office)

Tailscale gives the server one more address (here `100.102.25.112`) that every device **signed in to your tailnet** reaches from anywhere: the 1, 29 and 68 Wi-Fis, home, mobile data. Unlike a web tunnel it carries **both** the dashboard and RTSP, so VLC and ffprobe work over it too. Nothing is opened to the public internet, and no router has to change.

On the server that runs Docker (one-time; Tailscale is already installed there if `tailscale ip -4` prints the 100.x address):

```bash
tailscale ip -4                              # must print 100.102.25.112 on THIS machine
sudo systemctl enable --now tailscaled docker   # both start on every boot
```

In `.env`, keep the LAN address as the main one (the edge uses it) and add the Tailscale one:

```
RTSPM_PUBLIC_RTSP_HOST=192.168.29.105
RTSPM_RTSP_ALT_HOSTS=100.102.25.112
```

Then `docker compose up -d`. Docker publishes 8080 and 8554 on every interface, Tailscale's included, so nothing else is needed.

For each person: install Tailscale (Windows, macOS, Linux, iOS, Android), sign in to the company tailnet (or accept a share of this one machine from the Tailscale admin console), then open:

* dashboard: `http://100.102.25.112:8080`
* streams: `rtsp://100.102.25.112:8554/<name>`

A dashboard opened over Tailscale copies Tailscale URLs by default; the **URLs use** picker in the sidebar marks that address "Tailscale, any network". The edge keeps the LAN URL.

Notes:

* **If Tailscale runs on the Proxmox host but the app runs in a VM** with another IP, `tailscale ip -4` inside the VM prints nothing. Either install Tailscale in the VM, or make the host a subnet router (`sudo tailscale up --advertise-routes=192.168.29.0/24`, then approve the route in the admin console) and give tailnet users the VM's `192.168.29.x` address.
* Who may connect is controlled in the Tailscale admin console (users, device approval, ACLs). For people who cannot install anything, use Cloudflare (§4c) for the dashboard.
* Optional HTTPS name inside the tailnet: `sudo tailscale serve --bg http://127.0.0.1:8080` gives `https://<machine>.<tailnet>.ts.net` for the dashboard (RTSP still uses port 8554). Do **not** use `tailscale funnel`: it would publish the dashboard to the whole internet, and login is off by default.

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

| | **Docker** (portable, recommended for a new server) | **`python3 app.py`** (simple) | **`scripts/install.sh`** (permanent) |
|---|---|---|---|
| Setup | `docker compose up -d --build` | Copy the folder, run one command | Installer as root |
| Needs on the server | Docker only | Python 3, FFmpeg | Debian 12 / Ubuntu 24.04 |
| Starts after a server reboot | Yes (`restart: unless-stopped`) | No, unless you add the cron line below | Yes |
| Streams survive the app restarting | No: a container restart stops streams, they come back in seconds | No: restart stops streams, then they come back | Yes: they keep playing |
| Move to another server | Copy the folder + the `rig02-data` volume | Copy the folder + `storage/` | Re-run the installer, copy `/data` |
| Remote access through Cloudflare | `--profile cloudflare` (built in) | Install cloudflared yourself | Install cloudflared yourself |
| Good for | Proxmox VM (already runs Docker), any other server later | Trying it on the server | The ticket's reboot requirement with streams kept across app restarts |

### Docker (any server)

Needs Docker Engine 24+ with Compose v2.24+ (`docker compose version`). Step-by-step for the Proxmox VM, including validation and the capacity ramp: [PROXMOX_RUNBOOK.md](PROXMOX_RUNBOOK.md).

```bash
git clone <this-repository> rig02 && cd rig02        # or copy the folder
cp .env.example .env
#   set RTSPM_PUBLIC_RTSP_HOST=<server DNS name or fixed IP>
docker compose up -d --build
docker compose ps                                     # STATUS shows (healthy) within ~40 s
```

Open `http://<server>:8080`. RTSP URLs are `rtsp://<RTSPM_PUBLIC_RTSP_HOST>:8554/<name>`.

* **One container is the whole rig:** dashboard, API, FFmpeg publishers and MediaMTX (the backend runs MediaMTX itself). MediaMTX's API, metrics and HLS ports stay inside the container; only 8080 and 8554 (TCP) are published. Other ports: set `RIG02_HTTP_PORT` / `RIG02_RTSP_PORT` in `.env`.
* **State:** everything (database, videos, thumbnails, slates, logs) is in the named volume `rig02-data` (`/data` in the container). Uploads from the dashboard land in `/data/videos`. To keep videos in a host folder instead (e.g. a big disk on the Proxmox VM), add to the `rig02` service:

  ```yaml
      volumes:
        - rig02-data:/data
        - /srv/rig02/videos:/data/videos      # sudo chown -R 10001:10001 /srv/rig02/videos
  ```

* **Runs as a non-root user** (uid 10001), with tini as PID 1 so FFmpeg processes are reaped.
* **Upgrade:** `git pull && docker compose up -d --build` (the volume is kept; the database migrates itself).
* **Move to another server:** copy the folder and the volume:

  ```bash
  docker run --rm -v rig02_rig02-data:/data -v "$PWD":/backup debian:12 tar czf /backup/rig02-data.tgz -C /data .
  # on the new server, after copying rig02-data.tgz next to docker-compose.yml:
  docker volume create rig02_rig02-data
  docker run --rm -v rig02_rig02-data:/data -v "$PWD":/backup debian:12 sh -c "tar xzf /backup/rig02-data.tgz -C /data && chown -R 10001:10001 /data"
  docker compose up -d
  ```

  (The volume name is `<folder>_rig02-data`; check it with `docker volume ls`.) Update `RTSPM_PUBLIC_RTSP_HOST` if the new server has another name.
* **Logs:** `docker compose logs -f rig02` (rotated, 5 × 20 MB).
* **Trade-off:** restarting the container (or Docker) restarts the streams; readers reconnect after a few seconds. The systemd install keeps publishers running across backend restarts.
* Tested by `scripts/test_linux.sh docker`: build, health, non-root, upload, RTSP read through the published port, slate lead-in, container restart with streams coming back, data kept in the volume.

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
* `RTSPM_RTSP_ALT_HOSTS`: other names/IPs for users on other subnets or the VPN (see section 2).
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

## 4c. Remote access via Cloudflare (from anywhere on the internet)

The dashboard (UI, API, live preview, uploads) is published through a **Cloudflare Tunnel**: `cloudflared` makes an outbound connection to Cloudflare, so no port is opened on the office router. A **Cloudflare Access** application in front of the hostname decides who may sign in (company SSO or a one-time PIN sent by e-mail).

**The rig refuses every request that comes through Cloudflare without a valid Access token** (403), also while `RTSPM_CF_ACCESS_*` are not set. A tunnel without an Access policy therefore shows "no Cloudflare Access token" instead of an open dashboard. Office LAN access is unchanged (`RTSPM_CF_ACCESS_MODE=all` requires a token on the LAN too). The signed-in e-mail is recorded as the user on every event ("stream.created by alice@…").

One-time setup in the Cloudflare dashboard (Zero Trust; the free plan covers up to 50 users):

1. **Tunnel:** Networks → Tunnels → Create a tunnel (type *Cloudflared*), name it `rig02`. Copy the **token** into `.env` as `TUNNEL_TOKEN=…`.
2. **Public hostname** on the tunnel: e.g. `rig02.<company-domain>` → service `http://rig02:8080` (Docker) or `http://localhost:8080` (systemd install / app.py with cloudflared on the host).
3. **Access application:** Access → Applications → Add → *Self-hosted*, the same hostname. Policy: *Allow*, include "Emails ending in @<company-domain>" (or a list of e-mails). Session duration e.g. 24 h. Copy the application's **AUD tag**.
4. In `.env`: `RTSPM_CF_ACCESS_TEAM=<team>` (from `<team>.cloudflareaccess.com`, Settings → Custom pages / team domain) and `RTSPM_CF_ACCESS_AUD=<AUD tag>`.
5. Start it: `docker compose --profile cloudflare up -d`. Open `https://rig02.<company-domain>`: Cloudflare asks for the e-mail, then the dashboard opens.

**RTSP from outside the office.** RTSP is not a web protocol, so `rtsp://rig02.<company-domain>/…` does not work through a tunnel hostname. Remote readers (VLC, ffmpeg, a model running on a laptop) use the **WARP client** instead:

1. On the tunnel: *Private networks* → add the server's LAN IP as `/32` (e.g. `192.168.29.105/32`).
2. Settings → WARP Client → device enrolment: allow the same e-mails; Split Tunnels: make sure the office subnet is *included*.
3. Remote users install Cloudflare WARP, log in to the team, and use the normal URL `rtsp://<server LAN IP or office DNS name>:8554/<name>`. Pick that address in the dashboard's **URLs use** picker (add it to `RTSPM_RTSP_ALT_HOSTS` if it is not the main address).

On-site edge devices keep reading over the LAN; Cloudflare is for people working remotely.

Without WARP, a single machine can map the port with `cloudflared access tcp --hostname rtsp-rig02.<domain> --url localhost:8554` (needs a TCP public hostname `rtsp-rig02.<domain>` → `tcp://rig02:8554` on the tunnel and an Access application for it), then read `rtsp://localhost:8554/<name>`.

Notes:
* Uploads go through Cloudflare in 8 MB chunks, under its 100 MB request limit; a dropped connection resumes.
* The live preview (HLS) works through the tunnel; expect a few seconds more delay than on the LAN.
* Check Cloudflare's current plan terms for non-HTTP (TCP/WARP) traffic with whoever owns the Cloudflare account before relying on it for many users.
* **Not verified here:** a real tunnel, Access login and WARP RTSP read need your Cloudflare account; they stay NOT RUN until done.

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
