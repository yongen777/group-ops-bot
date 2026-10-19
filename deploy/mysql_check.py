# -*- coding: utf-8 -*-
"""MySQL 初始化检查（只读 · 阶段 A 部署准备）。

⛔ 本脚本**只执行 SHOW / SELECT**：不断连、不建库、不建表、不写任何数据。
   执行 DDL 前必须先跑本脚本并让用户审阅结论（用户硬规则：DDL 未经明确批准不执行）。

检查项（对应用户要求的「数据库初始化检查清单」）：
  1. 当前数据库版本（server version + 协议）
  2. 是否为空库（目标库存在性 + 表数量）
  3. 是否已有表（表清单 + 与 ddl/mysql_qsm_v1.sql 六表逐一对照）
  4. DDL 执行风险（共享库 / 字符集 / 时区 / 权限 / 表结构漂移）

用法（密码走环境变量，⛔ 不进命令行参数）：
    GOPS_MYSQL_PASSWORD='...' python3 mysql_check.py \
        --host 219.151.177.33 --port 28619 --user qsm --database qsm \
        [--ddl /opt/group-ops/repo/../../ddl/mysql_qsm_v1.sql]

退出码：
    0 = 目标库存在且 0 张表（空库，可提交批准申请）
    2 = 已有表（必须人工核对是否本脚本产物，⛔ 不得直接再跑 DDL）
    3 = 连接 / 鉴权 / 选库失败（凭据或网络问题）
    4 = 目标库不存在（需先与管理员确认建库——建库本身也是 DDL，⛔ 本脚本不做）
    5 = 用法 / 参数错误
"""

from __future__ import annotations

import argparse
import os
import re
import sys

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("FATAL: pymysql not installed. Run with the MCP venv: "
          "/opt/group-ops/venv/bin/python", file=sys.stderr)
    sys.exit(5)

# ddl/mysql_qsm_v1.sql 的六张表（§4.11.2）；DDL 未批准未执行，此处仅作对照
EXPECTED_TABLES = (
    "sys_user",
    "checkin_record",
    "summary_record",
    "report_record",
    "report_item",
    "operation_log",
)

# 实测环境基线（2026-10-12 只读验证，§5.1.1）：不一致要在报告里显式警告
BASELINE = {
    "server_version_prefix": "5.7",
    "db_charset": "utf8mb4",
    "db_collation": "utf8mb4_general_ci",
    "system_time_zone": "CST",
}

# 连接后执行 DDL 至少需要的权限（SHOW GRANTS 文本匹配，粗粒度但够用）
NEEDED_GRANTS = ("SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER",
                 "DROP", "INDEX")


def _readonly_guard(sql: str) -> str:
    """⛔ 硬闸：任何非 SHOW/SELECT 语句直接拒绝执行。"""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SHOW", "SELECT"):
        raise RuntimeError("readonly guard: refusing to execute %r" % sql[:60])
    return sql


def _query(conn, sql: str):
    with conn.cursor() as cur:
        _readonly_guard(sql)
        cur.execute(sql)
        return cur.fetchall()


def _parse_ddl_columns(path: str) -> dict:
    """从 DDL 文件解析每张表的列名集合（供「是否本脚本产物」核对）。"""
    text = open(path, "r", encoding="utf-8").read()
    tables: dict[str, set] = {}
    for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS `(\w+)` \((.*?)\n\) ENGINE",
            text, re.S):
        name, body = m.group(1), m.group(2)
        cols = set(re.findall(r"^\s*`(\w+)`", body, re.M))
        tables[name] = cols
    return tables


