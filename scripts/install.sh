#!/usr/bin/env bash
# RIG-02 RTSP Stream Manager: installer for a fresh Debian 12 / Ubuntu 22.04+
# VM (Proxmox). Idempotent: safe to re-run for upgrades.
#
#   sudo scripts/install.sh --host rig02.vidatt.local [options]
#
# Options:
#   --host NAME            fixed DNS name or reserved IP shown in RTSP URLs (required on first install)
#   --with-nginx           also install nginx as a reverse proxy on :80
#   --tls self-signed|CRT:KEY  with --with-nginx: serve the dashboard over HTTPS (:443) using a
#                          generated self-signed certificate for --host, or your certificate files
#   --configure-firewall   open 8554/tcp and the dashboard port with ufw (only if ufw is active)
#   --mediamtx-version V   MediaMTX release to install (default v1.21.2)
#   --upgrade-mediamtx     replace an existing /usr/local/bin/mediamtx
#   --no-start             install but do not (re)start services
#
# Safety rules: existing configuration files (MediaMTX config, unit files, env
# file, users) are NEVER overwritten. If ours differs, it is written next to
# the existing file as *.rig02-new and a warning is printed.
set -euo pipefail

HOST=""
WITH_NGINX=0
TLS=""
FIREWALL=0
MTX_VERSION="v1.21.2"
UPGRADE_MTX=0
START=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --with-nginx) WITH_NGINX=1; shift ;;
    --tls) TLS="$2"; shift 2 ;;
    --configure-firewall) FIREWALL=1; shift ;;
    --mediamtx-version) MTX_VERSION="$2"; shift 2 ;;
    --upgrade-mediamtx) UPGRADE_MTX=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done

SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/rtsp-manager
DATA=/var/lib/rtsp-manager
VIDEOS=/data/rtsp-manager/videos
ETC=/etc/rtsp-manager
ENVFILE=$ETC/rtsp-manager.env
WARNINGS=()

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARN:\033[0m %s\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (sudo)"
[[ -z "$TLS" || $WITH_NGINX -eq 1 ]] || die "--tls requires --with-nginx"
command -v apt-get >/dev/null || die "this installer supports Debian/Ubuntu (apt) only"
command -v systemctl >/dev/null || die "systemd is required"

# Install a config file without ever overwriting a different existing one.
install_conf() {  # install_conf <src> <dst> <mode> <owner:group>
  local src="$1" dst="$2" mode="$3" own="$4"
  if [[ -e "$dst" ]]; then
    if cmp -s "$src" "$dst"; then return 0; fi
    install -m "$mode" -o "${own%%:*}" -g "${own##*:}" "$src" "$dst.rig02-new"
    warn "$dst exists and differs; NOT overwritten. Review: diff -u $dst $dst.rig02-new"
    return 0
  fi
  install -D -m "$mode" -o "${own%%:*}" -g "${own##*:}" "$src" "$dst"
  log "installed $dst"
}

# ---------------------------------------------------------------- packages
log "installing packages (ffmpeg, python3-venv, curl)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
PKGS=(ffmpeg python3 python3-venv python3-pip curl ca-certificates tar rsync)
[[ $WITH_NGINX -eq 1 ]] && PKGS+=(nginx)
[[ -n "$TLS" ]] && PKGS+=(openssl)
if [[ ! -f "$SRC/frontend/dist/index.html" ]]; then PKGS+=(nodejs npm); fi
apt-get install -y -qq "${PKGS[@]}" >/dev/null
FFV=$(ffmpeg -hide_banner -version | head -1)
log "$FFV"
ENCODERS=$(ffmpeg -hide_banner -encoders 2>/dev/null || true)  # capture first: grep -q + pipefail = SIGPIPE false negative
grep -q libx264 <<<"$ENCODERS" || warn "ffmpeg has no libx264: transcoding (EC-01) will be refused"
FFMAJOR=$(ffmpeg -hide_banner -version | sed -nE 's/^ffmpeg version n?([0-9]+)\..*/\1/p')
if [[ -n "$FFMAJOR" && "$FFMAJOR" -lt 5 ]]; then warn "FFmpeg $FFMAJOR < 5.1: -fps_mode is unsupported; transcoding will fail. Use Debian 12 / Ubuntu 24.04."; fi

# ---------------------------------------------------------------- accounts
id mediamtx >/dev/null 2>&1 || { useradd --system --no-create-home --shell /usr/sbin/nologin mediamtx; log "created user mediamtx"; }
id rtspmgr  >/dev/null 2>&1 || { useradd --system --home-dir "$DATA" --no-create-home --shell /usr/sbin/nologin rtspmgr; log "created user rtspmgr"; }

# ---------------------------------------------------------------- directories
install -d -m 0755 -o root -g root "$APP"
install -d -m 0750 -o rtspmgr -g rtspmgr "$DATA"
# setgid + group-writable: team members in group "rtspmgr" can copy videos in
install -d -m 2775 -o rtspmgr -g rtspmgr "$VIDEOS"
install -d -m 0750 -o root -g rtspmgr "$ETC"
install -d -m 0755 -o root -g root /etc/mediamtx

# ---------------------------------------------------------------- application
log "copying application to $APP"
rsync -a --delete \
  --exclude '.git' --exclude 'backend/.venv' --exclude 'frontend/node_modules' --exclude 'tests/.fixtures' \
  --exclude 'storage' --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.env' \
  "$SRC/" "$APP/"
chown -R root:root "$APP"

if [[ ! -f "$APP/frontend/dist/index.html" ]]; then
  log "building frontend (node $(node --version))"
  (cd "$APP/frontend" && npm ci --no-audit --no-fund && npm run build)
fi

log "creating Python virtualenv"
python3 -m venv "$APP/backend/.venv"
"$APP/backend/.venv/bin/pip" install -q --upgrade pip
"$APP/backend/.venv/bin/pip" install -q -r "$APP/backend/requirements.txt"

# ---------------------------------------------------------------- MediaMTX
case "$(uname -m)" in
  x86_64) MTX_ARCH=amd64 ;;
  aarch64) MTX_ARCH=arm64 ;;
  armv7l) MTX_ARCH=armv7 ;;
  *) die "unsupported architecture $(uname -m)" ;;
