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

import logging
import re
from typing import Optional

from .. import commands
from . import report as report_handler
from .base import HandlerContext

LOG = logging.getLogger(__name__)

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

# ⭐ D36：实名填了别人的号（人员目录里已有别人的姓名）—— ⛔ 不覆盖、不合并
PHONE_TAKEN_TEXT = (
    "⚠️ 该手机号已被其他人员登记，无法重复实名。\n"
    "请填写「本人」的手机号重新提交（格式：姓名 手机号），如需申诉请联系管理员。")

EXPIRED_TEXT = ("实名登记等待已超时（%d 分钟无操作）已结束，"
                "请重新发送「实名」。" % TTL_MINUTES)

# ⭐ D33（2026-10-22 用户指令）：群内业务命令的**前置校验** ——
# 「后台是否录入该用户」，用户口径原话：**「这个群成员就是用户实名」**。
# 因此判定真源 = 本群成员名单（`group_member_task_config`），⛔ 不再查
# MySQL `sys_user.name`（那是另一个人工维护的口径，和名单会打架）。
# ⚠️ 默认常开：⛔ 不设群级开关，固定生效。
REQUIRE_REALNAME_TEXT = (
    "⚠️ 系统里还没有您的实名信息，暂时无法使用该功能。\n"
    "请先发送「@机器人 实名」完成登记（姓名 + 手机号），登记成功后即可正常使用。")

# 受该闸门约束的业务命令（导出走后白名单权限、帮助不拦、绑定/实名本身不拦）
GATED_COMMANDS = (commands.CMD_CHECKIN, commands.CMD_SUMMARY,
                  commands.CMD_REPORT)

# ⭐ D34-①B（2026-10-22 用户指令）：被管理员「移出群」的人⛔ 不许被自愈还魂。
REMOVED_FROM_ROSTER_TEXT = (
    "⚠️ 您已被管理员移出本群名单，暂无权限使用该功能。\n"
    "如需恢复请联系管理员在后台重新添加。")


def _auto_heal_into_roster(ctx: HandlerContext) -> Optional[bool]:
    """⭐ D34（2026-10-22 用户指令）：**全局档案静默自愈入册**。

    本群名单没有这个人 -> 去全局档案（MySQL `sys_user`，走 MCP `user.get`）查：
    - 有姓名：**静默写进本群名单**（`source='auto_heal'`）并**无感放行**本次业务；
    - 没有/查不到：返回 False（由调用方拦截并引导实名）；
    - 后端不可用：返回 None（fail-open，⛔ 不因抖动挡业务）。

    动机：管理员误删名单、或跨群已实名的人，此前会被「打卡要实名 / 实名说已完成」
    互锁死。自愈后一条命令就把他补回册，后台「群成员」里立刻可见。
    """
    # ⭐ D34-①B：被管理员移出过的人⛔ 不自愈还魂（管理员重新添加会清掉痕迹）
    try:
        if ctx.state.is_member_removed(ctx.group_id, ctx.phone):
            return False
    except Exception:
        pass
    try:
        user = (ctx.backend.user_get(phone=ctx.phone) or {}).get("user")
    except Exception:
        return None
    if not user:
        return False
    name = (user.get("name") or "").strip()
    if not name:
        return False
    try:
        ctx.state.upsert_member_task(
            ctx.group_id, ctx.phone, nickname=name,
            role=report_handler.DEFAULT_ROLE, enabled=True,
            source="auto_heal")
    except Exception:  # 入册失败⛔ 不拦人：让业务照常，下次再补
        LOG.exception("auto_heal into roster failed: group=%s", ctx.group_id)
        return True
    LOG.info("auto-healed %s(%s) into group %s roster",
             name, ctx.phone, ctx.group_id)
    return True


def registered_state(ctx: HandlerContext) -> Optional[bool]:
    """本群名单里是否已录入该用户；未录入时尝试**全局档案自愈**。

    True  = 在名单 / 已自愈入册（无感放行）
    False = 名单没有、全局档案也没有姓名（拦截并引导实名）
    None  = 查询异常 —— 调用方⛔ 不得据此拦截（fail-open）

    ⚠️ **名单为空 = 该群还没开始治理**：直接放行且⛔ 不入册
    （不给未启用名单的群凭空写一堆成员）。
    """
    try:
        roster = ctx.state.list_member_tasks(ctx.group_id)
    except Exception:
        return None
    if not roster:
        return True
    if any(row["user_id"] == ctx.phone for row in roster):
        return True
    return _auto_heal_into_roster(ctx)


def _phone_conflict(ctx: HandlerContext, phone: str) -> Optional[dict]:
    """该手机号是否已被**别人**实名占用（单人单号口径）。

    返回占用者 dict 表示冲突；None 表示可用：
    - 号码就是自己的平台身份 -> 不冲突（本人补登记）
    - 目录里有该号但姓名为空 -> 不冲突（历史遗留，可补录）
    - 后端不可用 -> None（fail-open，⛔ 不因抖动挡住实名）
    """
    if phone == ctx.phone:
        return None
    try:
        user = (ctx.backend.user_get(phone=phone) or {}).get("user")
    except Exception:
        return None
    if not user:
        return None
    if not (user.get("name") or "").strip():
        return None
    return user


