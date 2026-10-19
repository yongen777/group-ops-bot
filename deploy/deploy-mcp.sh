#!/usr/bin/env bash
# group-ops MCP Server deployment (run ON the server as root/sudo).
# Idempotent: safe to re-run. Code must already be uploaded (deploy.sh does it).
#
# ⛔ 本脚本不执行任何 DDL。建表只在用户明确批准后单独进行（ddl/mysql_qsm_v1.sql）。
# ⛔ 本脚本不改机器人侧任何代码/env（GOPS_MCP_BASE_URL 已在 group-ops-bot.env）。
set -euo pipefail

APP_ROOT=/opt/group-ops
REPO_DIR=$APP_ROOT/repo
VENV_DIR=$APP_ROOT/venv
ENV_DIR=/etc/group-ops
ENV_FILE=$ENV_DIR/mcp-server.env
SERVICE=group-ops-mcp
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "[1/6] preflight"
[ -d "$REPO_DIR/mcp_server" ] || {
    echo "ERROR: $REPO_DIR/mcp_server missing. Run deploy.sh first (it uploads code)." >&2
    exit 1
}
[ -f "$ENV_FILE" ] || {
    echo "ERROR: $ENV_FILE missing. Copy mcp-server.env.template, fill the" >&2
    echo "       MySQL password (from the DB admin), chmod 600." >&2
    exit 1
}
if grep -q "REPLACE_ME" "$ENV_FILE"; then
    echo "ERROR: $ENV_FILE still has REPLACE_ME placeholders (password)." >&2
    exit 1
fi
chown root:root "$ENV_FILE"; chmod 600 "$ENV_FILE"

echo "[2/6] database readonly check (recommended before first start)"
if [ -n "${GOPS_MYSQL_PASSWORD:-}" ]; then
    bash "$HERE/mysql-check.sh" || {
        echo "ERROR: database check did not pass (exit $?)." >&2
        echo "       空库(0)才继续；已有表(2)先人工核对。可用 --skip-db-check 跳过。" >&2
        [ "${SKIP_DB_CHECK:-0}" = "1" ] || exit 1
    }
else
    echo "SKIP: GOPS_MYSQL_PASSWORD not in env; run 'bash $HERE/mysql-check.sh'"
    echo "      manually before relying on this deployment (read-only, no DDL)."
fi

echo "[3/6] python deps (mcp SDK + pymysql)"
[ -x "$VENV_DIR/bin/python" ] || python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
# ⚠️ mcp 必须锁 1.x：2.x 已删 mcp.server.fastmcp（FastMCP→MCPServer），
#    requirements-mcp.txt 的裸 `mcp>=1.0` 会装到 2.3.0 导致 import 失败
#    （2026-10-14 实测）。pytest 是 CI 依赖，部署 venv 最小化不装。
"$VENV_DIR/bin/pip" install 'mcp>=1.0,<2' 'pymysql>=1.1'
"$VENV_DIR/bin/python" -c "import mcp, pymysql; from mcp.server.fastmcp import FastMCP; print('mcp+pymysql OK')"

echo "[4/6] systemd unit"
cp "$HERE/mcp-server.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"

echo "[5/6] start + fail-closed proof"
systemctl restart "$SERVICE"
sleep 3
systemctl --no-pager --lines=20 status "$SERVICE" || true
# 缺 MySQL 配置时必须起不来：这是 fail-closed 的自证（临时改名 env 太重，改为看日志）
journalctl -u "$SERVICE" --no-pager --lines=5 || true

echo "[6/6] SSE probe (must answer on 18001 loopback)"
code=$(curl -s -o /tmp/mcp_sse.txt -w '%{http_code}' --max-time 5 -N \
    "http://127.0.0.1:18001/sse" || echo "000")
echo "GET /sse -> $code"
head -c 300 /tmp/mcp_sse.txt 2>/dev/null; echo
if [ "$code" != "200" ]; then
    echo "WARN: 18001 did not answer. Check whether the SDK ignored FASTMCP_PORT"
    echo "      (older mcp SDK defaults to 8000):"
    echo "        ss -tlnp | grep -E ':18001|:8000'"
    echo "      If it listens on 8000: either upgrade mcp (pip install -U mcp)"
    echo "      or point the bot env GOPS_MCP_BASE_URL at the real port."
    echo "      ⛔ 仍只听 127.0.0.1，⛔ 不映射公网。"
fi

echo
echo "done. verify with: bash $HERE/mcp-check.sh"
echo "⛔ reminder: DDL (ddl/mysql_qsm_v1.sql) is NOT executed by this script."
echo "   Tables must exist before real submissions stop falling to class C."
