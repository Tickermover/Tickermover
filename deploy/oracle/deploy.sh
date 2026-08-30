#!/usr/bin/env bash
# ── TickerMover — deploy latest main. Mirrors the old push-to-prod flow. ──
#   sudo /opt/tickermover/deploy/oracle/deploy.sh
set -euo pipefail

APP_DIR=/opt/tickermover
APP_USER=tickermover

cd "$APP_DIR"
BEFORE=$(sudo -u "$APP_USER" git rev-parse --short HEAD)

echo "── fetching ────────────────────────────────────────────────"
sudo -u "$APP_USER" git fetch origin main --quiet
sudo -u "$APP_USER" git reset --hard origin/main --quiet
AFTER=$(sudo -u "$APP_USER" git rev-parse --short HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
  echo "  already at $AFTER — nothing to deploy"; exit 0
fi
echo "  $BEFORE -> $AFTER"

# Only reinstall when the pinned set actually moved; pandas/lxml take minutes on ARM.
if ! sudo -u "$APP_USER" git diff --quiet "$BEFORE" "$AFTER" -- requirements.txt; then
  echo "── requirements changed, reinstalling ──────────────────────"
  sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r requirements.txt
fi

echo "── restarting ──────────────────────────────────────────────"
sudo systemctl restart tickermover

# The old Railway lesson: the restart window looks broken. Poll until STABLE,
# not until the first success.
echo -n "  waiting for /api/status "
OK=0
for i in $(seq 1 60); do
  if curl -fsS -m 5 -o /dev/null http://127.0.0.1:8000/api/status 2>/dev/null; then
    OK=$((OK+1)); echo -n "+"
    [ "$OK" -ge 3 ] && { echo " stable"; exit 0; }
  else
    OK=0; echo -n "."
  fi
  sleep 3
done

echo
echo "FAILED to stabilise. Rolling back to $BEFORE"
sudo -u "$APP_USER" git reset --hard "$BEFORE" --quiet
sudo systemctl restart tickermover
exit 1
