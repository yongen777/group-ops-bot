#!/usr/bin/env bash
# MCP Server 链路检查（只读探针：不起服务、不写数据、⛔ 不执行 DDL）。
# 用法：bash mcp-check.sh   （在测试服务器上执行）
set -uo pipefail

fail=0
say() { echo "$1"; }

say "== services =="
systemctl is-enabled group-ops-mcp 2>/dev/null || say "(mcp unit not enabled)"
systemctl is-active group-ops-mcp 2>/dev/null || say "(mcp unit not active)"
systemctl is-active group-ops-bot 2>/dev/null || say "(bot unit not active)"

say "== listening sockets (expect 18001 loopback + 12334) =="
ss -tlnp | grep -E ":18001|:12334|:8000" || say "(no matching sockets)"

say "== MCP SSE handshake (GET /sse must stream an endpoint event) =="
body=$(curl -s -N --max-time 5 "http://127.0.0.1:18001/sse" | head -c 400 || true)
if [ -n "$body" ]; then
    say "PASS  18001 SSE answered:"
    echo "$body" | head -5
else
    say "FAIL  18001 SSE silent. If ss showed :8000 instead, the mcp SDK"
    say "      ignored FASTMCP_PORT -> upgrade mcp or repoint GOPS_MCP_BASE_URL."
    fail=1
fi

say "== loopback-only proof (18001 must NOT answer on public IP) =="
pub=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://219.151.177.33:18001/sse")
if [ "$pub" = "000" ]; then
    say "PASS  18001 not reachable from public IP (expected)"
else
    say "FAIL  18001 publicly reachable -> $pub (must stay loopback-only)"
    fail=1
fi

say "== bot -> MCP wiring =="
journalctl -u group-ops-bot --no-pager --lines=200 2>/dev/null \
    | grep -E "starting on" | tail -1 || say "(bot startup line not found)"
health=$(curl -s --max-time 5 "http://127.0.0.1:12334/healthz" || echo "")
say "bot healthz: $health"

say "== admin backend still localhost-only =="
pub_admin=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    "http://219.151.177.33:18003/healthz")
if [ "$pub_admin" = "000" ]; then
    say "PASS  admin not reachable from public IP (expected)"
else
    say "FAIL  admin reachable publicly -> $pub_admin"
    fail=1
fi

say "== DDL status reminder =="
say "⛔ 本检查不执行 DDL。若提交仍归 C 类（unknown）：先跑 mysql-check.sh"
say "   确认六表存在，再按批准流程执行 ddl/mysql_qsm_v1.sql。"

[ "$fail" = "0" ] && say "RESULT: PASS" || say "RESULT: FAIL (see above)"
exit $fail