esac
if [[ -x /usr/local/bin/mediamtx && $UPGRADE_MTX -eq 0 ]]; then
  log "keeping existing MediaMTX $(/usr/local/bin/mediamtx --version 2>/dev/null || echo '?') (use --upgrade-mediamtx to replace)"
else
  TARBALL="mediamtx_${MTX_VERSION}_linux_${MTX_ARCH}.tar.gz"
  BASE="https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}"
  TMP=$(mktemp -d)
  log "downloading MediaMTX $MTX_VERSION ($MTX_ARCH)"
  curl -fsSL -o "$TMP/$TARBALL" "$BASE/$TARBALL"
  curl -fsSL -o "$TMP/checksums.sha256" "$BASE/checksums.sha256" || die "could not download checksums for $MTX_VERSION"
  (cd "$TMP" && grep -E " \*?$TARBALL\$" checksums.sha256 | sha256sum -c -) || die "MediaMTX checksum verification FAILED"
  tar -xzf "$TMP/$TARBALL" -C "$TMP" mediamtx
  [[ -x /usr/local/bin/mediamtx ]] && cp -p /usr/local/bin/mediamtx /usr/local/bin/mediamtx.previous
  install -m 0755 -o root -g root "$TMP/mediamtx" /usr/local/bin/mediamtx
  rm -rf "$TMP"
  log "installed /usr/local/bin/mediamtx ($MTX_VERSION, checksum verified)"
  MTX_CHANGED=1
