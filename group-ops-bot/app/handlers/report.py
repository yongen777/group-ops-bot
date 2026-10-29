# -*- coding: utf-8 -*-
"""报数业务处理器（§2.12 第 4 项字段池 / §4.4 / 2026-10-13 字段池化修订 /
2026-10-16 去角色化重构：用户指令「去除角色区隔 + 全局字段共享」）。

流程：命令 -> 本地额度预判 -> 签发令牌 -> H5 **动态逐行添加**要报的字段
（下拉选择字段池 + 填数量，可多行增删）-> submit -> A/B/C；A 后消费令牌。

2026-10-16 去角色化（⛔ 不再问用户要角色、⛔ 不按角色过滤字段）：
- H5 页不再有角色输入；submit_from_h5 不再接收客户端 role
- role 由服务端派生：应交名单角色（Q-BIZ2 默认来源）优先，缺失回落
  DEFAULT_ROLE（⛔ 不因角色缺失拒提交，MCP 契约仍要求非空 role）
- 字段池全局共享：list_fields(enabled_only=True) 全部返回，
  ⛔ 不再按 report_field_pool.roles 过滤（该列自此废弃，仅存量保留）
- 管理员后台补录仍由管理员显式填 role（admin_backfill 不变）

服务端强校验（⛔ 不信任 H5 提交内容）：
- items 非空；field_id 必须存在于本群字段池且 enabled
- quantity 必须为**非负整数**（用户指令第四阶段：小数/负数/非数值拒绝）
- 单位权威在字段池：客户端不回传单位 -> 服务端按字段池快照填充；
  客户端显式回传了与字段池不一致的单位 -> 判篡改拒绝
MCP Server 只存不校验（§4.11.0），有效性责任在机器人侧 —— 本模块即落点。

成功回执（用户指令 2026-10-16 四：三业务统一总结格式风格）：
A 类成功且非 duplicate -> 群内 push_text + 真实 @ 提交人，
话术「✅ 您的今日报数已成功提交，辛苦啦！」（与打卡/总结同骨架）；
推送未接通 -> H5 页面仍显示成功话术（best-effort，⛔ 不改 A/B/C 归类）。

提交时间窗口（⭐ 2026-10-19 用户指令：**整天为执行时间，⛔ 不再使用截止时间**）：
- 当日 00:00:00–23:59:59 任意时刻都可发「报数」并提交 H5 表单，
  ⛔ 不再有 19:00 截止阻断（命令签发 / H5 info / H5 submit 三道阻断全部移除）
- 周期键仍是自然日（`daily_key`）：跨过 24:00 即进入新的一天，天然边界，
  因此不需要「截止时刻」来拦人；群配置 `daily_cutoff` 自此**不参与报数判定**
- 令牌有效期改为「当日 24:00 + 24 小时」（⛔ 不再锚在 daily_cutoff 上）
- ✅ 管理员后台补录（entry_source=admin，见 api.py backfill/report）保留
- 报数无覆盖语义（§2.5.3：一周期一次，修正重试是新记录前的重试）：
  同范围已成功 -> 用户路径 ALREADY_DONE、补录路径明确拒绝
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from .. import period as period_mod
from .. import tokens as tokens_mod
from .base import (KIND_A, KIND_B, KIND_C, HandlerContext, SubmitResult,
                   ALREADY_DONE_TEXT, biz_days, biz_enabled,
                   biz_scheduled_today, days_text, ensure_user_id,
                   run_submission, scope_key, token_record_from_row)
from ..tokens import RejectReason

LOG = logging.getLogger(__name__)

BIZ = "report"

NOT_ENABLED_TEXT = "本群未开启报数，请联系管理员"

# ⭐ D49（2026-10-29 用户指令）：每个业务可单独配置执行日（周几）。
# 非执行日发命令 -> 明确告知今天不用做，⛔ 不进业务、⛔ 不挂会话。
def not_scheduled_text(ctx) -> str:
    return ("今天不是本群{label}日（{label}日：{days}），无需{action}；"
            "如需调整请联系后台管理员。").format(
                label="报数", action="报数", days=days_text(biz_days(ctx, BIZ)))
# 报数链接话术（⭐ 2026-10-19 用户指令：与打卡同口径，带有效期提醒）。
# ⚠️ 有效期数字单一真源 = tokens.TOKEN_TTL_MINUTES，⛔ 不另写死 30。
LINK_TEXT = ("请点击链接填写今日报数：{url}\n"
             "（链接 {ttl} 分钟内有效，逾期失效，需重新发送「报数」）")
# 用户指令 2026-10-19：完成话术统一为陈述句（⛔ 不再「您的…辛苦啦」）。
SUBMIT_OK_TEXT = "✅ 今日工作报数已成功完成。"
EMPTY_ITEMS_TEXT = "请至少添加一个要报的字段"
UNKNOWN_FIELD_TEXT = "包含不存在或已停用的字段，请刷新页面后重试"
BAD_QUANTITY_TEXT = "数量必须是不小于 0 的整数，请检查「{name}」"
UNIT_MISMATCH_TEXT = "字段「{name}」的单位应为「{unit}」，请刷新页面后重试"
# 用户指令 2026-10-16 一：去角色化。role 服务端派生，⛔ 不再问用户要；
# 应交名单也没配角色时回落此值（MCP 契约要求非空，⛔ 不因此拒提交）
DEFAULT_ROLE = "成员"
# BAD_ROLE_TEXT 仅留后台补录路径（admin_backfill 显式传 role）
BAD_ROLE_TEXT = "角色不能为空且不超过 32 个字符"
# ⭐ 2026-10-19 用户指令：报数**整天为执行时间，⛔ 不再使用截止时间**
# （旧 PAST_CUTOFF_TEXT / daily_cutoff 阻断已删除，⛔ 不要恢复）
# 令牌有效期锚点 = 当日 24:00（自然日边界，⛔ 不再是 19:00 截止）
DAY_END_CUTOFF = (23, 59)

def _reject_text(reason: RejectReason) -> str:
    """拒绝话术（⛔ 单一真源在 tokens.reject_message，此处只传业务名）。"""
    return tokens_mod.reject_message(reason, BIZ)


def _audit_reject(ctx: HandlerContext, reason: RejectReason) -> None:
    """越权 / 篡改类拒绝留痕（§2.4）。info 与 submit 两条路径都要调。

    ⛔ 令牌明文不写日志；过期属正常业务事件，不当安全事件刷告警。
    """
    event = tokens_mod.REJECT_SECURITY_EVENT.get(reason)
    if not event:
        return
    ctx.state.record_security_event(event, ctx.group_id, ctx.phone,
                                    "biz=%s" % BIZ)

MAX_ROLE_LEN = 32
MAX_ITEMS = 50  # 防御性上限：字段池规模远小于此


def handle_command(parsed, ctx: HandlerContext) -> str:
    """「报数」入口命令：签发 H5 令牌。"""
    if not biz_enabled(ctx, BIZ):
        return NOT_ENABLED_TEXT
    if not biz_scheduled_today(ctx, BIZ):
        return not_scheduled_text(ctx)
    period_k = period_mod.daily_key(ctx.now)
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_k)
    if ctx.state.has_successful_scope(scope):
        return ALREADY_DONE_TEXT[BIZ]
    # ⭐ 2026-10-19：⛔ 不再有截止阻断 —— 当日任意时刻都能签发令牌
    value = tokens_mod.generate_token_value()
    # 令牌过期 = 当日 24:00 + 24h（§2.4 口径：周期边界后再留一天余量）
    expires_at = day_end_dt(ctx, period_k) + timedelta(hours=24)
    record = tokens_mod.new_record(value, ctx.phone, ctx.group_id, BIZ,
                                   period_k, None, ctx.now,
                                   expires_at=expires_at)
    ctx.state.put_token(record)
    url = "%s/h5/report#t=%s" % (ctx.config.external_base_url, value)
    return LINK_TEXT.format(url=url, ttl=tokens_mod.TOKEN_TTL_MINUTES)


def default_role(ctx: HandlerContext) -> Optional[str]:
    """应交名单里的角色（Q-BIZ2 默认来源：后台按群配置；停用人员不再供默认值）。

    2026-10-16 去角色化后仅作**服务端派生**用途（⛔ 不再回给 H5 展示）。
    """
    for row in ctx.state.list_member_tasks(ctx.group_id, enabled_only=True):
        if row["user_id"] == ctx.phone and row["role"]:
            return row["role"]
    return None


def field_pool(ctx: HandlerContext) -> list:
    """本群启用中的字段池（§2.12 第 4 项；权威在机器人侧 SQLite）。

    2026-10-16 去角色化：**全局共享**，全部启用字段对所有上报用户开放，
    ⛔ 不再按 roles 列过滤（该列自此废弃）。
    """
    return [dict(row) for row in ctx.state.list_fields(ctx.group_id,
                                                       enabled_only=True)]


def token_info(ctx: HandlerContext, value: str) -> dict:
    """H5 页面初始化（只本地校验 + 字段池，⛔ 不调 MCP）。"""
    if not value:
        return {"ok": False, "reason": RejectReason.NOT_FOUND.value}
    record = token_record_from_row(ctx.state.get_token(
        tokens_mod.token_id_of(value)))
    period_k = period_mod.daily_key(ctx.now)
    reason = tokens_mod.check_authorization(
        record, value, ctx.phone, ctx.group_id, BIZ, period_k, None, ctx.now)
    if reason is not None and reason != RejectReason.ALREADY_CONSUMED:
        _audit_reject(ctx, reason)
        return {"ok": False, "reason": reason.value,
                "message": _reject_text(reason)}
    # ⭐ 2026-10-19：⛔ 不再有截止阻断（当日任意时刻都能打开表单）
    done = ctx.state.has_successful_scope(
        scope_key(ctx.group_id, ctx.phone, BIZ, period_k))
    return {
        "ok": True, "period_key": period_k, "done": done,
        "fields": [{
            "field_id": f["field_id"], "field_name": f["field_name"],
            "unit": f["unit"],
        } for f in field_pool(ctx)],
    }


# ---------- 周期边界（⛔ 不是提交截止，仅令牌有效期锚点） ----------

def day_end_dt(ctx: HandlerContext, period_k: str):
    """当日周期边界 24:00（TZ 感知）。

    ⭐ 2026-10-19：报数改为整天执行后，令牌有效期锚点从「daily_cutoff」
    改为自然日边界；⛔ 本函数不参与任何提交拦截判定。
    """
    return period_mod.period_end(BIZ, period_k, daily_cutoff=DAY_END_CUTOFF)


# ---------- 管理后台补录（用户指令第四阶段：允许管理员后台补录） ----------

def admin_backfill(ctx: HandlerContext, period_key: str, role: str,
                   items) -> SubmitResult:
    """后台补录报数（ctx.phone = 目标用户）。

    - entry_source=admin；⛔ 不检查截止（补录即补截止后的账）
    - 报数无覆盖语义：同范围已成功 -> B（⛔ 不覆盖，与打卡补录同式）
    - role / items 复用用户路径同一套校验（字段池 + 非负整数 + 单位快照）
    - ⚠️ 调用方（admin API）负责鉴权与审计；本函数只做业务管线
    """
    role = (role or "").strip() if isinstance(role, str) else ""
    if not role or len(role) > MAX_ROLE_LEN:
        return SubmitResult(KIND_B, BAD_ROLE_TEXT)
    error, snapshot = _validate_items(ctx, items)
    if error:
        return SubmitResult(KIND_B, error)
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_key)
    if ctx.state.has_successful_scope(scope):
        return SubmitResult(KIND_B, "该用户本日已有报数记录，不能重复补录")

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.report_submit(
            user_id=user_id, group_id=ctx.group_id, period_key=period_key,
            role=role, items=snapshot, request_id=request_id,
            entry_source="admin")

    return run_submission(
        ctx, biz=BIZ, call=call, period_key=period_key,
        payload={"role": role, "item_count": len(snapshot),
                 "entry": "admin"})


def _validate_items(ctx: HandlerContext, items):
    """校验并归一化 items。

    返回 (error_text, normalized)；error_text 为 None 表示通过。
    normalized 元素：{"field_id", "field_name", "quantity"(int), "unit"}，
    单位按字段池快照填充（⛔ 不信任客户端回传单位）。
    """
    if not isinstance(items, list) or not items:
        return EMPTY_ITEMS_TEXT, None
    if len(items) > MAX_ITEMS:
        return EMPTY_ITEMS_TEXT, None
    pool = {f["field_id"]: f for f in field_pool(ctx)}
    seen = set()
    normalized = []
    for item in items:
        if not isinstance(item, dict):
            return UNKNOWN_FIELD_TEXT, None
        field_id = item.get("field_id")
        try:
            field_id = int(field_id)
        except (TypeError, ValueError):
            return UNKNOWN_FIELD_TEXT, None
        field = pool.get(field_id)
        if field is None or field_id in seen:
            return UNKNOWN_FIELD_TEXT, None
        seen.add(field_id)
        quantity = item.get("quantity")
        # 非负整数（用户指令第四阶段）：bool 是 int 子类须先排除；
        # float 仅接受整数值（3.0 -> 3），小数/NaN/inf 拒绝
        if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
            return BAD_QUANTITY_TEXT.format(name=field["field_name"]), None
        if isinstance(quantity, float):
            if not quantity.is_integer():
                return BAD_QUANTITY_TEXT.format(
                    name=field["field_name"]), None
            quantity = int(quantity)
        if quantity < 0:
            return BAD_QUANTITY_TEXT.format(name=field["field_name"]), None
        # 单位权威在字段池（后台配置）：客户端不回传单位 -> 服务端快照填充；
        # 客户端显式回传了不一致的单位 -> 判篡改，拒绝
        unit = item.get("unit")
        if unit is not None and field["unit"] and unit != field["unit"]:
            return UNIT_MISMATCH_TEXT.format(name=field["field_name"],
                                             unit=field["unit"]), None
        normalized.append({
            "field_id": field["field_id"],
            "field_name": field["field_name"],
            "quantity": quantity,
            "unit": field["unit"],
        })
    return None, normalized


def _notify_group_report(ctx: HandlerContext, text: str) -> bool:
    """报数成功后群内 @ 通知（用户指令 2026-10-16 三/四）。

    与打卡 / 总结成功通知同口径（checkin._notify_group_checkin /
    summary._notify_group_summary）：
    push_text + mention_phones=[ctx.phone] —— 真实 @（⛔ 不靠 content
    文本假装 @，见 platform/qmx.py push_text）。

    best-effort：推送失败照常返回成功话术（H5 页面仍可见），
    ⛔ 不改写已完成的 A/B/C 归类（报数结果以 MCP 回执为准）。
    """
    sender = getattr(ctx, "sender", None)
    push = getattr(sender, "push_text", None) if sender else None
    if push is None:
        LOG.info("report notify skipped: no push port wired")
        return False
    try:
        ok, _reason = push(ctx.group_id, text,
                           mention_phones=[ctx.phone])
    except TypeError:
        # 旧签名 push(group_id, text) 兼容（⛔ 测试替身 / 未升级端口）
        try:
            ok, _reason = push(ctx.group_id, text)
        except Exception as exc:  # 推送端口异常不当已发送
            LOG.warning("report notify push error: %s", exc)
            return False
    except Exception as exc:
        LOG.warning("report notify push error: %s", exc)
        return False
    return bool(ok)


def submit_from_h5(ctx: HandlerContext, value: str, items) -> SubmitResult:
    """H5 提报报数（2026-10-16 去角色化：⛔ 不再接收客户端 role）。"""
    period_k = period_mod.daily_key(ctx.now)
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_k)
    record = token_record_from_row(ctx.state.get_token(
        tokens_mod.token_id_of(value))) if value else None
    reason = tokens_mod.check_authorization(
        record, value, ctx.phone, ctx.group_id, BIZ, period_k, None,
        ctx.now) if value else RejectReason.NOT_FOUND

    if reason == RejectReason.ALREADY_CONSUMED:
        # 安全重试（§2.4）：范围键已成功 -> 归 A 语义；否则 C 待核验
        if ctx.state.has_successful_scope(scope):
            # ⛔ 重复识别不重复 @ 打扰（通知只在首次成功时发一次）
            return SubmitResult(KIND_A, SUBMIT_OK_TEXT, duplicate=True)
        return SubmitResult(KIND_C,
                            "该链接的提交状态未知，系统将自动核对。请勿重复提交")
    if reason is not None:
        _audit_reject(ctx, reason)
        return SubmitResult(KIND_B, _reject_text(reason))

    # ⭐ 2026-10-19：⛔ 不再有 H5 侧截止阻断（当日任意时刻都能提交）

    # 角色服务端派生（⛔ 不信客户端）：应交名单优先，缺失回落 DEFAULT_ROLE
    # （MCP 契约要求非空 role，⛔ 不因角色缺失拒提交）
    role = default_role(ctx) or DEFAULT_ROLE
    error, snapshot = _validate_items(ctx, items)
    if error:
        return SubmitResult(KIND_B, error)

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.report_submit(
            user_id=user_id, group_id=ctx.group_id, period_key=period_k,
            role=role, items=snapshot, request_id=request_id,
            entry_source="user")

    result = run_submission(
        ctx, biz=BIZ, call=call, period_key=period_k,
        payload={"role": role, "item_count": len(snapshot)},
        consume_token_id=record.token_id)
    if result.kind == KIND_A and not result.duplicate:
        _notify_group_report(ctx, SUBMIT_OK_TEXT)
        return SubmitResult(KIND_A, SUBMIT_OK_TEXT,
                            request_id=result.request_id,
                            record_id=result.record_id)
    return result
