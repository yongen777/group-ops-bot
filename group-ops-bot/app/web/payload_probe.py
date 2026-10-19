# -*- coding: utf-8 -*-
""":12334 入站载荷观测探针（⚠️ 临时模块：协议对齐后应整模块删除）。

⚠️ **只观测不拦截**：本模块⛔ 不改变任何业务判定、⛔ 不改变响应码、
⛔ 不吞异常。唯一职责是把真实平台载荷的**字段结构**落日志 + 落
`security_event.detail`，供 Q3（入站字段名）对齐使用。

⛔ 为什么只记结构、绝不记值：载荷含手机号 / 消息正文 / 可能的长效令牌，
   按§4.7「凭据不进日志、不进审计 detail」纪律，值一律不落盘。
   （字符串仅记长度——判断该字段是不是消息正文足够用，且泄露面极小。）

⛔ 为什么探针必须**先于**动作上线：平台回调是一次性的、**不可回溯**。
   没装探针就发消息，那一次载荷永久丢失，只能让用户重发；而且一旦开了
   「先放行再观察」，就等于在未知协议上放行，与 fail-closed 冲突。

⛔ 退役条件：`deploy/qmx-callback-protocol.md` §5 Q3 字段名确认完毕、
   字段映射落在适配层之后，本文件与 `web/server.py` 中的两处调用一并删除。
"""

from __future__ import annotations

from typing import Any

MAX_DEPTH = 4          # 嵌套上限：够看清平台信封结构，又⛔ 不无限递归
MAX_LEN = 1200         # 落库/落日志的总长上限，防超长结构撑爆一行


def _type_of(value: Any) -> str:
    """值 → 类型标记（⛔ 不返回任何值内容）。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "str(len=%d)" % len(value)
    return type(value).__name__


def describe(payload: Any, depth: int = 0) -> str:
    """把任意 JSON 结构压成一行「字段名:类型」描述。

    例：`groupId:str(len=12),sender:{phone:str(len=11)},items:[2]{name:str}`
    ⛔ 字段名本身不是隐私（业务需要它做映射），⛔ 值内容一律不出现在结果里。
    """
    if isinstance(payload, dict):
        parts = []
        for key, value in payload.items():
            item = "%s:%s" % (key, _type_of(value))
            if isinstance(value, (dict, list)) and depth < MAX_DEPTH:
                item += "{" + describe(value, depth + 1) + "}"
            parts.append(item)
        return ",".join(parts)
    if isinstance(payload, list):
        if not payload:
            return "[]empty"
        head = payload[0]
        if isinstance(head, (dict, list)) and depth < MAX_DEPTH:
            return "[%d]{%s}" % (len(payload), describe(head, depth + 1))
        return "[%d] of %s" % (len(payload), _type_of(head))
    return _type_of(payload)


def shape_line(payload: Any) -> str:
    """对外统一入口：截断到 MAX_LEN，保证一行可读且⛔ 不炸日志。"""
    try:
        text = "payload_shape: " + describe(payload)
    except Exception as exc:                # 探针本身⛔ 绝不影响主流程
        return "payload_shape: <unavailable: %s>" % type(exc).__name__
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN] + "...(truncated)"
    return text