fi
install_conf "$APP/deployment/mediamtx.yml" /etc/mediamtx/mediamtx.yml 0644 root:root
# New MediaMTX releases may add servers that are ON by default (v1.21: MoQ).
# Refuse a binary/config combination that exposes anything but RTSP.
if ! "$APP/backend/.venv/bin/python" "$APP/scripts/check_mediamtx_exposure.py" /usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml; then
  if [[ "${MTX_CHANGED:-0}" == 1 && -x /usr/local/bin/mediamtx.previous ]]; then
    mv /usr/local/bin/mediamtx.previous /usr/local/bin/mediamtx
    die "the new MediaMTX exposes extra ports with this config; the previous binary was restored. Disable the new servers in /etc/mediamtx/mediamtx.yml, then retry."
  fi
  die "MediaMTX would expose ports other than RTSP with /etc/mediamtx/mediamtx.yml; fix the config first."
fi

# ---------------------------------------------------------------- configuration
if [[ ! -f "$ENVFILE" ]]; then
  [[ -n "$HOST" ]] || die "--host is required on first install (fixed DNS name or reserved IP of this VM)"
  sed -e "s|^RTSPM_PUBLIC_RTSP_HOST=.*|RTSPM_PUBLIC_RTSP_HOST=$HOST|" \
      -e "s|^RTSPM_FRONTEND_DIST=.*|RTSPM_FRONTEND_DIST=$APP/frontend/dist|" \
      "$APP/.env.example" > "$ENVFILE.tmp"
  if [[ $WITH_NGINX -eq 1 ]]; then echo "RTSPM_LISTEN_HOST=127.0.0.1" >> "$ENVFILE.tmp"; fi
  if [[ -n "$TLS" ]]; then echo "RTSPM_COOKIE_SECURE=true" >> "$ENVFILE.tmp"; fi
  install -m 0640 -o root -g rtspmgr "$ENVFILE.tmp" "$ENVFILE"
  rm -f "$ENVFILE.tmp"
  log "wrote $ENVFILE (RTSP host: $HOST)"
else
  log "keeping existing $ENVFILE"
  [[ -n "$HOST" ]] && ! grep -q "^RTSPM_PUBLIC_RTSP_HOST=$HOST\$" "$ENVFILE" && \
    warn "$ENVFILE has a different RTSPM_PUBLIC_RTSP_HOST; edit it manually if --host should change"
fi

install_conf "$APP/deployment/mediamtx.service" /etc/systemd/system/mediamtx.service 0644 root:root
install_conf "$APP/deployment/rtsp-manager.service" /etc/systemd/system/rtsp-manager.service 0644 root:root
install_conf "$APP/deployment/rtsp-manager-backup.service" /etc/systemd/system/rtsp-manager-backup.service 0644 root:root
install_conf "$APP/deployment/rtsp-manager-backup.timer" /etc/systemd/system/rtsp-manager-backup.timer 0644 root:root
install -d -m 0700 -o root -g root /var/backups/rtsp-manager
install_conf "$APP/deployment/polkit-rtsp-manager.rules" /etc/polkit-1/rules.d/50-rtsp-manager.rules 0644 root:root
command -v pkaction >/dev/null || apt-get install -y -qq polkitd >/dev/null 2>&1 || apt-get install -y -qq policykit-1 >/dev/null 2>&1 || \
  warn "polkit not installed: the dashboard will not be able to restart MediaMTX (systemd still restarts it)"

if [[ -n "$TLS" ]]; then
  install -d -m 0750 -o root -g root /etc/rtsp-manager/tls
  if [[ "$TLS" == "self-signed" ]]; then
    if [[ ! -f /etc/rtsp-manager/tls/server.crt ]]; then
      CN="${HOST:-$(hostname -f)}"
      openssl req -x509 -newkey rsa:2048 -nodes -days 1825 -subj "/CN=$CN" \
        -addext "subjectAltName=DNS:$CN" \
        -keyout /etc/rtsp-manager/tls/server.key -out /etc/rtsp-manager/tls/server.crt 2>/dev/null
      log "generated a self-signed certificate for $CN (browsers will ask to trust it once)"
    fi
  else
    install -m 0644 "${TLS%%:*}" /etc/rtsp-manager/tls/server.crt
    install -m 0600 "${TLS##*:}" /etc/rtsp-manager/tls/server.key
  fi
  chmod 0600 /etc/rtsp-manager/tls/server.key
fi

