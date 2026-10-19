# -*- coding: utf-8 -*-
"""DDL ↔ DAO 只读一致性核对（脱库：只读两个文件，⛔ 不连数据库）。

核对内容：
1. dao.py 引用的六张表都在 DDL 中存在
2. dao.py 每条 INSERT 的列名都在对应 DDL 表定义中
3. DDL 的唯一键 / 索引名与设计意图对齐（范围键唯一约束存在）
4. 输出 PASS/FAIL 明细（任一 FAIL 即整体 FAIL）

用法：python ddl_dao_check.py
⛔ 本脚本⛔ 不执行任何 SQL、⛔ 不连接MySQL、⛔ 不修改任何文件。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DDL_PATH = ROOT / "ddl" / "mysql_qsm_v1.sql"
DAO_PATH = ROOT / "group-ops-bot" / "mcp_server" / "db" / "dao.py"

EXPECTED_TABLES = ("sys_user", "checkin_record", "summary_record",
                   "report_record", "report_item", "operation_log")


def parse_ddl_tables(text: str) -> dict:
    """提取每张 CREATE TABLE 的列名集合（粗解析：取反引号标识符）。"""
    tables: dict = {}
    for match in re.finditer(
            r"CREATE TABLE (?:IF NOT EXISTS )?`(\w+)`\s*\((.*?)\n\) ENGINE",
            text, re.S):
        name, body = match.group(1), match.group(2)
        columns = set()
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("`"):
                columns.add(re.match(r"`(\w+)`", line).group(1))
            elif line.upper().startswith(("PRIMARY KEY", "UNIQUE KEY",
                                          "KEY ", "CONSTRAINT")):
                continue
        tables[name] = columns
    return tables


def parse_ddl_keys(text: str) -> list:
    return re.findall(r"UNIQUE KEY `(\w+)`\s*\(([^)]+)\)", text)


def _clean_col(piece: str) -> str:
    """从可能跨字符串字面量拼接的片段中提取列标识符。

    dao.py 的列名写在相邻字符串字面量里（source,"\n  " created_time），
    直接 split + strip 会残留引号；这里取第一个标识符形态的 token。
    """
    m = re.match(r'[\s"\'`]*([A-Za-z_]\w*)', piece)
    return m.group(1) if m else ""


def main() -> int:
    ddl = DDL_PATH.read_text(encoding="utf-8")
    dao = DAO_PATH.read_text(encoding="utf-8")
    failures: list = []

    tables = parse_ddl_tables(ddl)
    print("== DDL 表清单 ==")
    for name in sorted(tables):
        print("  %-16s columns=%d" % (name, len(tables[name])))

    missing = [t for t in EXPECTED_TABLES if t not in tables]
    if missing:
        failures.append("DDL 缺少表: %s" % missing)
    extra = [t for t in tables if t not in EXPECTED_TABLES]
    if extra:
        failures.append("DDL 多出预期外的表: %s" % extra)

    # 唯一键存在性（范围键唯一约束，§4.11.2 / §2.5.0）—— 按**列**判定，不按键名
    keys = parse_ddl_keys(ddl)
    print("== DDL 唯一键 ==")
    for name, cols in keys:
        print("  %-24s (%s)" % (name, cols.strip()))
    key_cols = [tuple(c.strip().strip("`") for c in cols.split(","))
                for _name, cols in keys]
    for required in (("group_id", "user_id", "period_key", "session"),
                     ("group_id", "user_id", "period_key"),
                     ("request_id",), ("phone",)):
        if not any(set(required) <= set(k) for k in key_cols):
            failures.append("缺少覆盖列 %s 的唯一键" % (required,))

    # dao.py 的 INSERT 列名核对（表名走 %s 占位 + T_ 常量，需先解析常量表）
    print("== dao INSERT 列名核对 ==")
    t_const = {name: table for name, table in
               re.findall(r'T_(\w+)\s*=\s*"(\w+)"', dao)}
    insert_re = re.compile(r'INSERT INTO %s\s*\(([^)]+)\)')
    checked = 0
    for match in insert_re.finditer(dao):
        cols = [_clean_col(c) for c in match.group(1).split(",")]
        cols = [c for c in cols if c]
        # 格式化目标在全部相邻字面量之后；两种形态都要认：
        #   % (T_USER, "%s", ...)  元组
        #   % T_REPORT_ITEM        裸常量
        tail = dao[match.end():match.end() + 3000]
        table = None
        fmt = re.search(r'%\s*\(?\s*(T_\w+)', tail)
        if fmt:
            name = fmt.group(1)[2:]
            if name in t_const:
                table = t_const[name]
        if table is None:
            failures.append("无法解析 INSERT 的表名（占位符上下文）: %s"
                            % ",".join(cols))
            continue
        bad = [c for c in cols if c not in tables.get(table, set())]
        checked += 1
        status = "OK " if not bad else "FAIL"
        print("  %s %-16s %s" % (status, table, ",".join(cols)))
        if bad:
            failures.append("dao INSERT %s 列不存在于 DDL: %s" % (table, bad))
    if checked == 0:
        failures.append("dao.py 中未找到任何 INSERT（解析失败？）")

    # SELECT ... FROM %s 的表名可解析性（JOIN 别名的列明细不在本脚本 scope）
    print("== dao SELECT 表名解析 ==")
    select_re = re.compile(r'SELECT\s+.+?\s+FROM\s+%s', re.S)
    resolved = set()
    for match in select_re.finditer(dao):
        tail = dao[match.end():match.end() + 120]
        m = re.search(r'T_(\w+)\b', tail)
        if m and m.group(1) in t_const:
            resolved.add(t_const[m.group(1)])
    for table in EXPECTED_TABLES:
        if table not in resolved:
            failures.append("dao.py 未找到指向 %s 的 SELECT" % table)
    print("  可解析 SELECT 表: %s" % sorted(resolved))

    print()
    if failures:
        print("RESULT: FAIL (%d)" % len(failures))
        for f in failures:
            print("  -", f)
        return 1
    print("RESULT: PASS —— DDL 与 DAO 表名/列名/唯一键一致（%d 条 INSERT 核对）"
          % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
