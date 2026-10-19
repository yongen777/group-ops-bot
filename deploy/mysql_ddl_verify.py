# -*- coding: utf-8 -*-
"""DDL 执行后验证（只读）——阶段 A 第 4 项。

对照 ddl/mysql_qsm_v1.sql 逐项核验线上 schema：
  1. 六张目标表是否创建成功（表集合精确相等，不多不少）
  2. 主键（PRIMARY KEY 列序）
  3. 索引（UNIQUE KEY / 二级 KEY 名称 + 列序）
  4. 字段类型（列名 + 类型，整数类型忽略显示宽度）
  5. 字符集 / 排序规则 / 引擎
  6. 表数量

⛔ 本脚本只执行 SHOW / SELECT（硬闸同 mysql_check.py）：不建表、不写数据。
⛔ 不修改 DDL 文件；DDL 的预期结构从这里实时解析，两边同源才可比。

用法（密码走环境变量）：
    GOPS_MYSQL_PASSWORD='...' python3 mysql_ddl_verify.py \
        --host 219.151.177.33 --port 28619 --user qsm --database qsm \
        --ddl /path/to/ddl/mysql_qsm_v1.sql

退出码：0 = 全部通过；1 = 存在不一致；3 = 连接失败；5 = 用法/参数错误。
"""

from __future__ import annotations

import argparse
import os
import re
import sys

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("FATAL: pymysql not installed.", file=sys.stderr)
    sys.exit(5)

EXPECTED_TABLES = (
    "sys_user",
    "checkin_record",
    "summary_record",
    "report_record",
    "report_item",
    "operation_log",
)

# 整数类型在 information_schema.COLUMN_TYPE 里带显示宽度（bigint(20)/tinyint(4)），
# DDL 不写宽度 —— 归一化时剥掉，避免假阳性
_INT_TYPES = ("bigint", "int", "smallint", "tinyint", "mediumint")

_TABLE_RE = re.compile(
    r"CREATE TABLE IF NOT EXISTS `(\w+)` \((.*?)\n\)\s*"
    r"ENGINE=(\w+)\s+DEFAULT\s+CHARSET=(\w+)\s+COLLATE=(\w+)",
    re.S)
_COL_RE = re.compile(r"^`(\w+)`\s+([A-Za-z]+(?:\([0-9,]+\))?)")
_PK_RE = re.compile(r"^PRIMARY KEY \((.*?)\)$")
_UQ_RE = re.compile(r"^UNIQUE KEY `(\w+)` \((.*?)\)$")
_KX_RE = re.compile(r"^KEY `(\w+)` \((.*?)\)$")


def _readonly_guard(sql: str) -> str:
    """⛔ 硬闸：任何非 SHOW/SELECT 语句直接拒绝执行。"""
    head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
    if head not in ("SHOW", "SELECT"):
        raise RuntimeError("readonly guard: refusing to execute %r" % sql[:60])
    return sql


def _query(conn, sql: str, params=None):
    _readonly_guard(sql)
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _norm_type(t: str) -> str:
    t = t.strip().lower()
    base = t.split("(")[0]
    if base in _INT_TYPES:
        return base
    return t


def _split_cols(text: str) -> list:
    return [c.strip().strip("`") for c in text.split(",") if c.strip()]


def parse_ddl(path: str) -> dict:
    """解析 DDL -> {table: {columns, pk, unique, index, engine, charset, collation}}"""
    text = open(path, "r", encoding="utf-8").read()
    out: dict = {}
    for m in _TABLE_RE.finditer(text):
        name, body, engine, charset, coll = m.groups()
        t = {"columns": {}, "pk": [], "unique": {}, "index": {},
             "engine": engine.lower(), "charset": charset.lower(),
             "collation": coll.lower()}
        for line in body.splitlines():
            s = line.strip().rstrip(",").strip()
            if not s:
                continue
            mp = _PK_RE.match(s)
            mu = _UQ_RE.match(s)
            mk = _KX_RE.match(s)
            if mp:
                t["pk"] = _split_cols(mp.group(1))
            elif mu:
                t["unique"][mu.group(1)] = _split_cols(mu.group(2))
            elif mk:
                t["index"][mk.group(1)] = _split_cols(mk.group(2))
            else:
                mc = _COL_RE.match(s)
                if mc:
                    t["columns"][mc.group(1)] = _norm_type(mc.group(2))
        out[name] = t
    return out