def blocks_business(ctx: HandlerContext, parsed) -> bool:
    """该命令是否要先过实名校验（常开，⛔ 无开关可关）。"""
    return getattr(parsed, "command", None) in GATED_COMMANDS


def deny_text(ctx: HandlerContext) -> Optional[str]:
    """准入失败时的回话；None = 放行。

    「被管理员移出」优先于「未实名」：前者是管理员的明确决定，
    回实名话术会误导他去做无用功。
    """
    if registered_state(ctx) is not False:
        return None
    try:
        if ctx.state.is_member_removed(ctx.group_id, ctx.phone):
            return REMOVED_FROM_ROSTER_TEXT
    except Exception:
        pass
    return REQUIRE_REALNAME_TEXT

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


def _roster_verdict(ctx: HandlerContext) -> Optional[bool]:
    """本群名单里有没有这个人：True=在，False=不在，None=名单为空（未治理）。"""
    try:
        roster = ctx.state.list_member_tasks(ctx.group_id)
    except Exception:
        return None
    if not roster:
        return None
    return any(row["user_id"] == ctx.phone for row in roster)


def _registered_user(ctx: HandlerContext) -> Optional[dict]:
    """已实名判定 = **在本群名单里** 且 **人员目录有姓名**，缺一不可。

    ⚠️ 2026-10-22 实测死循环（用户截图）：郑佳在别的群实名过 -> `sys_user.name`
    有值，但他在**本群**被移除 -> 不在名单。于是「打卡」提示去实名，「实名」又
    回「已完成登记」-> 他既进不了名单、也补不了登记，**永远卡死**。
    故增加前置：明确不在本群名单 -> 视为未实名，放行实名流程（登记成功会自动
    入册，见 handle_pending_text 的成功分支）。

    ⚠️ 后端不可用时返回 None（⛔ 不拦）：宁可让用户重走一遍登记，
    也不把正常登记路径堵死。
    """
    if _roster_verdict(ctx) is False:
        return None
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

    # ⭐ D36（2026-10-22 用户指令）：实名也要防撞号（单人单号）。
    # 若输入的号码在人员目录里**已经有别人的姓名**，直接拒绝并留痕 ——
    # ⛔ 绝不能 upsert，否则会把别人的姓名覆盖掉（比名单撞号更严重）。
    conflict = _phone_conflict(ctx, phone)
    if conflict is not None:
        try:
            ctx.state.add_phone_conflict(
                ctx.group_id, phone, "realname",
                "attempted_by=%s; owner=%s" % (ctx.phone,
                                               conflict.get("name") or "?"))
        except Exception:
            LOG.exception("record phone conflict failed")
        return PHONE_TAKEN_TEXT

    # 写人员目录（按解析出的手机号 upsert；platform_user_id 一并绑定）
    ctx.backend.user_upsert(phone=phone, name=name,
                            platform_user_id=ctx.platform_user_id,
                            source="manual", group_id=ctx.group_id)

    # 名单里已有该成员时同步姓名（已存在行只改名，⛔ 不动 enabled/role）
    member = ctx.state.get_member_task(ctx.group_id, ctx.phone)
    if member is not None:
        ctx.state.upsert_member_task(
            ctx.group_id, ctx.phone,
            nickname=name,
            role=member["role"],
            enabled=bool(member["enabled"]),
            source=member["source"] or "manual")
    else:
        # ⭐ D33 口径变更（2026-10-22 用户指令）：「群成员名单 = 实名花名册」。
        # 实名成功即把人**加入本群名单**（否则「不在名单 -> 提示实名 -> 实名
        # 又不进名单」会死循环，用户永远解不开）。
        ctx.state.upsert_member_task(
            ctx.group_id, ctx.phone, nickname=name,
            role=report_handler.DEFAULT_ROLE, enabled=True,
            source="realname")

    # 解析出的手机号与平台身份不一致：如实留痕（⛔ 不拦，业务记录仍按平台身份）
    if phone != ctx.phone:
        ctx.state.record_security_event(
            "realname_phone_mismatch", ctx.group_id, ctx.phone,
            "registered phone differs from platform identity")
    else:
        # ⭐ D31 修复 5（2026-10-21）：号码与平台身份一致时，把姓名同步进
        # 本地 `learned_user` —— 否则提醒/看板里同一个人的显示名会「实名写
        # 的是真名、@ 时还是旧昵称」两套口径（learned_user 是展示名首选源）。
        try:
            ctx.state.learn_user(ctx.phone, name, ctx.platform_user_id)
        except Exception:  # 展示名同步失败⛔ 不影响实名主流程与回执
            pass

    ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
    return success_text(name, phone)