def main() -> int:
    ap = argparse.ArgumentParser(description="group-ops MySQL readonly check")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", required=True)
    ap.add_argument("--database", default="qsm")
    ap.add_argument("--password-env", default="GOPS_MYSQL_PASSWORD",
                    help="密码所在环境变量名（默认 GOPS_MYSQL_PASSWORD）")
    ap.add_argument("--ddl", default="",
                    help="ddl/mysql_qsm_v1.sql 路径（可选，用于列级核对）")
    args = ap.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        print("FATAL: password env %s is empty; refusing to prompt "
              "(credentials never via argv)" % args.password_env,
              file=sys.stderr)
        return 5

    print("=" * 66)
    print("MySQL 初始化检查（只读）  target=%s@%s:%s/%s"
          % (args.user, args.host, args.port, args.database))
    print("=" * 66)

    # ---- 1. 连接（不含选库，先证明网络+鉴权） ----
    try:
        conn = pymysql.connect(
            host=args.host, port=args.port, user=args.user,
            password=password, charset="utf8mb4",
            connect_timeout=10)
    except pymysql.err.MySQLError as exc:
        print("[连接] FAIL: %s" % exc)
        print("[结论] 退出码 3：连接/鉴权失败。检查凭据、防火墙、端口。")
        return 3
    print("[连接] OK（TCP + 鉴权通过）")

    warnings: list[str] = []

    # ---- 2. 版本 ----
    row = _query(conn, "SELECT VERSION()")[0]
    version = row[0]
    print("[版本] server=%s  protocol=%s"
          % (version, _query(conn, "SELECT @@protocol_version")[0][0]))
    if not version.startswith(BASELINE["server_version_prefix"]):
        warnings.append("服务器版本 %s 与实测基线 5.7.x 不一致——DATETIME(3) "
                        "行为需复核" % version)

    # ---- 3. 目标库存在性 ----
    dbs = {r[0] for r in _query(conn, "SHOW DATABASES")}
    if args.database not in dbs:
        print("[库] FAIL: 数据库 %s 不存在（现有库: %s）"
              % (args.database, sorted(dbs)))
        print("[结论] 退出码 4：需先与管理员确认建库。⛔ 建库亦属 DDL，"
              "本脚本不执行。")
        conn.close()
        return 4
    print("[库] OK: %s 已存在" % args.database)

    # ---- 4. 字符集 / 排序规则 / 时区 ----
    conn.select_db(args.database)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME "
            "FROM information_schema.SCHEMATA WHERE SCHEMA_NAME=%s",
            (args.database,))
        cs = cur.fetchone()
    print("[字符集] db=%s / %s（实测基线 %s / %s）"
          % (cs[0], cs[1], BASELINE["db_charset"], BASELINE["db_collation"]))
    if (cs[0], cs[1]) != (BASELINE["db_charset"], BASELINE["db_collation"]):
        warnings.append("库字符集/排序规则与实测基线不一致：%s/%s"
                        % (cs[0], cs[1]))
    tz = _query(conn, "SELECT @@system_time_zone, @@time_zone, NOW()")
    print("[时区] system=%s session=%s now=%s"
          % (tz[0][0], tz[0][1], tz[0][2]))
    if tz[0][0] != BASELINE["system_time_zone"]:
        warnings.append("system_time_zone=%s 与实测基线 CST(UTC+8) 不一致——"
                        "DATETIME(3) 存服务端本地时间，需复核" % tz[0][0])

    # ---- 5. 表清单（是否空库 / 是否已有表） ----
    tables = [r[0] for r in _query(conn, "SHOW TABLES")]
    print("[表] 数量=%d" % len(tables))
    for t in sorted(tables):
        print("       - %s" % t)

    expected = set(EXPECTED_TABLES)
    present_expected = expected & set(tables)
    foreign = set(tables) - expected
    missing = expected - set(tables)

    # ---- 6. 权限 ----
    grants = [r[0] for r in _query(conn, "SHOW GRANTS")]
    grant_text = " | ".join(grants)
    print("[权限] %s" % " ; ".join(grants))
    if "ALL PRIVILEGES" not in grant_text.upper():
        lacked = [g for g in NEEDED_GRANTS if g not in grant_text.upper()]
        if lacked:
            warnings.append("缺少权限: %s（DDL 执行会失败）" % ",".join(lacked))

    # ---- 7. DDL 风险结论 ----
    print("-" * 66)
    ddl_cols = {}
    if args.ddl and os.path.exists(args.ddl):
        try:
            ddl_cols = _parse_ddl_columns(args.ddl)
            print("[DDL 对照] 解析 %s -> %d 张表"
                  % (os.path.basename(args.ddl), len(ddl_cols)))
        except OSError as exc:
            warnings.append("DDL 文件读取失败: %s" % exc)

    if not tables:
        verdict = "空库：0 张表"
        print("[风险] LOW——目标库为空。可提交 DDL 批准申请；"
              "批准后执行 ddl/mysql_qsm_v1.sql（CREATE TABLE IF NOT EXISTS）。")
        print("[提醒] ⛔ 执行前仍须：① 用户明确批准 ② 空库基线备份一次 "
              "③ 字符集/时区如上核对无误。")
        code = 0
    elif foreign:
        verdict = "非预期表 %d 张（%s）" % (
            len(foreign), ",".join(sorted(foreign)[:8]))
        print("[风险] HIGH——qsm 是共享库，存在非本项目表。⛔ 不得直接执行 DDL："
              "先人工核对这些表的归属与是否已被他人使用。")
        code = 2
    elif present_expected and not missing:
        verdict = "六表齐全（%d/%d）" % (len(present_expected), len(expected))
        print("[风险] MEDIUM——六表已存在，疑似 DDL 已执行过。"
              "⛔ 不要重复执行；改做结构核对。")
        code = 2
    else:
        verdict = "部分表存在（缺 %s）" % ",".join(sorted(missing))
        print("[风险] HIGH——表不齐全（缺 %s）。可能是他人建了一半或历史残留，"
              "⛔ 不得直接补跑 DDL，先人工核对。" % ",".join(sorted(missing)))
        code = 2

    # 列级核对（仅在提供了 DDL 且表存在时）
    if ddl_cols and present_expected:
        for t in sorted(present_expected):
            want = ddl_cols.get(t)
            if not want:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (args.database, t))
                got = {r[0] for r in cur.fetchall()}
            if got == want:
                print("[列核对] %s OK（%d 列一致）" % (t, len(got)))
            else:
                extra, lack = got - want, want - got
                print("[列核对] %s 漂移：多 %s / 缺 %s"
                      % (t, sorted(extra) or "-", sorted(lack) or "-"))
                warnings.append("表 %s 列与 DDL 不一致（多 %s / 缺 %s）——"
                                "DAO 依赖列名，⛔ 必须人工复核"
                                % (t, sorted(extra), sorted(lack)))

    if warnings:
        print("-" * 66)
        print("[警告 %d 条]" % len(warnings))
        for w in warnings:
            print("  ! %s" % w)

    print("=" * 66)
    print("[结论] %s | 退出码 %d" % (verdict, code))
    print("=" * 66)
    conn.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