def live_schema(conn, db: str) -> dict:
    """从 information_schema 拉线上结构，归一成与 parse_ddl 同构的 dict。"""
    tables = [r[0] for r in _query(conn, "SHOW TABLES")]
    meta = {r[0]: {"engine": (r[1] or "").lower(),
                   "collation": (r[2] or "").lower()}
            for r in _query(
                conn,
                "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
                (db,))}
    cols: dict = {}
    for tname, cname, ctype, nullable in _query(
            conn,
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE "
            "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION", (db,)):
        cols.setdefault(tname, {})[cname] = {
            "type": _norm_type(ctype), "nullable": nullable}
    idx: dict = {}
    for tname, iname, nonuniq, seq, cname in _query(
            conn,
            "SELECT TABLE_NAME, INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, "
            "COLUMN_NAME FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA=%s "
            "ORDER BY TABLE_NAME, INDEX_NAME, SEQ_IN_INDEX", (db,)):
        slot = idx.setdefault(tname, {"pk": [], "unique": {}, "index": {}})
        if iname == "PRIMARY":
            slot["pk"].append((seq, cname))
        elif nonuniq == 0:
            slot["unique"].setdefault(iname, []).append((seq, cname))
        else:
            slot["index"].setdefault(iname, []).append((seq, cname))
    out: dict = {}
    for t in tables:
        m = meta.get(t, {})
        i = idx.get(t, {"pk": [], "unique": {}, "index": {}})
        out[t] = {
            "columns": cols.get(t, {}),
            "pk": [c for _s, c in sorted(i["pk"])],
            "unique": {k: [c for _s, c in sorted(v)]
                       for k, v in i["unique"].items()},
            "index": {k: [c for _s, c in sorted(v)]
                      for k, v in i["index"].items()},
            "engine": m.get("engine", ""),
            "charset": (m.get("collation", "").split("_")[0]
                        if m.get("collation") else ""),
            "collation": m.get("collation", ""),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="group-ops DDL post-verify (readonly)")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", required=True)
    ap.add_argument("--database", default="qsm")
    ap.add_argument("--password-env", default="GOPS_MYSQL_PASSWORD")
    ap.add_argument("--ddl", required=True)
    args = ap.parse_args()

    password = os.environ.get(args.password_env, "")
    if not password:
        print("FATAL: password env %s is empty (credentials never via argv)"
              % args.password_env, file=sys.stderr)
        return 5
    if not os.path.exists(args.ddl):
        print("FATAL: DDL file not found: %s" % args.ddl, file=sys.stderr)
        return 5

    ddl = parse_ddl(args.ddl)
    print("=" * 70)
    print("DDL 执行后验证（只读）  target=%s@%s:%s/%s"
          % (args.user, args.host, args.port, args.database))
    print("DDL 解析：%d 张表 -> %s"
          % (len(ddl), ", ".join(sorted(ddl))))
    print("=" * 70)

    try:
        conn = pymysql.connect(
            host=args.host, port=args.port, user=args.user,
            password=password, charset="utf8mb4", connect_timeout=10)
    except pymysql.err.MySQLError as exc:
        print("[连接] FAIL: %s" % exc)
        return 3
    conn.select_db(args.database)

    failures: list = []
    live = live_schema(conn, args.database)

    # ---- 1. 表数量 + 表集合（精确相等） ----
    live_tables = set(live)
    want_tables = set(ddl)
    print("\n[1] 表数量：线上 %d / 期望 %d" % (len(live_tables), len(want_tables)))
    extra = live_tables - want_tables
    missing = want_tables - live_tables
    if extra:
        failures.append("多出非预期表: %s" % sorted(extra))
    if missing:
        failures.append("缺少目标表: %s" % sorted(missing))
    if not extra and not missing:
        print("    ✅ 六张目标表全部创建，且无多出表")
    for t in sorted(live_tables):
        print("      - %s" % t)

    # ---- 2~5. 逐表对照 ----
    for t in sorted(want_tables & live_tables):
        d, l = ddl[t], live[t]
        print("\n[2] 表 `%s`" % t)
        # 主键
        if d["pk"] != l["pk"]:
            failures.append("%s 主键不一致: DDL=%s 线上=%s"
                            % (t, d["pk"], l["pk"]))
            print("    ❌ 主键: DDL=%s 线上=%s" % (d["pk"], l["pk"]))
        else:
            print("    ✅ 主键 (%s)" % ", ".join(l["pk"]))
        # 唯一键
        if d["unique"] != l["unique"]:
            failures.append("%s 唯一键不一致: DDL=%s 线上=%s"
                            % (t, d["unique"], l["unique"]))
            print("    ❌ 唯一键: DDL=%s 线上=%s" % (d["unique"], l["unique"]))
        else:
            for k, v in sorted(l["unique"].items()):
                print("    ✅ 唯一键 %s (%s)" % (k, ", ".join(v)))
        # 二级索引
        if d["index"] != l["index"]:
            failures.append("%s 二级索引不一致: DDL=%s 线上=%s"
                            % (t, d["index"], l["index"]))
            print("    ❌ 二级索引: DDL=%s 线上=%s" % (d["index"], l["index"]))
        else:
            for k, v in sorted(l["index"].items()):
                print("    ✅ 索引 %s (%s)" % (k, ", ".join(v)))
        # 字段（名称 + 类型）
        dcols, lcols = d["columns"], l["columns"]
        only_ddl = {k: v for k, v in dcols.items() if k not in lcols}
        only_live = {k: v["type"] for k, v in lcols.items() if k not in dcols}
        type_diff = {k: (v, lcols[k]["type"])
                     for k, v in dcols.items()
                     if k in lcols and lcols[k]["type"] != v}
        if only_ddl:
            failures.append("%s 缺列: %s" % (t, sorted(only_ddl)))
        if only_live:
            failures.append("%s 多出列（疑似被扩展）: %s" % (t, sorted(only_live)))
        for k, (dv, lv) in sorted(type_diff.items()):
            failures.append("%s.%s 类型不一致: DDL=%s 线上=%s" % (t, k, dv, lv))
        if not (only_ddl or only_live or type_diff):
            print("    ✅ 字段 %d 个，名称与类型全部一致" % len(lcols))
            for k in dcols:
                if k in lcols:
                    print("        %-18s %s" % (k, lcols[k]["type"]))
        else:
            if only_ddl:
                print("    ❌ 缺列: %s" % sorted(only_ddl))
            if only_live:
                print("    ❌ 多出列: %s" % sorted(only_live))
            for k, (dv, lv) in sorted(type_diff.items()):
                print("    ❌ 类型 %s: DDL=%s 线上=%s" % (k, dv, lv))
        # 引擎 / 字符集 / 排序规则
        eng_ok = d["engine"] == l["engine"]
        cs_ok = (d["charset"] == l["charset"]
                 and d["collation"] == l["collation"])
        if not eng_ok:
            failures.append("%s 引擎不一致: DDL=%s 线上=%s"
                            % (t, d["engine"], l["engine"]))
        if not cs_ok:
            failures.append("%s 字符集/排序不一致: DDL=%s/%s 线上=%s/%s"
                            % (t, d["charset"], d["collation"],
                               l["charset"], l["collation"]))
        if eng_ok and cs_ok:
            print("    ✅ 引擎 %s / 字符集 %s / 排序 %s"
                  % (l["engine"], l["charset"], l["collation"]))

    # ---- 6. 汇总 ----
    print("\n" + "=" * 70)
    if failures:
        print("[结论] ❌ %d 项不一致：" % len(failures))
        for f in failures:
            print("   - %s" % f)
        print("[结论] 退出码 1")
        conn.close()
        return 1
    print("[结论] ✅ 全部通过：6 表 / 主键 / 唯一键 / 索引 / 字段类型 / "
          "字符集 / 排序规则 / 引擎 与 DDL 完全一致")
    print("[结论] 退出码 0")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
