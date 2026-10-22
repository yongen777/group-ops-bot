# -*- coding: utf-8 -*-
"""user.* / op.* 工具（§4.11.1 契约）。

- user.upsert        按 phone upsert（手机号是稳定自然键）
- user.get / user.list  只读；后台候选池与名单维护
- op.query_by_request C 类恢复查证的唯一入口（§4.11.3 第 6 条）
- op.query_log       MCP 调试与审计
"""

from __future__ import annotations

from typing import Optional


def handle_upsert(writer, *, phone: str, name: Optional[str],
                  platform_user_id: Optional[str], department: Optional[str],
                  source: str, group_id: Optional[str] = None) -> dict:
    """按 phone upsert + operation_log 留痕。

    2026-10-19：群内「实名」登记要求「写入 sys_user 并记录 operation_log」，
    故本工具补一条 op_log（pending -> success 同一事务），group_id 可为空
    （后台/系统调用没有群上下文）。
    """
    import json
    import uuid

    request_id = "user-upsert-%s" % uuid.uuid4().hex
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            before = writer.dao.get_user(cur, phone)
            writer.dao.insert_op_log(
                cur, request_id, "user.upsert", "user", group_id, None, None,
                json.dumps({"phone": phone, "name": name, "source": source},
                           ensure_ascii=False))
            user_id = writer.dao.upsert_user(
                cur, phone, name, platform_user_id, department, source)
            created = before is None
            writer.dao.finish_op_log(
                cur, request_id, "success",
                json.dumps({"user_id": user_id, "created": created}),
                user_id, None, None)
        conn.commit()
    return {"user_id": user_id, "created": created}


def handle_update_phone(writer, *, old_phone: str, new_phone: str,
                        group_id: Optional[str] = None) -> dict:
    """⭐ D34-②B（2026-10-22 用户指令）：改手机号（同步全局档案，防身份分裂）。

    ⛔ 不新建行：直接改 `sys_user.phone`，历史业务记录按 `user_id` 关联，
    改号后仍然指向同一个人；否则用户拿新号打卡会 upsert 出第二条记录，
    月底导出变成「一个人两张账单」。
    """
    import json
    import uuid

    request_id = "user-phone-%s" % uuid.uuid4().hex
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            writer.dao.insert_op_log(
                cur, request_id, "user.update_phone", "user", group_id,
                None, None,
                json.dumps({"old_phone": old_phone, "new_phone": new_phone},
                           ensure_ascii=False))
            try:
                user = writer.dao.update_user_phone(cur, old_phone, new_phone)
            except ValueError as exc:
                writer.dao.finish_op_log(cur, request_id, "failed",
                                         json.dumps({"error": str(exc)},
                                                    ensure_ascii=False),
                                         None, None, None)
                conn.commit()
                raise
            if user is None:
                writer.dao.finish_op_log(cur, request_id, "failed",
                                         '{"error": "old phone not found"}',
                                         None, None, None)
                conn.commit()
                return {"updated": False, "user": None}
            writer.dao.finish_op_log(cur, request_id, "success",
                                     json.dumps({"user": user},
                                                ensure_ascii=False),
                                     user.get("id"), None, None)
        conn.commit()
    return {"updated": True, "user": user}


def handle_get(writer, *, phone: str) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            user = writer.dao.get_user(cur, phone)
    return {"user": user}


def handle_list(writer, *, phone_prefix: Optional[str] = None,
                limit: int = 200) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            users = writer.dao.list_users(cur, phone_prefix, limit)
    return {"users": users}


def handle_query_by_request(writer, *, request_id: str) -> dict:
    """C 类恢复查证（§4.11.1 op.query_by_request）。"""
    return writer.query_by_request(request_id)


def handle_query_log(writer, *, tool_name: Optional[str] = None,
                     group_id: Optional[str] = None,
                     status: Optional[str] = None,
                     since: Optional[str] = None,
                     limit: int = 200) -> dict:
    with writer.pool.connection() as conn:
        with conn.cursor() as cur:
            entries = writer.dao.query_op_log(cur, tool_name, group_id, status,
                                              since, limit)
    return {"entries": entries}


def register(mcp, writer) -> None:
    @mcp.tool(name="user.upsert")
    def user_upsert(phone: str, name: str | None = None,
                    platform_user_id: str | None = None,
                    department: str | None = None,
                    source: str = "learned",
                    group_id: str | None = None) -> dict:
        """按手机号 upsert 人员目录（source: manual/learned；写 operation_log）。"""
        return handle_upsert(writer, phone=phone, name=name,
                             platform_user_id=platform_user_id,
                             department=department, source=source,
                             group_id=group_id)

    @mcp.tool(name="user.get")
    def user_get(phone: str) -> dict:
        """按手机号查人员（只读）。"""
        return handle_get(writer, phone=phone)

    @mcp.tool(name="user.update_phone")
    def user_update_phone(old_phone: str, new_phone: str,
                          group_id: str | None = None) -> dict:
        """改手机号（后台改号同步全局档案；⛔ 不新建行，防身份分裂）。"""
        return handle_update_phone(writer, old_phone=old_phone,
                                   new_phone=new_phone, group_id=group_id)

    @mcp.tool(name="user.list")
    def user_list(phone_prefix: str | None = None, limit: int = 200) -> dict:
        """列人员（可带手机号前缀过滤；只读）。"""
        return handle_list(writer, phone_prefix=phone_prefix, limit=limit)

    @mcp.tool(name="op.query_by_request")
    def op_query_by_request(request_id: str) -> dict:
        """按请求键查最终结论（C 类恢复的唯一入口；只读）。"""
        return handle_query_by_request(writer, request_id=request_id)

    @mcp.tool(name="op.query_log")
    def op_query_log(tool_name: str | None = None, group_id: str | None = None,
                     status: str | None = None, since: str | None = None,
                     limit: int = 200) -> dict:
        """查操作日志（只读；审计与调试）。"""
        return handle_query_log(writer, tool_name=tool_name, group_id=group_id,
                                status=status, since=since, limit=limit)
