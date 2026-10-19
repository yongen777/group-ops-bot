# -*- coding: utf-8 -*-
"""幂等与回查机制（§4.11.3，§2.5.0 两类键的落地点）。

实现保证（对应文档条目）：
1. request_id 是幂等的唯一载体；operation_log.request_id 唯一索引（物理载体）
2. 单事务：INSERT operation_log(pending) -> 业务写 -> UPDATE operation_log 同事务提交
3. 重复投递判定（每次写工具先查 operation_log）：
   - 无记录   -> 正常流程
   - success  -> duplicate=true，返回原 record_id 与原结果（原样重试安全）
   - rejected -> duplicate=true，返回原拒绝结果（重放不改变结论）
   - pending  -> 返回 unknown（机器人侧归 C，走恢复查证，⛔ 不当成功也不当失败）
4. 并发兜底：唯一索引冲突（MySQL 1062 Duplicate entry）-> 按冲突在
   request_id 还是范围键归类
5. 总结覆盖：范围键已存在且 request_id 不同 -> UPDATE 内容 + 记新 request_id
   （由 dao.upsert_summary 实现，不经本模块的范围键拒绝路径）
6. 恢复查证入口 op.query_by_request 由 dao.get_op_log 支撑

⛔ 本模块不判断业务资格（名单/白名单/周期合法性归机器人侧，§4.11.0）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

import pymysql
import pymysql.err

from app.config import MysqlConfig, load_mysql_config
from .db.dao import Dao
from .db.pool import MysqlPool

# 范围键唯一索引名（与 ddl/mysql_qsm_v1.sql 一致；并发兜底靠它识别冲突类型）
UQ_CHECKIN_SCOPE = "uq_checkin_scope"
UQ_SUMMARY_SCOPE = "uq_summary_scope"
UQ_REPORT_SCOPE = "uq_report_scope"
_SCOPE_INDEXES = {UQ_CHECKIN_SCOPE, UQ_SUMMARY_SCOPE, UQ_REPORT_SCOPE}

# 结果状态词（与 §4.11.1 出参统一）
STATUS_SUCCESS = "success"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"


class ScopeRejected(Exception):
    """范围键已存在（本周期已提交）—— 转为 rejected 出参。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _verdict_from_log(entry: dict) -> dict:
    """按 operation_log 历史行给出重放结论（第 3 条）。"""
    status = entry["status"]
    if status == "success":
        result = {"status": STATUS_SUCCESS, "duplicate": True}
        if entry.get("result_json"):
            try:
                result.update(json.loads(entry["result_json"]))
            except (ValueError, TypeError):
                pass
        result.setdefault("record_id", entry.get("record_id"))
        return result
    if status == "rejected":
        return {"status": STATUS_REJECTED, "duplicate": True,
                "message": (entry.get("error_msg") or "已拒绝"),
                "error_code": entry.get("error_code")}
    # pending：上次写入未完结 -> unknown（机器人归 C）
    return {"status": STATUS_UNKNOWN, "duplicate": False,
            "message": "请求状态未知，等待查证"}


def _sanitize_input(payload: dict) -> str:
    """input_json 脱敏（§4.11.2：⛔ 不含令牌/凭据）。

    工具入参本身不含令牌（令牌只在机器人侧），这里做纵深防御：
    显式剔除已知敏感键，其余按 JSON 存档。
    """
    forbidden = {"token", "token_value", "password", "secret", "credential",
                 "authorization", "mysql_password", "mcp_key"}
    cleaned = {k: v for k, v in payload.items() if k.lower() not in forbidden}
    return json.dumps(cleaned, ensure_ascii=False, default=str)


