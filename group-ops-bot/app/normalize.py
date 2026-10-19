# -*- coding: utf-8 -*-
"""入站载荷归一化（§3.2 第 2 步：平台字段多写法兼容）。

⚠️ 状态标注：量子密信平台真实回调载荷**尚未实测**（§7.2 未执行验证清单）。
本模块按设计写就，字段名以平台实际文档 / 实测为准；实测后如有偏差，
必须修订本模块并记入 §8，⛔ 不得为了让测试通过而假设载荷形态。

归一化目标（§3.2）：
- 群标识 group_id
- 用户手机号 phone（稳定自然键，sys_user.phone 的基础）
- 文本内容 text
- 回推地址 reply_url（出站发送用）

多写法兼容策略：同一语义字段尝试多个候选键名（平台历史版本常见差异），
取第一个非空值；⛔ 不做语义猜测，取不到就如实报缺失。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


class PayloadError(ValueError):
    """载荷缺少必需字段（如实上报，不猜）。"""


# 候选键名表：按可能性排序，取第一个非空
_GROUP_KEYS = ("group_id", "groupId", "group", "room_id", "roomId")
_PHONE_KEYS = ("phone", "mobile", "phone_number", "phoneNumber", "sender_phone")
_TEXT_KEYS = ("text", "content", "message", "msg", "body")
_REPLY_KEYS = ("reply_url", "replyUrl", "callback_url", "callbackUrl",
               "webhook", "push_url")
_USERID_KEYS = ("user_id", "userId", "platform_user_id", "platformUserId",
                "sender_id")
_NAME_KEYS = ("name", "nickname", "nick_name", "display_name", "sender_name")


@dataclass
class InboundContext:
    """归一化后的入站上下文（后续所有模块只认这个结构）。"""

    group_id: str
    phone: str
    text: str
    reply_url: str
    platform_user_id: Optional[str] = None
    name: Optional[str] = None
    raw: dict = None  # type: ignore[assignment]


def _first_nonempty(payload: dict, keys: tuple) -> Optional[str]:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _dig(payload: dict, dotted: str) -> Any:
    """按 a.b.c 取嵌套值（平台可能把字段嵌在 sender/message 等对象里）。"""
    node = payload
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _first_nonempty_nested(payload: dict, dotted_keys: tuple) -> Optional[str]:
    for dotted in dotted_keys:
        value = _dig(payload, dotted)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


# 嵌套候选（平台常见结构：sender.* / message.* / data.*）
_NESTED_CANDIDATES = {
    "group": ("data.group_id", "data.groupId", "message.group_id",
              "message.groupId", "data.group.id", "group.id"),
    "phone": ("data.phone", "data.sender.phone", "message.sender.phone",
              "sender.phone", "data.mobile", "message.mobile"),
    "text": ("data.text", "data.content", "message.text", "message.content",
             "data.message", "message.body"),
    "reply": ("data.reply_url", "data.replyUrl", "message.reply_url",
              "data.callback_url", "message.callbackUrl", "reply_url"),
    "userid": ("data.user_id", "data.userId", "data.sender.user_id",
               "message.sender.user_id", "sender.user_id",
               "data.platform_user_id", "message.platform_user_id"),
    "name": ("data.name", "data.sender.name", "message.sender.name",
             "sender.name", "data.nickname", "message.nickname"),
}


def normalize_payload(payload: dict) -> InboundContext:
    """平台回调 -> InboundContext。

    必需字段：group_id / phone / text / reply_url —— 缺一则 PayloadError，
    ⛔ 不猜不补（缺字段时按 §3.2 记安全事件并结束，由调用方决定）。
    """
    if not isinstance(payload, dict):
        raise PayloadError("payload must be a JSON object")

    group_id = (_first_nonempty(payload, _GROUP_KEYS)
                or _first_nonempty_nested(payload, _NESTED_CANDIDATES["group"]))
    phone = (_first_nonempty(payload, _PHONE_KEYS)
             or _first_nonempty_nested(payload, _NESTED_CANDIDATES["phone"]))
    text = (_first_nonempty(payload, _TEXT_KEYS)
            or _first_nonempty_nested(payload, _NESTED_CANDIDATES["text"]))
    reply_url = (_first_nonempty(payload, _REPLY_KEYS)
                 or _first_nonempty_nested(payload, _NESTED_CANDIDATES["reply"]))

    missing = [name for name, value in (
        ("group_id", group_id), ("phone", phone), ("text", text),
        ("reply_url", reply_url)) if not value]
    if missing:
        raise PayloadError("payload missing required fields: %s" % ",".join(missing))

    return InboundContext(
        group_id=group_id,
        phone=phone,
        text=text,
        reply_url=reply_url,
        platform_user_id=(_first_nonempty(payload, _USERID_KEYS)
                          or _first_nonempty_nested(payload, _NESTED_CANDIDATES["userid"])),
        name=(_first_nonempty(payload, _NAME_KEYS)
              or _first_nonempty_nested(payload, _NESTED_CANDIDATES["name"])),
        raw=payload,
    )
