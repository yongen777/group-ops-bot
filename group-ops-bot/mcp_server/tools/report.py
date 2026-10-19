# -*- coding: utf-8 -*-
"""report.* 工具（§4.11.1 契约，2026-10-13 字段池化修订）。

- report.submit      同 checkin.submit 的额度语义；items 一次性写入，
                     ⛔ 不做增量追加；field_id 为字段池引用（有效性由机器人侧
                     校验），field_name / unit 为提交时快照，MCP Server
                     只存不校验（§4.11.0 职责边界）
- report.query_status / query_records  只读；导出含明细项
"""

from __future__ import annotations

from typing import Optional

TOOL_SUBMIT = "report.submit"


def handle_submit(writer, *, user_id: int, group_id: str, period_key: str,
                  role: Optional[str], items: list, request_id: str,
                  entry_source: str = "user") -> dict:
    """items: [{field_id, field_name, quantity, unit?}]（快照结构，§4.11.1）。"""

    def biz(cur, dao):
        record_id = dao.insert_report(cur, user_id, group_id, period_key,
                                      role, request_id,
                                      entry_source=entry_source)
        dao.insert_report_items(cur, record_id, items)
        return record_id, {"role": role, "item_count": len(items)}

    payload = {
        "user_id": user_id, "group_id": group_id, "period_key": period_key,
        "role": role, "items": items, "entry_source": entry_source,
        "request_id": request_id,
    }
    return writer.execute(
        tool_name=TOOL_SUBMIT, biz_type="report", group_id=group_id,
        user_id=user_id, period_key=period_key, request_id=request_id,
        payload=payload, biz_fn=biz)


def handle_query_status(writer, *, user_id: int, group_id: str,
                        period_key: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            row = writer.dao.query_report_status(cur, user_id, group_id,
                                                 period_key)
    return row or {"completed": False, "record_id": None}


def handle_query_records(writer, *, group_id: str, period_key: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            records = writer.dao.query_report_records(cur, group_id, period_key)
    return {"records": records}


def handle_admin_update(writer, *, user_id: int, group_id: str,
                        period_key: str, items=None,
                        submit_time=None) -> dict:
    """report.admin_update（2026-10-19 D17）：管理员修改既有报数。

    items: [{"field_id": int, "quantity": int}]，只改传入的字段；
    ⛔ 无记录 -> rejected（补录走 report.submit）。
    """
    normalized = []
    for item in items or []:
        try:
            field_id = int(item.get("field_id"))
            quantity = int(item.get("quantity"))
        except (TypeError, ValueError, AttributeError):
            return {"status": "rejected", "record_id": None,
                    "message": "报数字段必须是整数"}
        if quantity < 0:
            return {"status": "rejected", "record_id": None,
                    "message": "报数必须是非负整数"}
        normalized.append({"field_id": field_id, "quantity": quantity})
    if not normalized and not submit_time:
        return {"status": "rejected", "record_id": None,
                "message": "没有需要修改的内容"}
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            changed = writer.dao.update_report_admin(
                cur, group_id, user_id, period_key,
                items=normalized, submit_time=submit_time)
        conn.commit()
    if not changed:
        return {"status": "rejected", "record_id": None,
                "message": "该用户本周期还没有报数记录"}
    return {"status": "success", "record_id": None, "message": "ok"}


def register(mcp, writer) -> None:
    @mcp.tool(name=TOOL_SUBMIT)
    def report_submit(user_id: int, group_id: str, period_key: str,
                      role: str | None, items: list, request_id: str,
                      entry_source: str = "user") -> dict:
        """提交一次报数（多字段明细一次性写入）。role 为本次实际所用角色。
        entry_source 由机器人侧计算（user/admin）。"""
        return handle_submit(writer, user_id=user_id, group_id=group_id,
                             period_key=period_key, role=role, items=items,
                             request_id=request_id,
                             entry_source=entry_source)

    @mcp.tool(name="report.query_status")
    def report_query_status(user_id: int, group_id: str, period_key: str) -> dict:
        """查询报数完成状态（只读）。"""
        return handle_query_status(writer, user_id=user_id, group_id=group_id,
                                   period_key=period_key)

    @mcp.tool(name="report.query_records")
    def report_query_records(group_id: str, period_key: str) -> dict:
        """查询某群某周期的报数记录（含明细项，只读，导出用）。"""
        return handle_query_records(writer, group_id=group_id,
                                    period_key=period_key)

    @mcp.tool(name="report.admin_update")
    def report_admin_update(user_id: int, group_id: str, period_key: str,
                            items: list | None = None,
                            submit_time: str | None = None) -> dict:
        """管理员修改既有报数（各字段数值 / 提交时间）。无记录返回 rejected。"""
        return handle_admin_update(writer, user_id=user_id,
                                   group_id=group_id, period_key=period_key,
                                   items=items, submit_time=submit_time)
