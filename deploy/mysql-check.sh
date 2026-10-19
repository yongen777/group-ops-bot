#!/usr/bin/env bash
# MySQL 初始化检查包装器（只读，⛔ 不执行任何 DDL）。
# 密码走环境变量（⛔ 不进命令行）：export GOPS_MYSQL_PASSWORD=... 后运行。
#
# 用法：
#   export GOPS_MYSQL_PASSWORD='<mysql password>'
#   bash mysql-check.sh [--host H] [--port P] [--user U] [--database D] \
#       [--ddl /path/to/mysql_qsm_v1.sql]
#
# 退出码：0=空库可申请批准  2=已有表需人工核对  3=连接失败  4=库不存在  5=用法错误
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
VENV_PY=/opt/group-ops/venv/bin/python

if [ -x "$VENV_PY" ]; then
    PY="$VENV_PY"
elif command -v python3 >/dev/null 2>&1; then
    PY=python3
    echo "WARN: $VENV_PY not found, falling back to python3 (needs pymysql)" >&2
else
    echo "FATAL: no python found" >&2
    exit 5
fi

if [ -z "${GOPS_MYSQL_PASSWORD:-}" ]; then
    echo "FATAL: export GOPS_MYSQL_PASSWORD first (credentials never via argv)" >&2
    exit 5
fi

DEFAULT_DDL="$(cd "$HERE/.." && pwd)/ddl/mysql_qsm_v1.sql"
ARGS=()
if [ -f "$DEFAULT_DDL" ]; then
    ARGS+=(--ddl "$DEFAULT_DDL")
fi

"$PY" "$HERE/mysql_check.py" "${ARGS[@]}" "$@"
code=$?

echo
case $code in
    0) echo ">> 空库，可提交 DDL 批准申请（⛔ 批准前仍不得执行）" ;;
    2) echo ">> 已有表：停止，人工核对归属与结构后再决定" ;;
    3) echo ">> 连接/鉴权失败：查凭据、防火墙、端口 28619 可达性" ;;
    4) echo ">> 库不存在：找管理员确认建库（建库也是 DDL，需批准）" ;;
    *) echo ">> 用法/环境错误（退出码 $code）" ;;
esac
exit $code
