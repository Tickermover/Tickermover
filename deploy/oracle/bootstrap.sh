#!/usr/bin/env bash
# ── TickerMover — one-time Oracle Cloud (Ubuntu 22.04/24.04 ARM) bootstrap ──
# Run ONCE on a fresh Ampere A1 instance, as the default `ubuntu` user:
#   curl -fsSL https://raw.githubusercontent.com/Tickermover/Tickermover/main/deploy/oracle/bootstrap.sh | bash
set -euo pipefail

APP_USER=tickermover
APP_DIR=/opt/tickermover
REPO=https://github.com/Tickermover/Tickermover.git

echo "── system packages ─────────────────────────────────────────"
sudo apt-get update -y
# build deps: lxml needs libxml2/libxslt, Pillow needs jpeg/zlib/freetype.
# ARM64 wheels exist for pandas/numpy/matplotlib, so those need no compiler.
sudo apt-get install -y --no-install-recommends \
  python3 python3-venv python3-dev python3-pip git curl ca-certificates \
  build-essential libxml2-dev libxslt1-dev zlib1g-dev libjpeg-dev libfreetype6-dev

echo "── service user + checkout ─────────────────────────────────"
sudo useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER" 2>/dev/null || true
sudo mkdir -p "$APP_DIR"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"
if [ ! -d "$APP_DIR/.git" ]; then
  sudo -u "$APP_USER" git clone --branch main "$REPO" "$APP_DIR"
fi

echo "── virtualenv ──────────────────────────────────────────────"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip wheel
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "── env file (secrets live here, NEVER in git) ──────────────"
if [ ! -f /etc/tickermover.env ]; then
  sudo install -m 600 -o root -g root /dev/null /etc/tickermover.env
  echo "TICKERMOVER_ENV=prod" | sudo tee /etc/tickermover.env >/dev/null
  echo "  created /etc/tickermover.env — add your keys, then: sudo systemctl restart tickermover"
fi

echo "── systemd + caddy ─────────────────────────────────────────"
sudo cp "$APP_DIR/deploy/oracle/tickermover.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tickermover

# Caddy gives automatic Let's Encrypt TLS with no cron and no certbot.
if ! command -v caddy >/dev/null; then
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y caddy
fi
sudo cp "$APP_DIR/deploy/oracle/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl restart caddy

# Oracle images ship a DROP-all iptables policy that survives security lists.
sudo iptables -I INPUT -p tcp --dport 80  -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save 2>/dev/null || sudo apt-get install -y iptables-persistent

echo
echo "Done. Next:"
echo "  1. sudo nano /etc/tickermover.env      # add keys (see docs/prod/FREE_APIS.md)"
echo "  2. sudo systemctl restart tickermover"
echo "  3. In the OCI console: VCN -> Security List -> ingress 80 + 443 from 0.0.0.0/0"
echo "  4. Point tickermover.com A record at this instance's public IP"
echo "  5. journalctl -u tickermover -f        # watch it boot"
