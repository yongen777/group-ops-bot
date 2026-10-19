# -*- coding: utf-8 -*-
"""summary.* 工具（§4.11.1 契约）。

- summary.save       范围键唯一但语义为**覆盖**：范围键已存在且非同一请求 ->
                     UPDATE content + 记新 request_id；同 request_id 重放 ->
                     duplicate=true 返回原结果
- summary.query_status / query_records  只读

⚠️ 覆盖语义硬约束（§2.5.0）：每次覆盖必须换新 request_id，否则会被
幂等机制当成重复投递而原样返回首次结果 —— 「看起来提交了，实际没生效」。
机器人侧负责为每次覆盖生成新键；本模块只保证「新键 -> 真覆盖」。
"""

from __future__ import annotations

TOOL_SAVE = "summary.save"


def handle_save(writer, *, user_id: int, group_id: str, period_key: str,
                period_type: str, content: str, request_id: str,
                entry_source: str = "user") -> dict:
    def biz(cur, dao):
        record_id, created = dao.upsert_summary(
            cur, user_id, group_id, period_key, period_type, content, request_id,
            entry_source=entry_source)
        return record_id, {"created": created, "period_type": period_type}

    payload = {
        "user_id": user_id, "group_id": group_id, "period_key": period_key,
        "period_type": period_type, "content": content,
        "entry_source": entry_source, "request_id": request_id,
    }
    return writer.execute(
        tool_name=TOOL_SAVE, biz_type="summary", group_id=group_id,
        user_id=user_id, period_key=period_key, request_id=request_id,
        payload=payload, biz_fn=biz)


def handle_query_status(writer, *, user_id: int, group_id: str,
                        period_key: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            row = writer.dao.query_summary_status(cur, user_id, group_id,
                                                  period_key)
    return row or {"completed": False, "record_id": None, "updated_time": None}


def handle_query_records(writer, *, group_id: str, period_key: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            records = writer.dao.query_summary_records(cur, group_id, period_key)
    return {"records": records}


def register(mcp, writer) -> None:
    @mcp.tool(name=TOOL_SAVE)
    def summary_save(user_id: int, group_id: str, period_key: str,
                     period_type: str, content: str, request_id: str,
                     entry_source: str = "user") -> dict:
        """保存日报/周报。同周期重复提交为覆盖（须换新 request_id）。
        entry_source 由机器人侧计算（user/admin）。"""
        return handle_save(writer, user_id=user_id, group_id=group_id,
                           period_key=period_key, period_type=period_type,
                           content=content, request_id=request_id,
                           entry_source=entry_source)

    @mcp.tool(name="summary.query_status")
    def summary_query_status(user_id: int, group_id: str, period_key: str) -> dict:
        """查询总结完成状态（只读）。"""
        return handle_query_status(writer, user_id=user_id, group_id=group_id,
                                   period_key=period_key)

    @mcp.tool(name="summary.query_records")
    def summary_query_records(group_id: str, period_key: str) -> dict:
        """查询某群某周期的总结记录（只读）。"""
        return handle_query_records(writer, group_id=group_id,
                                    period_key=period_key)
