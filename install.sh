#!/usr/bin/env bash
# Install/update leadpipe under /opt/leadpipe. Idempotent — re-run after
# every code change; live state (leads.db, .env, backups, venv) is never
# clobbered.
set -euo pipefail

APP=/opt/leadpipe
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT_DIR=/etc/systemd/system

[[ $EUID -eq 0 ]] || { echo "run as root (sudo ./install.sh)" >&2; exit 1; }

id -u leadpipe >/dev/null 2>&1 ||
    useradd --system --home-dir "$APP" --shell /usr/sbin/nologin leadpipe

mkdir -p "$APP" "$APP/logs" "$APP/backup"

rsync -a \
    --exclude 'venv/' --exclude 'leads.db*' --exclude '.env' \
    --exclude 'backup/' --exclude 'logs/' --exclude '.git/' \
    --exclude '__pycache__/' --exclude '.pytest_cache/' \
    "$SRC/" "$APP/"

if [[ ! -f "$APP/.env" ]]; then
    cp "$APP/.env.example" "$APP/.env"
    chmod 600 "$APP/.env"
    echo "NOTE: created $APP/.env from the example — fill in keys before anything can send" >&2
fi

[[ -d "$APP/venv" ]] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install --quiet --upgrade pip
"$APP/venv/bin/pip" install --quiet -r "$APP/requirements.txt"

chmod +x "$APP/backup.sh"
chown -R leadpipe:leadpipe "$APP"

cp "$APP"/systemd/*.service "$APP"/systemd/*.timer "$UNIT_DIR/"
systemctl daemon-reload
systemctl enable --now \
    leadpipe-discover.timer leadpipe-triage.timer \
    leadpipe-report.timer leadpipe-backup.timer \
    leadpipe-webhook.service

echo "installed. verify with: systemctl list-timers 'leadpipe-*'"