class IdempotentWriter:
    """所有写工具的统一入口（§4.11.3 第 2 条单事务 + 第 3/4 条判定）。"""

    def __init__(self, pool: MysqlPool) -> None:
        self.pool = pool
        self.dao = Dao(pool)

    def execute(self, *, tool_name: str, biz_type: str, group_id: Optional[str],
                user_id: Optional[int], period_key: Optional[str],
                request_id: str, payload: dict,
                biz_fn: Callable) -> dict:
        """执行一次写工具调用。

        biz_fn(cur, dao) 在已开启的事务内执行业务写，返回
        (record_id, extra_dict)；抛 ScopeRejected 表示范围键已存在。
        """
        input_json = _sanitize_input(payload)

        # 1) 快路径：重复投递判定（第 3 条）
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                existing = self.dao.get_op_log(cur, request_id)
        if existing is not None:
            return _verdict_from_log(existing)

        # 2) 单事务：pending -> 业务写 -> success/rejected（第 2 条）
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    self.dao.insert_op_log(
                        cur, request_id, tool_name, biz_type, group_id,
                        user_id, period_key, input_json)
                    record_id, extra = biz_fn(cur, self.dao)
                    result = {"status": STATUS_SUCCESS, "duplicate": False,
                              "record_id": record_id, "message": "ok"}
                    result.update(extra or {})
                    self.dao.finish_op_log(
                        cur, request_id, STATUS_SUCCESS,
                        json.dumps(result, ensure_ascii=False, default=str),
                        record_id, None, None)
                conn.commit()
                return result
        except ScopeRejected as exc:
            # 范围键已存在（本周期已提交）—— 落一条 rejected 日志保证重放结论一致
            self._log_rejected(request_id, tool_name, biz_type, group_id,
                               user_id, period_key, input_json,
                               "scope_conflict", exc.message)
            return {"status": STATUS_REJECTED, "duplicate": False,
                    "message": exc.message, "error_code": "scope_conflict"}
        except pymysql.err.IntegrityError as exc:
            return self._handle_integrity_error(
                exc, request_id, tool_name, biz_type, group_id, user_id,
                period_key, input_json)

    def _handle_integrity_error(self, exc, request_id, tool_name, biz_type,
                                group_id, user_id, period_key, input_json) -> dict:
        """并发兜底（第 4 条）：1062 Duplicate entry 按冲突索引归类。"""
        if exc.args and exc.args[0] == 1062:
            message = str(exc)
            if any(idx in message for idx in _SCOPE_INDEXES):
                # 范围键冲突（另一请求已成功提交同范围）
                self._log_rejected(request_id, tool_name, biz_type, group_id,
                                   user_id, period_key, input_json,
                                   "scope_conflict", "本周期已提交")
                return {"status": STATUS_REJECTED, "duplicate": False,
                        "message": "本周期已提交",
                        "error_code": "scope_conflict"}
            # request_id 唯一索引冲突：并发同请求 -> 按 duplicate 回查原结果
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    existing = self.dao.get_op_log(cur, request_id)
            if existing is not None:
                return _verdict_from_log(existing)
            # 极端：日志行不存在却撞了唯一索引（不应发生）-> unknown，⛔ 不猜
            return {"status": STATUS_UNKNOWN, "duplicate": False,
                    "message": "请求状态未知，等待查证"}
        # 非唯一索引类完整性错误（如外键/非空）-> 如实 unknown，⛔ 不伪造结论
        return {"status": STATUS_UNKNOWN, "duplicate": False,
                "message": "写入失败且结论未知：%s" % exc,
                "error_code": "integrity_error"}

    def _log_rejected(self, request_id, tool_name, biz_type, group_id, user_id,
                      period_key, input_json, error_code, error_msg) -> None:
        """拒绝结论落库（重放时返回原拒绝，重放不改变结论）。"""
        try:
            with self.pool.connection() as conn:
                with conn.cursor() as cur:
                    self.dao.insert_op_log(
                        cur, request_id, tool_name, biz_type, group_id,
                        user_id, period_key, input_json)
                    self.dao.finish_op_log(
                        cur, request_id, STATUS_REJECTED,
                        json.dumps({"status": STATUS_REJECTED,
                                    "duplicate": False,
                                    "message": error_msg},
                                   ensure_ascii=False),
                        None, error_code, error_msg)
                conn.commit()
        except pymysql.err.MySQLError:
            # 日志落库失败不影响主结论（已由返回值给出），但记 pending 由恢复兜底
            pass

    # ---------- 只读：恢复查证（§4.11.3 第 6 条 / §4.11.1 op.query_by_request） ----------

    def query_by_request(self, request_id: str) -> dict:
        with self.pool.connection() as conn:
            with conn.cursor() as cur:
                entry = self.dao.get_op_log(cur, request_id)
        if entry is None:
            return {"status": STATUS_UNKNOWN, "found": False,
                    "message": "无此请求记录"}
        result = _verdict_from_log(entry)
        result["found"] = True
        result["tool_name"] = entry["tool_name"]
        result["created_time"] = str(entry["created_time"])
        return result
