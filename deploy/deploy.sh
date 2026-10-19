#!/usr/bin/env bash
# group-ops bot test-server deployment (run ON the server as root/sudo).
# Idempotent: safe to re-run. Uploads are expected at /tmp/group-ops-upload/.
set -euo pipefail

APP_ROOT=/opt/group-ops
REPO_DIR=$APP_ROOT/repo
VENV_DIR=$APP_ROOT/venv
DATA_DIR=$APP_ROOT/data
ENV_DIR=/etc/group-ops
ENV_FILE=$ENV_DIR/group-ops-bot.env
UPLOAD=/tmp/group-ops-upload
SERVICE=group-ops-bot

echo "[1/6] service user + directories"
if ! id -u groupops >/dev/null 2>&1; then
    useradd --system --home "$APP_ROOT" --shell /usr/sbin/nologin groupops
fi
mkdir -p "$REPO_DIR" "$DATA_DIR" "$VENV_DIR" "$ENV_DIR"
chown -R groupops:groupops "$APP_ROOT"
chmod 750 "$DATA_DIR"

echo "[2/6] install code from upload"
if [ ! -d "$UPLOAD/app" ]; then
    echo "ERROR: $UPLOAD/app not found. Upload code first (see README.md)." >&2
    exit 1
fi
rm -rf "$REPO_DIR/app" "$REPO_DIR/mcp_server"
cp -r "$UPLOAD/app" "$UPLOAD/mcp_server" "$REPO_DIR/"
cp "$UPLOAD/requirements.txt" "$UPLOAD/requirements-mcp.txt" "$REPO_DIR/"
chown -R groupops:groupops "$REPO_DIR"

echo "[3/6] python venv + dependencies"
if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install -r "$REPO_DIR/requirements.txt"
"$VENV_DIR/bin/python" --version

echo "[4/6] env file"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: $ENV_FILE missing. Copy the template, set admin credentials, chmod 600." >&2
    exit 1
fi
if grep -q "REPLACE_ME" "$ENV_FILE"; then
    echo "ERROR: $ENV_FILE still has REPLACE_ME placeholders." >&2
    exit 1
fi
chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"

echo "[5/6] systemd unit"
cp "$(dirname "$0")/group-ops-bot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"

echo "[6/6] start + healthz"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=20 status "$SERVICE" || true
curl -fsS "http://127.0.0.1:12334/healthz" && echo " <- healthz OK"
echo "done. verify with: bash $(dirname "$0")/verify.sh"
