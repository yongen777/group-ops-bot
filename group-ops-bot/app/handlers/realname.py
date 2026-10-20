# -*- coding: utf-8 -*-
"""群成员「实名」两轮登记（2026-10-19 用户指令）。

口径（与绑定/总结两轮同一套 pending_session 机制，⛔ 不另造状态机）：
- 第 1 轮：群内精确发送「实名」-> 挂 `WAITING_REAL_NAME_INFO` 会话（10 分钟）
           -> 提示发送「姓名 手机号」
- 第 2 轮：下一条文本按「姓名 + 手机号」解析
           - 「取消」-> 清会话 + 已取消回执
           - 格式不符 -> **保留会话** + 固定纠错话术
           - 通过 -> 写 sys_user（按解析出的手机号 upsert 姓名）
                     + 同步本群名单里的姓名（仅对已存在的名单行）
                     + operation_log 留痕（MCP 侧 user.upsert 内写）
                     + 清会话 + 脱敏回执
- 会话过期 -> 明确超时提示（⛔ 不静默失效）

硬约束：
- 手机号严格 `^1[3-9]\\d{9}$`；姓名 2~20 位汉字或英文字母
- 回执手机号只显示前 3 位 + 后 4 位（⛔ 不回显完整号码）
- ⛔ 不因实名登记自动把人加进「应交人员名单」（名单归管理员维护）；
  只更新已存在行的姓名
"""

from __future__ import annotations

import re
from typing import Optional

from .base import HandlerContext

BIZ = "realname"

# 会话状态名（写进 payload，便于后台/排障一眼看出处于哪一步）
PENDING_STATE = "WAITING_REAL_NAME_INFO"
TTL_MINUTES = 10

CANCEL_TEXT = "取消"

# ⭐ 2026-10-20 用户指令：示例 ⛔ 不再拿真人姓名/号码当模板，统一写「xx 手机号」
_ASK_EXAMPLE = "（例如：xx 手机号）"

# ⭐ 2026-10-20 用户指令：实名引导必须提醒「手机号 = 软件注册手机号」
_PHONE_HINT = "（填报手机号一定和软件注册手机号一致）"

ASK_TEXT = ("请发送您的「姓名 手机号」%s\n"
            "%s\n"
            "%d 分钟内有效；如需放弃请回复：取消"
            % (_ASK_EXAMPLE, _PHONE_HINT, TTL_MINUTES))

FORMAT_ERROR_TEXT = ("输入格式有误。请重新发送「姓名 手机号」%s\n"
                     "%s\n"
                     "或回复：取消" % (_ASK_EXAMPLE, _PHONE_HINT))

# ⭐ 2026-10-20 用户指令：已实名过的人再发「实名」-> 直接拦住（⛔ 不重复登记）
ALREADY_REGISTERED_TEXT = ("您已完成实名登记（姓名：{name}，手机号：{masked}），"
                           "无需重复提交；如需变更请联系后台管理员。")

CANCELED_TEXT = "已取消实名登记。"

EXPIRED_TEXT = ("实名登记等待已超时（%d 分钟无操作）已结束，"
                "请重新发送「实名」。" % TTL_MINUTES)

_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
# 姓名：2~20 位汉字（允许少数民族姓名的间隔号）或纯英文字母
_NAME_RE = re.compile(r"^(?:[\u4e00-\u9fff\u00b7]{2,20}|[A-Za-z]{2,20})$")


def _mask(phone: str) -> str:
    """手机号脱敏：前 3 位 + **** + 后 4 位（⛔ 不回显完整号码）。"""
    phone = phone or ""
    if len(phone) < 7:
        return "***"
    return "%s****%s" % (phone[:3], phone[-4:])


def success_text(name: str, phone: str) -> str:
    """成功回执（手机号脱敏：前 3 位 + **** + 后 4 位）。"""
    return "✅ 您的实名信息已成功录入（姓名：%s，手机号：%s）。" % (
        name, _mask(phone))


def _registered_user(ctx: HandlerContext) -> Optional[dict]:
    """人员目录里已有姓名 = 已实名过（实名登记写的就是 sys_user.name）。

    ⚠️ 后端不可用时返回 None（⛔ 不拦）：宁可让用户重走一遍登记，
    也不把正常登记路径堵死。
    """
    try:
        user = ctx.backend.user_get(phone=ctx.phone).get("user")
    except Exception:  # 后端不可达：不据此拦截
        return None
    if not user:
        return None
    if not (user.get("name") or "").strip():
        return None
    return user


