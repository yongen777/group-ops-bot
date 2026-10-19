# -*- coding: utf-8 -*-
"""MySQL `qsm` 数据访问层（§4.11.2 数据模型的 SQL 落地）。

纪律：
- 全部参数化查询，⛔ 不字符串拼值
- 表名 / 列名与 §4.11.2 字段设计一一对应；DDL 见 ddl/mysql_qsm_v1.sql
  （⛔ DDL 未经用户批准不得对任何库执行）
- MCP Server ⛔ 不判断业务资格（名单 / 白名单 / 周期合法性归机器人侧，
  §4.11.0）—— 本层只做「存 / 查 / 幂等判定」
- period_key 对 MCP Server 是不透明字符串（§4.11.0），本层⛔ 不解释其含义
"""

from __future__ import annotations

from typing import Iterable, Optional

from .pool import MysqlPool

# 表名集中定义，⛔ 不散落
T_USER = "sys_user"
T_CHECKIN = "checkin_record"
T_SUMMARY = "summary_record"
T_REPORT = "report_record"
T_REPORT_ITEM = "report_item"
T_OPLOG = "operation_log"


class Dao:
    """六张表的读写。所有方法要求调用方已持有连接（池借出态）。"""

    def __init__(self, pool: MysqlPool) -> None:
        self.pool = pool

    # ---------- sys_user ----------

    def upsert_user(self, cur, phone: str, name: Optional[str],
                    platform_user_id: Optional[str], department: Optional[str],
                    source: str) -> int:
        """按 phone upsert（手机号是稳定自然键，§4.11.1 user.upsert）。"""
        cur.execute(
            "INSERT INTO %s (phone, platform_user_id, name, department, source,"
            " created_time, updated_time)"
            " VALUES (%s, %s, %s, %s, %s, NOW(3), NOW(3))"
            " ON DUPLICATE KEY UPDATE"
            " platform_user_id = COALESCE(VALUES(platform_user_id), platform_user_id),"
            " name = COALESCE(VALUES(name), name),"
            " department = COALESCE(VALUES(department), department),"
            " updated_time = NOW(3)" % (T_USER, "%s", "%s", "%s", "%s", "%s"),
            (phone, platform_user_id, name, department, source))
        cur.execute("SELECT id FROM %s WHERE phone=%%s" % T_USER, (phone,))
        row = cur.fetchone()
        return int(row[0])

    def get_user(self, cur, phone: str) -> Optional[dict]:
        cur.execute(
            "SELECT id, phone, platform_user_id, name, department, source, status"
            " FROM %s WHERE phone=%%s" % T_USER, (phone,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(
            ("id", "phone", "platform_user_id", "name", "department", "source",
             "status"), row))

    def list_users(self, cur, phone_prefix: Optional[str] = None,
                   limit: int = 200) -> list:
        if phone_prefix:
            cur.execute(
                "SELECT id, phone, name, department, source, status FROM %s"
                " WHERE phone LIKE %%s ORDER BY phone LIMIT %%s" % T_USER,
                (phone_prefix + "%", limit))
        else:
            cur.execute(
                "SELECT id, phone, name, department, source, status FROM %s"
                " ORDER BY phone LIMIT %%s" % T_USER, (limit,))
        cols = ("id", "phone", "name", "department", "source", "status")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- checkin_record ----------

    def insert_checkin(self, cur, user_id: int, group_id: str, period_key: str,
                       session: str, image_ref: str, image_name: Optional[str],
                       image_size: Optional[int], request_id: str,
                       status: str = "NORMAL",
                       entry_source: str = "user") -> int:
        cur.execute(
            "INSERT INTO %s (user_id, group_id, period_key, session, checkin_time,"
            " image_ref, image_name, image_size, status, entry_source,"
            " request_id, created_time)"
            " VALUES (%%s, %%s, %%s, %%s, NOW(3), %%s, %%s, %%s, %%s, %%s, %%s, NOW(3))"
            % T_CHECKIN,
            (user_id, group_id, period_key, session, image_ref, image_name,
             image_size, status, entry_source, request_id))
        return int(cur.lastrowid)

    def query_checkin_status(self, cur, user_id: int, group_id: str,
                             period_key: str, session: str) -> Optional[dict]:
        cur.execute(
            "SELECT id, checkin_time FROM %s"
            " WHERE group_id=%%s AND user_id=%%s AND period_key=%%s AND session=%%s"
            % T_CHECKIN, (group_id, user_id, period_key, session))
        row = cur.fetchone()
        if not row:
            return None
        return {"completed": True, "record_id": row[0],
                "checkin_time": str(row[1])}

    def query_checkin_records(self, cur, group_id: str, period_key: str) -> list:
        cur.execute(
            "SELECT c.id, u.phone, u.name, c.period_key, c.session, c.checkin_time,"
            " c.image_ref, c.image_name, c.image_size, c.status, c.entry_source"
            " FROM %s c JOIN %s u ON u.id=c.user_id"
            " WHERE c.group_id=%%s AND c.period_key=%%s"
            " ORDER BY c.checkin_time" % (T_CHECKIN, T_USER),
            (group_id, period_key))
        cols = ("record_id", "phone", "name", "period_key", "session",
                "checkin_time", "image_ref", "image_name", "image_size",
                "status", "entry_source")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- summary_record ----------

    def upsert_summary(self, cur, user_id: int, group_id: str, period_key: str,
                       period_type: str, content: str, request_id: str,
                       entry_source: str = "user") -> tuple:
        """覆盖语义（§4.11.1 summary.save）：范围键唯一，同键不同 request -> UPDATE。

        返回 (record_id, created: bool)。created=True 为新建，False 为覆盖。
        """
        cur.execute(
            "SELECT id FROM %s"
            " WHERE group_id=%%s AND user_id=%%s AND period_key=%%s" % T_SUMMARY,
            (group_id, user_id, period_key))
        row = cur.fetchone()
        if row:
            record_id = int(row[0])
            cur.execute(
                "UPDATE %s SET content=%%s, period_type=%%s, request_id=%%s,"
                " entry_source=%%s, updated_time=NOW(3) WHERE id=%%s" % T_SUMMARY,
                (content, period_type, request_id, entry_source, record_id))
            return record_id, False
        cur.execute(
            "INSERT INTO %s (user_id, group_id, period_key, period_type, content,"
            " request_id, entry_source, created_time, updated_time)"
            " VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, NOW(3), NOW(3))" % T_SUMMARY,
            (user_id, group_id, period_key, period_type, content, request_id,
             entry_source))
        return int(cur.lastrowid), True

    def query_summary_status(self, cur, user_id: int, group_id: str,
                             period_key: str) -> Optional[dict]:
        cur.execute(
            "SELECT id, updated_time FROM %s"
            " WHERE group_id=%%s AND user_id=%%s AND period_key=%%s" % T_SUMMARY,
            (group_id, user_id, period_key))
        row = cur.fetchone()
        if not row:
            return None
        return {"completed": True, "record_id": row[0],
                "updated_time": str(row[1])}

    def query_summary_records(self, cur, group_id: str, period_key: str) -> list:
        cur.execute(
            "SELECT s.id, u.phone, u.name, s.period_key, s.period_type, s.content,"
            " s.created_time, s.updated_time, s.entry_source"
            " FROM %s s JOIN %s u ON u.id=s.user_id"
            " WHERE s.group_id=%%s AND s.period_key=%%s"
            " ORDER BY s.updated_time" % (T_SUMMARY, T_USER),
            (group_id, period_key))
        cols = ("record_id", "phone", "name", "period_key", "period_type",
                "content", "created_time", "updated_time", "entry_source")
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ---------- report_record / report_item ----------

    def insert_report(self, cur, user_id: int, group_id: str, period_key: str,
                      role: Optional[str], request_id: str,
                      entry_source: str = "user") -> int:
        cur.execute(
            "INSERT INTO %s (user_id, group_id, period_key, role, request_id,"
            " entry_source, created_time, updated_time)"
            " VALUES (%%s, %%s, %%s, %%s, %%s, %%s, NOW(3), NOW(3))" % T_REPORT,
            (user_id, group_id, period_key, role, request_id, entry_source))
        return int(cur.lastrowid)

    def insert_report_items(self, cur, record_id: int, items: list) -> None:
        """明细整批写入（§4.11.1：一次性写入，⛔ 不做增量追加）。"""
        if not items:
            return
        cur.executemany(
            "INSERT INTO %s (record_id, field_id, field_name, quantity, unit,"
            " created_time) VALUES (%%s, %%s, %%s, %%s, %%s, NOW(3))" % T_REPORT_ITEM,
            [(record_id, it["field_id"], it["field_name"], it["quantity"],
              it.get("unit")) for it in items])

    def query_report_status(self, cur, user_id: int, group_id: str,
                            period_key: str) -> Optional[dict]:
        cur.execute(
            "SELECT id FROM %s"
            " WHERE group_id=%%s AND user_id=%%s AND period_key=%%s" % T_REPORT,
            (group_id, user_id, period_key))
        row = cur.fetchone()
        if not row:
            return None
        return {"completed": True, "record_id": row[0]}

    def query_report_records(self, cur, group_id: str, period_key: str) -> list:
        cur.execute(
            "SELECT r.id, u.phone, u.name, r.period_key, r.role, r.created_time,"
            " r.entry_source"
            " FROM %s r JOIN %s u ON u.id=r.user_id"
            " WHERE r.group_id=%%s AND r.period_key=%%s"
            " ORDER BY r.created_time" % (T_REPORT, T_USER),
            (group_id, period_key))
        cols = ("record_id", "phone", "name", "period_key", "role",
                "created_time", "entry_source")
        records = [dict(zip(cols, r)) for r in cur.fetchall()]
        if not records:
            return []
        ids = [r["record_id"] for r in records]
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(
            "SELECT record_id, field_id, field_name, quantity, unit FROM %s"
            " WHERE record_id IN (%s) ORDER BY record_id, field_id"
            % (T_REPORT_ITEM, placeholders), ids)
        items_by_record: dict = {}
        for row in cur.fetchall():
            items_by_record.setdefault(row[0], []).append({
                "field_id": row[1], "field_name": row[2],
                "quantity": float(row[3]), "unit": row[4]})
        for r in records:
            r["items"] = items_by_record.get(r["record_id"], [])
        return records

    # ---------- operation_log ----------

    def insert_op_log(self, cur, request_id: str, tool_name: str, biz_type: str,
                      group_id: Optional[str], user_id: Optional[int],
                      period_key: Optional[str], input_json: str) -> None:
        """单事务第一步：pending 行（§4.11.3 第 2 条）。"""
        cur.execute(
            "INSERT INTO %s (request_id, tool_name, biz_type, group_id, user_id,"
            " period_key, input_json, status, created_time, updated_time)"
            " VALUES (%%s, %%s, %%s, %%s, %%s, %%s, %%s, 'pending', NOW(3), NOW(3))"
            % T_OPLOG,
            (request_id, tool_name, biz_type, group_id, user_id, period_key,
             input_json))

    def finish_op_log(self, cur, request_id: str, status: str,
                      result_json: str, record_id: Optional[int],
                      error_code: Optional[str], error_msg: Optional[str]) -> None:
        cur.execute(
            "UPDATE %s SET status=%%s, result_json=%%s, record_id=%%s,"
            " error_code=%%s, error_msg=%%s, updated_time=NOW(3)"
            " WHERE request_id=%%s" % T_OPLOG,
            (status, result_json, record_id, error_code, error_msg, request_id))

    def get_op_log(self, cur, request_id: str) -> Optional[dict]:
        cur.execute(
            "SELECT request_id, tool_name, biz_type, group_id, user_id, period_key,"
            " status, record_id, result_json, error_code, error_msg,"
            " created_time, updated_time FROM %s WHERE request_id=%%s" % T_OPLOG,
            (request_id,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(
            ("request_id", "tool_name", "biz_type", "group_id", "user_id",
             "period_key", "status", "record_id", "result_json", "error_code",
             "error_msg", "created_time", "updated_time"), row))

    def query_op_log(self, cur, tool_name: Optional[str] = None,
                     group_id: Optional[str] = None,
                     status: Optional[str] = None,
                     since: Optional[str] = None, limit: int = 200) -> list:
        sql = ("SELECT request_id, tool_name, biz_type, group_id, user_id,"
               " period_key, status, record_id, error_code, created_time"
               " FROM %s WHERE 1=1" % T_OPLOG)
        params: list = []
        if tool_name:
            sql += " AND tool_name=%s"
            params.append(tool_name)
        if group_id:
            sql += " AND group_id=%s"
            params.append(group_id)
        if status:
            sql += " AND status=%s"
            params.append(status)
        if since:
            sql += " AND created_time>=%s"
            params.append(since)
        sql += " ORDER BY id DESC LIMIT %s"
        params.append(limit)
        cur.execute(sql, params)
        cols = ("request_id", "tool_name", "biz_type", "group_id", "user_id",
                "period_key", "status", "record_id", "error_code", "created_time")
        return [dict(zip(cols, r)) for r in cur.fetchall()]
