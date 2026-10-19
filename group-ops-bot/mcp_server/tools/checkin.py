# -*- coding: utf-8 -*-
"""checkin.* 工具（§4.11.1 契约）。

- checkin.submit      范围键（group+user+period+session）唯一；同 request_id 重放
                      -> duplicate=true 原结果；同范围键不同 request_id -> rejected
- checkin.query_status 只读；提醒判定用（按时段查）
- checkin.query_records 只读；统计导出用（一次返回当日全部时段记录）

⛔ 本模块不判断业务资格（打卡资格/开关归机器人侧）；period_key/session 对
MCP Server 是不透明字符串（§4.11.0）。
"""

from __future__ import annotations

from typing import Optional

TOOL_SUBMIT = "checkin.submit"


def handle_submit(writer, *, user_id: int, group_id: str, period_key: str,
                  session: str, image_ref: str,
                  image_meta: Optional[dict], request_id: str,
                  status: str = "NORMAL",
                  entry_source: str = "user",
                  checkin_time: Optional[str] = None) -> dict:
    """checkin.submit（§4.11.1）。image_meta: {name, size} 可空。

    status: NORMAL/LATE/ADMIN_EDIT；entry_source: user/admin —— 均由机器人侧
    计算（§4.11.0：MCP Server 只存不校验），此处只落库。
    checkin_time：管理员补录可指定打卡时间（YYYY-MM-DD HH:MM:SS）；
    ⛔ 普通提交不传，服务端记 NOW(3)。
    """
    meta = image_meta or {}

    def biz(cur, dao):
        record_id = dao.insert_checkin(
            cur, user_id, group_id, period_key, session, image_ref,
            meta.get("name"), meta.get("size"), request_id,
            status=status, entry_source=entry_source,
            checkin_time=checkin_time)
        return record_id, {"session": session}

    payload = {
        "user_id": user_id, "group_id": group_id, "period_key": period_key,
        "session": session, "image_ref": image_ref,
        "image_name": meta.get("name"), "image_size": meta.get("size"),
        "status": status, "entry_source": entry_source,
        "request_id": request_id, "checkin_time": checkin_time,
    }
    return writer.execute(
        tool_name=TOOL_SUBMIT, biz_type="checkin", group_id=group_id,
        user_id=user_id, period_key=period_key, request_id=request_id,
        payload=payload, biz_fn=biz)


def handle_query_status(writer, *, user_id: int, group_id: str,
                        period_key: str, session: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            row = writer.dao.query_checkin_status(cur, user_id, group_id,
                                                  period_key, session)
    return row or {"completed": False, "record_id": None, "checkin_time": None}


def handle_query_records(writer, *, group_id: str, period_key: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            records = writer.dao.query_checkin_records(cur, group_id, period_key)
    return {"records": records}


def register(mcp, writer) -> None:
    @mcp.tool(name=TOOL_SUBMIT)
    def checkin_submit(user_id: int, group_id: str, period_key: str, session: str,
                       image_ref: str, image_meta: dict | None = None,
                       request_id: str = "", status: str = "NORMAL",
                       entry_source: str = "user",
                       checkin_time: str | None = None) -> dict:
        """提交一次打卡。session 为 am/pm（机器人侧按服务端时间判定）。
        image_meta 为 {name, size}。status/entry_source 由机器人侧计算。
        checkin_time 仅管理员补录可指定（YYYY-MM-DD HH:MM:SS），普通提交留空。
        同 request_id 重放返回原结果。"""
        return handle_submit(writer, user_id=user_id, group_id=group_id,
                             period_key=period_key, session=session,
                             image_ref=image_ref, image_meta=image_meta,
                             request_id=request_id, status=status,
                             entry_source=entry_source,
                             checkin_time=checkin_time)

    @mcp.tool(name="checkin.query_status")
    def checkin_query_status(user_id: int, group_id: str, period_key: str,
                             session: str) -> dict:
        """查询某用户某群某周期某时段的打卡完成状态（只读）。"""
        return handle_query_status(writer, user_id=user_id, group_id=group_id,
                                   period_key=period_key, session=session)

    @mcp.tool(name="checkin.query_records")
    def checkin_query_records(group_id: str, period_key: str) -> dict:
        """查询某群某周期的全部打卡记录（含各时段，只读，统计导出用）。"""
        return handle_query_records(writer, group_id=group_id,
                                    period_key=period_key)
