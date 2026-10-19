# -*- coding: utf-8 -*-
"""业务处理器基座（§3.2 通用骨架第 5 步之后 / §2.5 状态机落地）。

本模块提供五项业务处理器共用的：
- HandlerContext：一次入站消息的全部上下文（⛔ 不含令牌明文）
- A/B/C 结果归类（§2.5.1）：success->A / rejected->B / unknown->C
- run_submission：落 intent -> 调 MCP -> 按结果落回执/恢复任务的标准管线
  （§4.11.3 机器人侧对应物；C 类 ⛔ 不自动重试、不新建请求，§2.5.4）；
  intent 落盘时带绑定令牌 id，供 C 类恢复判 A 后补消费（§2.4 / app/recovery.py）

实现级口径说明（需用户知晓，记入 §8.9）：
- 总结（summary）允许同周期覆盖（§2.5.3），因此 run_submission 的
  allow_overwrite=True 路径不做「已完成」预判，且每次保存换新 request_id
  （summary.save 覆盖语义的硬约束，见 mcp_server/tools/summary.py 头注）
- 打卡 / 报数 allow_overwrite=False：本地范围键预判 + MCP 侧范围键唯一约束
  双保险，B 可修正重试（新请求键），C 挂起期间拒绝同范围新独立提交
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from .. import period as period_mod
from .. import tokens as tokens_mod
from ..backend.base import Backend, BackendError
from ..config import BotConfig
from ..state import StateStore

KIND_A = "A"   # 后端确认成功（占额度）
KIND_B = "B"   # 明确拒绝（不占额度，可修正重试）
KIND_C = "C"   # 结果未知（待核验 + 恢复，⛔ 不自动重试）

# 机器人在量子密信平台的名称（2026-10-16 用户实测截图：@general-bot）。
# ⚠️ 平台载荷不含机器人名（adapter 只映射 groupId/phone/textMsg/callBackUrl/
# userName），故按实测写为常量；更名只改这一处，⛔ 各处不得硬编码。
# 提醒文案的「快速处理」与总结的「确认覆盖」共用本常量。
BOT_MENTION_NAME = "general-bot"

# C 类恢复任务延迟（分钟）：给后端事务留收敛时间再查证
RECOVERY_DELAY_MINUTES = 2

ALREADY_DONE_TEXT = {
    "checkin": "本时段已完成打卡，不能重复提交",
    # 用户指令验收项 3：报数每日一次，当日已提交直接提醒、⛔ 不开放重复提交
    # 用户指令 2026-10-19：重复报数话术带出口（找管理员改），⛔ 不说死"不可重复"
    "report": "今日已提交报数，不可重复提交；如需调整请联系后台管理员修改。",
}

C_UNKNOWN_TEXT = ("提交结果未知，系统将自动核对后确认。"
                  "请勿重复提交，稍后可重新发送命令查询状态")

# 暂停考核（名单 enabled=0）的固定回执（2026-10-19 用户口径）：
# 暂停考核 = 该成员在群内**不能执行业务操作**，而不只是「不再提醒」。
# ⛔ 一处真源：路由层与处理器都用这一句，不各写一套。
PAUSED_MEMBER_TEXT = ("您已被暂停业务考核，暂不能在群内执行「打卡」「总结」"
                      "「报数」操作；如需恢复请联系群管理员。")


@dataclass
class HandlerContext:
    """一次入站消息的上下文（由 Web 层构造，⛔ 不跨消息复用）。"""

    group_id: str
    phone: str
    name: Optional[str]
    platform_user_id: Optional[str]
    text: str
    state: StateStore
    backend: Backend
    config: BotConfig
    now: datetime
    reply_url: str = ""
    raw: dict = field(default_factory=dict)
    # 平台提供的**真实群名**（2026-10-15 实测载荷字段 `groupName`）。
    # ⛔ 不再用发送者昵称冒充群名——否则绑定后后台显示的是管理员名字。
    # None = 平台未提供（回落：用户指定名 -> 发送者昵称 -> 群标识）
    group_name: Optional[str] = None
    # 平台出站适配器（阶段八）：提供 send_file / push_text；
    # None = 未注入（如旧测试），处理器须走 fail-closed 兜底
    sender: object = None


@dataclass
class SubmitResult:
    """一次提交的最终归类（§2.5.1）。"""

    kind: str                       # A / B / C
    message: str                    # 回复用户的话（⛔ 不暴露内部细节）
    request_id: Optional[str] = None
    record_id: Optional[str] = None
    duplicate: bool = False         # 同 request_id 重放被识别


def classify(result: dict) -> str:
    """MCP 工具返回 -> A/B/C。未知形态一律 C（保守，§2.5.1）。"""
    status = (result or {}).get("status")
    if status == "success":
        return KIND_A
    if status == "rejected":
        return KIND_B
    return KIND_C


def scope_key(group_id: str, phone: str, biz: str, period_key: str,
              session: Optional[str] = None) -> str:
    """业务记录范围键（§2.5.0）：群+用户+业务+周期（+时段）。"""
    parts = [group_id, phone, biz, period_key]
    if session:
        parts.append(session)
    return "|".join(parts)


def new_request_id() -> str:
    """请求幂等键（§2.5.0）：每次独立提交 / 每次覆盖都换新值。"""
    return uuid.uuid4().hex


def biz_enabled(ctx: HandlerContext, biz: str) -> bool:
    """群业务开关（§2.11 / §2.12）。缺配置视为未开启（fail-closed）。"""
    row = ctx.state.get_group_config(ctx.group_id)
    if row is None:
        return False
    try:
        switches = __import__("json").loads(row["biz_switches"] or "{}")
    except ValueError:
        return False
    return bool(switches.get(biz))


def group_period_type(ctx: HandlerContext) -> str:
    """总结周期类型（daily/weekly），缺省 daily。"""
    row = ctx.state.get_group_config(ctx.group_id)
    if row is None:
        return period_mod.PERIOD_DAILY
    value = row["period_type"] or period_mod.PERIOD_DAILY
    return value if value in (period_mod.PERIOD_DAILY,
                              period_mod.PERIOD_WEEKLY) else period_mod.PERIOD_DAILY


def ensure_user_id(ctx: HandlerContext) -> int:
    """提交前确保人员目录有该用户（user.upsert 幂等，§4.11.1）。"""
    result = ctx.backend.user_upsert(
        phone=ctx.phone, name=ctx.name,
        platform_user_id=ctx.platform_user_id, source="learned")
    return int(result["user_id"])


def _arm_recovery(ctx: HandlerContext, request_id: str, scope: str,
                  biz: str) -> None:
    due = ctx.now + timedelta(minutes=RECOVERY_DELAY_MINUTES)
    ctx.state.put_recovery_task(request_id, scope, biz, due)


def run_submission(ctx: HandlerContext, *, biz: str, call: Callable[[str], dict],
                   period_key: str, session: Optional[str] = None,
                   payload: Optional[dict] = None,
                   allow_overwrite: bool = False,
                   consume_token_id: Optional[str] = None) -> SubmitResult:
    """标准提交管线（§4.11.3 机器人侧对应物）。

    顺序：本地额度预判（非覆盖业务）-> 落 intent -> 调 MCP -> 按结果落账。
    ⛔ 适配器层不重试；本函数对 BackendError 也⛔ 不重试（§2.5.4）。

    令牌状态机（consume_token_id 传入时，用户指令 P1 验收项 5）：
    active --提交发起--> pending_verify --A--> consumed
                                      --B--> active（明确失败，可修正重试）
                                      --C--> pending_verify（保持，等恢复结论）
    """
    phone = ctx.phone
    scope = scope_key(ctx.group_id, phone, biz, period_key, session)

    if not allow_overwrite and ctx.state.has_successful_scope(scope):
        return SubmitResult(KIND_B, ALREADY_DONE_TEXT.get(biz, "本期已完成"))
    # C 类挂起期间 ⛔ 禁止同范围再发起独立提交（pending/unknown 都挡）——
    # 否则未知结论的窗口内同一操作可能落两条记录。
    if not allow_overwrite and ctx.state.has_pending_scope(scope):
        return SubmitResult(KIND_C, C_UNKNOWN_TEXT)

    request_id = new_request_id()
    ctx.state.put_intent(request_id, scope, biz, ctx.group_id, phone,
                         period_key, session, payload or {},
                         token_id=consume_token_id)

    if consume_token_id and not ctx.state.mark_token_submitting(consume_token_id):
        # 令牌已不在 active（并发点击 / 已消费）：⛔ 不再发起第二次独立提交
        return SubmitResult(KIND_B, "该链接状态已变化，请刷新页面后重试")

    try:
        result = call(request_id)
    except BackendError:
        # 结果未知：⛔ 不当成功也不当失败；挂起期间拒绝同范围新独立提交
        ctx.state.update_intent_status(request_id, "unknown")
        _arm_recovery(ctx, request_id, scope, biz)
        return SubmitResult(KIND_C, C_UNKNOWN_TEXT, request_id=request_id)

    kind = classify(result)
    if kind == KIND_A:
        ctx.state.update_intent_status(request_id, "confirmed")
        record_id = result.get("record_id")
        ctx.state.put_receipt(request_id, scope, biz,
                              "" if record_id is None else str(record_id))
        if consume_token_id:
            ctx.state.consume_token(consume_token_id, request_id)
        message = result.get("message") or "提交成功"
        if result.get("duplicate"):
            message = "本期已提交成功（重复提交已识别）"
        return SubmitResult(KIND_A, message, request_id=request_id,
                            record_id=record_id,
                            duplicate=bool(result.get("duplicate")))
    if kind == KIND_B:
        ctx.state.update_intent_status(request_id, "rejected")
        if consume_token_id:
            # 明确失败：令牌回 active，用户可修正后重试（⛔ 不吞掉重试机会）
            ctx.state.reactivate_token(consume_token_id)
        message = result.get("message") or "提交被拒绝，请检查后重试"
        return SubmitResult(KIND_B, message, request_id=request_id)

    ctx.state.update_intent_status(request_id, "unknown")
    _arm_recovery(ctx, request_id, scope, biz)
    return SubmitResult(KIND_C, C_UNKNOWN_TEXT, request_id=request_id)


def token_record_from_row(row):
    """SQLite tokens 行 -> tokens.TokenRecord。"""
    if row is None:
        return None
    return tokens_mod.TokenRecord(
        token_id=row["token_id"], phone=row["phone"],
        group_id=row["group_id"], biz=row["biz"],
        period_key=row["period_key"], session=row["session"],
        created_at=datetime.fromisoformat(row["created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        state=row["state"],
        consumed_request_id=row["consumed_request_id"])