if [[ $WITH_NGINX -eq 1 ]]; then
  NGX_SRC="$APP/deployment/nginx.conf"; [[ -n "$TLS" ]] && NGX_SRC="$APP/deployment/nginx-tls.conf"
  install_conf "$NGX_SRC" /etc/nginx/sites-available/rtsp-manager 0644 root:root
  if [[ ! -e /etc/nginx/sites-enabled/rtsp-manager ]]; then
    ln -s /etc/nginx/sites-available/rtsp-manager /etc/nginx/sites-enabled/rtsp-manager
  fi
  if [[ -L /etc/nginx/sites-enabled/default ]]; then
    # Only disable the stock welcome page, never a site someone has edited.
    if dpkg --verify nginx-common 2>/dev/null | grep -q "sites-available/default"; then
      warn "nginx 'default' site was modified and still owns port 80; review it (our site is in sites-enabled/rtsp-manager)"
    else
      rm /etc/nginx/sites-enabled/default
      log "disabled the stock nginx welcome site (file kept in sites-available)"
    fi
  fi
  nginx -t || die "nginx configuration test failed (see above)"
  systemctl enable --now nginx >/dev/null
  systemctl reload nginx
fi

# First operator account (password shown once, never logged)
USERS="$DATA/users"
if [[ ! -s "$USERS" ]]; then
  PW=$(head -c 18 /dev/urandom | base64 | tr -d '/+=' | head -c 20)
  runuser -u rtspmgr -- env RTSPM_ENV_FILE=/dev/null RTSPM_DATA_DIR="$DATA" \
    "$APP/backend/.venv/bin/python" "$APP/scripts/manage_users.py" add admin --role admin --password-stdin <<<"$PW"
  FIRST_PW="$PW"
fi

# ---------------------------------------------------------------- firewall
PORT=$(sed -nE 's/^RTSPM_LISTEN_PORT=([0-9]+)/\1/p' "$ENVFILE" | tail -1); PORT=${PORT:-8080}
WEBPORT=$([[ $WITH_NGINX -eq 1 ]] && { [[ -n "$TLS" ]] && echo 443 || echo 80; } || echo "$PORT")
SCHEME=$([[ -n "$TLS" ]] && echo https || echo http)
if [[ $FIREWALL -eq 1 ]]; then
  if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 8554/tcp comment 'RIG-02 RTSP' >/dev/null
    ufw allow "$WEBPORT/tcp" comment 'RIG-02 dashboard' >/dev/null
    [[ -n "$TLS" ]] && ufw allow 80/tcp comment 'RIG-02 HTTP->HTTPS redirect' >/dev/null
    log "ufw: allowed 8554/tcp and $WEBPORT/tcp"
  else
    warn "ufw is not active; firewall left unchanged (see docs/DEPLOYMENT.md#firewall)"
  fi
fi

# ---------------------------------------------------------------- start
systemctl daemon-reload
if [[ $START -eq 1 ]]; then
  systemctl enable --now mediamtx.service rtsp-manager.service rtsp-manager-backup.timer >/dev/null
  systemctl restart mediamtx.service rtsp-manager.service
  log "waiting for the dashboard"
  for _ in $(seq 30); do curl -fs "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 1; done
  "$APP/scripts/healthcheck.sh" || warn "health check reported problems (see above)"
fi

echo
log "RIG-02 installed."
echo "    Dashboard : $SCHEME://${HOST:-<this-host>}:$WEBPORT/"
echo "    RTSP URLs : rtsp://${HOST:-<this-host>}:8554/<stream-name>"
echo "    Videos    : copy files into $VIDEOS (members of group rtspmgr can write)"
if [[ -n "${FIRST_PW:-}" ]]; then
  echo "    Login     : admin / $FIRST_PW   <- shown ONCE; change it with:"
  echo "                the dashboard (user menu -> Change password), or"
  echo "                sudo -u rtspmgr $APP/backend/.venv/bin/python $APP/scripts/manage_users.py add admin --role admin"
fi
for w in "${WARNINGS[@]}"; do printf '\033[1;33mWARN:\033[0m %s\n' "$w"; done