def parse_realname(text: str) -> Optional[tuple]:
    """「姓名 手机号」-> (name, phone)；不合法返回 None。

    姓名与手机号之间允许 1 个或多个普通空格/制表符（⛔ 不允许多余字段）。
    """
    parts = re.split(r"[ \t\u3000]+", (text or "").strip())
    if len(parts) != 2:
        return None
    name, phone = parts[0], parts[1]
    if not _NAME_RE.match(name) or not _PHONE_RE.match(phone):
        return None
    return name, phone


def _payload() -> dict:
    return {"state": PENDING_STATE}


def has_pending(ctx: HandlerContext) -> bool:
    """是否存在活跃会话 **或** 刚过期未消费的会话。

    过期行也返回 True：让 ``handle_pending_text`` 能给出明确超时提示
    （⛔ 不静默掉进帮助文案）。消费一次即删，下一条消息正常走业务。
    """
    if ctx.state.get_pending_session(
            ctx.group_id, ctx.phone, BIZ, ctx.now) is not None:
        return True
    # ⚠️ 这里只能用只读 peek：⛔ 不能用 get_expired_pending_session
    # （它命中即删，路由层一探就把超时话术的「证据」吃掉）
    return ctx.state.peek_pending_session(
        ctx.group_id, ctx.phone, BIZ) is not None


def handle_command(parsed, ctx: HandlerContext) -> str:
    """第 1 轮：挂 10 分钟会话 + 引导发送「姓名 手机号」。

    ⭐ 2026-10-20 用户指令：已实名过的人再发「实名」-> 直接拦住并回显脱敏
    信息（⛔ 不重复登记、⛔ 不重复写 upsert / 审计）。
    """
    existing = _registered_user(ctx)
    if existing is not None:
        return ALREADY_REGISTERED_TEXT.format(
            name=existing["name"].strip(),
            masked=_mask(existing.get("phone") or ctx.phone))
    ctx.state.put_pending_session(
        ctx.group_id, ctx.phone, BIZ, None, ctx.now,
        ttl_minutes=TTL_MINUTES, payload=_payload())
    return ASK_TEXT


def handle_pending_text(ctx: HandlerContext) -> Optional[str]:
    """第 2 轮：解析并入库。无活跃会话返回 None（交回主流程）。"""
    row = ctx.state.get_pending_session(ctx.group_id, ctx.phone, BIZ, ctx.now)
    if row is None:
        # 过期与「从未存在」区分开，给明确超时话术
        expired = ctx.state.get_expired_pending_session(
            ctx.group_id, ctx.phone, BIZ, ctx.now)
        if expired is not None:
            ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
            return EXPIRED_TEXT
        return None

    text = (ctx.text or "").strip()
    if text == CANCEL_TEXT:
        ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
        return CANCELED_TEXT

    parsed = parse_realname(text)
    if parsed is None:
        # ⛔ 保留会话：用户可直接重发，不必再发一次「实名」
        return FORMAT_ERROR_TEXT
    name, phone = parsed

    # 写人员目录（按解析出的手机号 upsert；platform_user_id 一并绑定）
    ctx.backend.user_upsert(phone=phone, name=name,
                            platform_user_id=ctx.platform_user_id,
                            source="manual", group_id=ctx.group_id)

    # 名单里已有该成员时同步姓名（⛔ 不新建名单行：应交人员归管理员维护）
    member = ctx.state.get_member_task(ctx.group_id, ctx.phone)
    if member is not None:
        ctx.state.upsert_member_task(
            ctx.group_id, ctx.phone,
            nickname=name,
            role=member["role"],
            enabled=bool(member["enabled"]),
            source=member["source"] or "manual")

    # 解析出的手机号与平台身份不一致：如实留痕（⛔ 不拦，业务记录仍按平台身份）
    if phone != ctx.phone:
        ctx.state.record_security_event(
            "realname_phone_mismatch", ctx.group_id, ctx.phone,
            "registered phone differs from platform identity")

    ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
    return success_text(name, phone)
