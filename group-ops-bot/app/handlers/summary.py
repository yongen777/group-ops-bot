# -*- coding: utf-8 -*-
"""总结业务处理器（§2.2 / §4.3 / 用户指令第三阶段）。

形态（⭐ 2026-10-16 用户指令三.2：⛔ 移除一轮提交）：
- **恒两轮**：「总结」-> pending_session（TTL 30 分钟）-> 下一条纯文本即内容；
  ⛔ 不再支持「总结 正文」同行提交（命令层已判为无关文本）
- 周期：**群配置** daily/weekly（日报=当日 / 周报=本周），⛔ 命令不带参数、
  ⛔ 不猜周期（日报与周报同为总结内容，只是数据量不同）
- 额度：同周期允许覆盖（§2.5.3）-> 每次保存换新 request_id

覆盖确认流（用户指令第三阶段，⛔ 禁止直接覆盖）：
  已有记录 -> 新内容存 pending_summary（TTL **10 分钟**）
  -> 提示「您本周期内已提交过总结，如需覆盖更新，请回复：@机器人 确认覆盖」
  -> 「确认覆盖」= 覆盖（新 request_id）；「取消」= 保留原记录；
     其他文本 = 重新提示（⛔ 不当新内容提交）；超时 = 失效
  （取消 / 超时自动取消均已实现并由 test_summary_phase3.py 覆盖，
  2026-10-16 用户指令四复核通过。）

焦点过期（⭐ 2026-10-17 用户指令六：两轮等待不是无限期的）：
- 两轮输入焦点 TTL = ``config.pending_ttl_minutes``（默认 30 分钟，§2.2；
  本地演示可经 GOPS_PENDING_SESSION_TTL_MINUTES 调小）
- 过期后下一条文本 -> ``expired_prompt`` 明确提示重新发送「总结」
  （⛔ 不静默落入帮助文案）；覆盖确认过期 -> OVERWRITE_EXPIRED_TEXT
- 等待输入期间可发「取消」主动放弃（SUMMARY_CANCELED_TEXT，⛔ 不占满 TTL）

成功话术投递（用户指令 2026-10-16 四：上报 / 覆盖回复要 @ 提交人）：
  保存 / 覆盖成功 -> 优先 push_text + mention_phones（真实 @，与打卡成功
  通知同口径，见 checkin._notify_group_checkin）；推送未接通 -> 回落
  普通回复（⛔ 不让用户干等）；已推送 -> 返空串（router 约定空串 = 不
  回复，⛔ 避免同一条话术在群里出现两次）。

提交时间窗口（⭐ 2026-10-19 用户指令：**整天为执行时间，⛔ 不再使用截止时间**）：
- 日报：当日 00:00:00–23:59:59 任意时刻都可提交（⛔ 不再有 19:00 截止阻断）
- 周报：该 ISO 周任意时刻可提交（周期键仍按周日 24:00 翻页，⛔ 不是截止）
- 说明：周期键本身（daily/weekly）就是天然边界——跨过自然日/自然周即进入
  新周期，因此无需再用「截止时刻」拦人；群配置的 `daily_cutoff` 自此
  **不再参与总结判定**（字段保留只为存量兼容，⛔ 请勿再当成提交截止）
- ✅ 管理员后台补录（entry_source=admin，见 api.py backfill/summary）保留：
  它的用途是「补历史周期的账」，不是绕过截止

⛔ 总结不经 H5 / 令牌（§4.10：H5 只覆盖打卡 / 报数）。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from .. import commands, period as period_mod
from .base import (KIND_A, KIND_B, KIND_C, BOT_MENTION_NAME, HandlerContext,
                   SubmitResult, biz_days, biz_enabled, biz_scheduled_today,
                   days_text, ensure_user_id, group_period_type,
                   run_submission, scope_key)

LOG = logging.getLogger("group-ops.summary")

BIZ = "summary"

_ARG_TO_PERIOD = {"日报": period_mod.PERIOD_DAILY, "周报": period_mod.PERIOD_WEEKLY}
_PERIOD_TO_ARG = {v: k for k, v in _ARG_TO_PERIOD.items()}

NOT_ENABLED_TEXT = "本群未开启总结，请联系管理员"

# ⭐ D49（2026-10-29 用户指令）：每个业务可单独配置执行日（周几）。
# 非执行日发命令 -> 明确告知今天不用做，⛔ 不进业务、⛔ 不挂会话。
def not_scheduled_text(ctx) -> str:
    return ("今天不是本群{label}日（{label}日：{days}），无需{action}；"
            "如需调整请联系后台管理员。").format(
                label="总结", action="提交总结", days=days_text(biz_days(ctx, BIZ)))
EMPTY_CONTENT_TEXT = "总结内容不能为空"

# ⭐ D47（2026-10-26 用户反馈：群里有人发「asdfghjkl」被当成工作总结入库，
# 后台显示「正常完成」，统计里多出一条假记录）：明显不是总结的内容
# ⛔ 不落库、⛔ 不消耗会话（用户可直接重发）。
#
# ⚠️ 判据刻意**不按长度**：真实总结可以很短（「第一版」「今天完成了X」），
# 按下限会误伤 —— 首版试过 10 字下限，直接打挂 25 条既有用例。
# 只拦「一个中文都没有、又不成句」的输入：
#   - 含中文            -> 放行（多短都放行）
#   - 无中文但 ≥3 个词   -> 当英文总结放行（"Fixed login bug"）
#   - 无中文但 ≥20 字符  -> 放行（长英文段落）
#   - 其余（asdfghjkl / qwer / 12345）-> 拦
MIN_EN_WORDS = 3
MIN_EN_CHARS = 20
MEANINGLESS_CONTENT_TEXT = ("⚠️ 这看起来不是工作总结内容，请发送具体的今日工作内容；"
                            "如需放弃请回复：取消")


def is_meaningless_content(text: str) -> bool:
    """是否「明显不是总结」的输入（判据见上）。空内容不在此列。"""
    s = (text or "").strip()
    if not s:
        return False
    if re.search(r"[\u4e00-\u9fff]", s):
        return False
    words = [w for w in re.split(r"\s+", s) if w]
    return len(words) < MIN_EN_WORDS and len(s) < MIN_EN_CHARS


def two_round_prompt(ttl_minutes: int) -> str:
    """两轮第 1 轮提示（⭐ 2026-10-17 用户指令六：TTL 与「取消」出口写进话术）。"""
    return ("请发送您的工作总结内容（发送后将自动归档为今日日报；"
            "%d 分钟内有效，发送「取消」可放弃）" % ttl_minutes)


def expired_prompt(ttl_minutes: int) -> str:
    """两轮输入焦点过期话术（旧为死常量，2026-10-17 用户指令六接线）。"""
    return ("上一轮总结输入已过期（超过 %d 分钟无操作），"
            "请重新发送「总结」" % ttl_minutes)


# ⭐ 2026-10-17 用户指令六：等待输入期间可主动放弃（与绑定两轮同口径）
SUMMARY_CANCELED_TEXT = "已取消本次总结提交，如需提交请重新发送「总结」。"

# 机器人在量子密信平台的名称统一由 base.BOT_MENTION_NAME 提供（⛔ 不再本地定义，
# 否则提醒文案与覆盖提示会各写一个，改名时必然漏掉一处）。

# 成功 / 覆盖话术（⭐ 2026-10-19 用户指令：统一点名「今日 / 本周工作总结」，
# ⛔ 不再用「您的日报已成功保存，辛苦啦」这类各写一套的句式）
_PERIOD_WORD = {period_mod.PERIOD_DAILY: "今日",
                period_mod.PERIOD_WEEKLY: "本周"}


def saved_text(ptype: str) -> str:
    """总结保存成功（用户指令 2026-10-19：✅ 今日工作总结已成功完成。）。"""
    return "✅ %s工作总结已成功完成。" % _PERIOD_WORD.get(ptype, "今日")


def overwrite_done_text(ptype: str) -> str:
    return "✅ %s工作总结已成功覆盖更新。" % _PERIOD_WORD.get(ptype, "今日")


def overwrite_prompt(ptype: str) -> str:
    """重复提交提示：日报说「今天」、周报说「本周期」（用户指令 2026-10-19）。

    ⛔ 两种周期的措辞不得混用：日报写「本周期」会被用户当成周报。
    """
    when = "今天" if ptype == period_mod.PERIOD_DAILY else "本周期"
    return ("⚠️ 您在%s内已提交过总结。如需覆盖更新，"
            "请回复：@%s 确认覆盖" % (when, BOT_MENTION_NAME))


# 覆盖确认流（用户指令第三阶段）
OVERWRITE_CONFIRM_TTL_MINUTES = 10
CONFIRM_OVERWRITE_TEXT = "确认覆盖"
CANCEL_TEXT = "取消"
OVERWRITE_CANCELED_TEXT = "已保留原有总结，未做修改"
OVERWRITE_EXPIRED_TEXT = "确认已超时失效，请重新发送「总结」提交新内容"


def overwrite_reprompt(ptype: str = "") -> str:
    when = "今天" if ptype == period_mod.PERIOD_DAILY else "本周期"
    return ("请回复「确认覆盖」以覆盖%s总结，"
            "回复「取消」保留原有总结" % when)

# ⭐ 2026-10-19 用户指令：总结**整天为执行时间，⛔ 不再使用截止时间**。
# 旧的「超过截止 -> 已截至，请找管理员补交」分支已删除：日期 / 周键本身就是
# 周期边界，跨过去即进入新周期，无需再拦人。⛔ 不要恢复该阻断。


def handle_command(parsed, ctx: HandlerContext) -> str:
    """「总结」入口命令（⭐ 恒走两轮，2026-10-16 用户指令三.2）。

    ⛔ **移除一轮直接提交**：不再接受「总结 + 正文」同行提交（命令层已把
    此类文本判为无关文本 -> 帮助文案），本命令唯一效果 = 挂待输入会话 +
    提示。周期由**群配置**决定（日报 = 当日 / 周报 = 本周），⛔ 不接受命令行
    参数、⛔ 不猜周期（日报与周报同为总结内容，只是数据量不同）。
    """
    if not biz_enabled(ctx, BIZ):
        return NOT_ENABLED_TEXT
    if not biz_scheduled_today(ctx, BIZ):
        return not_scheduled_text(ctx)
    ctx.state.put_pending_session(ctx.group_id, ctx.phone, BIZ, None,
                                  ctx.now,
                                  ttl_minutes=ctx.config.pending_ttl_minutes)
    return two_round_prompt(ctx.config.pending_ttl_minutes)


def handle_pending_text(ctx: HandlerContext) -> Optional[str]:
    """两轮第二轮 / 覆盖确认的文本处理。无待处理返回 None。"""
    row = ctx.state.get_pending_session(ctx.group_id, ctx.phone, BIZ, ctx.now)
    if row is None:
        text = (ctx.text or "").strip()
        # ⭐ 2026-10-17 用户指令六：焦点过期**明确告知**（区分「输入超时」
        # 与「覆盖确认超时」两套话术），⛔ 不静默落入帮助文案
        expired = ctx.state.get_expired_pending_session(
            ctx.group_id, ctx.phone, BIZ, ctx.now)
        if expired is not None:
            payload = _payload_of(expired)
            if payload and payload.get("mode") == "overwrite":
                return OVERWRITE_EXPIRED_TEXT
            return expired_prompt(ctx.config.pending_ttl_minutes)
        if text in (CONFIRM_OVERWRITE_TEXT, CANCEL_TEXT):
            return OVERWRITE_EXPIRED_TEXT
        return None
    payload = _payload_of(row)
    text = (ctx.text or "").strip()
    if payload and payload.get("mode") == "overwrite":
        # ---- 覆盖确认流 ----
        if text == CONFIRM_OVERWRITE_TEXT:
            ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
            return _do_overwrite(ctx, payload["period_type"],
                                 payload.get("content") or "")
        if text == CANCEL_TEXT:
            ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
            return OVERWRITE_CANCELED_TEXT
        # 其他文本：⛔ 不当新总结内容（防误覆盖），重新提示
        return overwrite_reprompt(payload.get("period_type") or "")
    # ---- 两轮内容提交 ----
    if text == CANCEL_TEXT:
        # ⭐ 2026-10-17 用户指令六：等待输入期间可主动放弃（不占满 TTL）
        ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
        return SUMMARY_CANCELED_TEXT
    # ⭐ D47：明显不是总结的内容⛔ 不落库、⛔ 不消耗会话（保留会话让用户重发）
    if is_meaningless_content(text):
        return MEANINGLESS_CONTENT_TEXT
    ctx.state.drop_pending_session(ctx.group_id, ctx.phone, BIZ)
    if not text:
        return EMPTY_CONTENT_TEXT
    arg = row["arg"]
    ptype = _ARG_TO_PERIOD.get(arg) if arg else None
    if ptype is None:
        ptype = group_period_type(ctx)
    return _submit_and_reply(ctx, ptype, text)


def _payload_of(row) -> Optional[dict]:
    import json
    raw = row["payload_json"] if "payload_json" in row.keys() else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _submit_and_reply(ctx: HandlerContext, ptype: str, content: str) -> str:
    """用户路径提交：已有记录则走覆盖确认流 -> 否则直接保存。

    ⭐ 2026-10-19：⛔ 不再检查截止 —— 整天（日报）/ 整周（周报）任意时刻可提交。
    """
    period_k = period_mod.period_key(ptype, ctx.now)
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_k)
    if ctx.state.has_successful_scope(scope):
        # ⛔ 禁止直接覆盖：挂 10 分钟确认会话，等用户明确意图
        ctx.state.put_pending_session(
            ctx.group_id, ctx.phone, BIZ, None, ctx.now,
            ttl_minutes=OVERWRITE_CONFIRM_TTL_MINUTES,
            payload={"mode": "overwrite", "period_type": ptype,
                     "content": content})
        return overwrite_prompt(ptype)
    ok, message = _save(ctx, ptype, content)
    if not ok:
        return message
    return _deliver(ctx, saved_text(ptype))


def _do_overwrite(ctx: HandlerContext, ptype: str, content: str) -> str:
    """确认覆盖：换新 request_id 覆盖（§2.5.3 / summary.save 覆盖语义）。

    ⭐ 2026-10-19：⛔ 不再检查截止（确认期间跨天也只受周期键约束）。
    """
    content = (content or "").strip()
    if not content:
        return EMPTY_CONTENT_TEXT
    period_k = period_mod.period_key(ptype, ctx.now)
    ok, message = _save(ctx, ptype, content)
    if not ok:
        return message
    return _deliver(ctx, overwrite_done_text(ptype))


def _notify_group_summary(ctx: HandlerContext, text: str) -> bool:
    """总结成功后群内 @ 通知（用户指令 2026-10-16 四）。

    与打卡成功通知同口径（checkin._notify_group_checkin）：
    push_text + mention_phones=[ctx.phone] —— 真实 @（⛔ 不靠 content
    文本假装 @，见 platform/qmx.py push_text）。

    best-effort：推送失败由调用方回落普通回复，⛔ 不改写已完成的
    A/B/C 归类（保存结果以 MCP 回执为准，通知是衍生动作）。
    """
    sender = getattr(ctx, "sender", None)
    push = getattr(sender, "push_text", None) if sender else None
    if push is None:
        LOG.info("summary notify skipped: no push port wired")
        return False
    try:
        ok, _reason = push(ctx.group_id, text,
                           mention_phones=[ctx.phone])
    except TypeError:
        # 旧签名 push(group_id, text) 兼容（⛔ 测试替身 / 未升级端口）
        try:
            ok, _reason = push(ctx.group_id, text)
        except Exception as exc:  # 推送端口异常不当已发送
            LOG.warning("summary notify push error: %s", exc)
            return False
    except Exception as exc:
        LOG.warning("summary notify push error: %s", exc)
        return False
    return bool(ok)


def _deliver(ctx: HandlerContext, text: str) -> str:
    """成功话术投递：优先群内 @ 推送；未接通回落普通回复。

    已推送 -> 返空串（router 约定「空串 = 不回复」，⛔ 避免同一条
    话术在群里出现两次：一条 @ 推送 + 一条普通回复）。
    """
    if _notify_group_summary(ctx, text):
        return ""
    return text


def _save(ctx: HandlerContext, ptype: str, content: str) -> tuple:
    """调 MCP 保存（每次换新 request_id：覆盖语义的硬约束）。

    返回 (ok, message)；成功话术由调用方按场景定制（保存 / 覆盖各
    一套，且要走 @ 投递），故成功时 message 为空串。
    """
    period_k = period_mod.period_key(ptype, ctx.now)

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.summary_save(
            user_id=user_id, group_id=ctx.group_id, period_key=period_k,
            period_type=ptype, content=content, request_id=request_id)

    result: SubmitResult = run_submission(
        ctx, biz=BIZ, call=call, period_key=period_k,
        payload={"period_type": ptype, "content_length": len(content)},
        allow_overwrite=True)
    if result.kind == KIND_A:
        return True, ""
    return False, result.message


# ---------- 管理后台补录（用户指令第三阶段） ----------

def admin_backfill(ctx: HandlerContext, period_key: str, period_type: str,
                   content: str) -> SubmitResult:
    """后台补录总结（ctx.phone = 目标用户）。

    - entry_source=admin；⛔ 不检查截止（补录即补截止后的账）
    - 已有记录 -> 覆盖（与用户覆盖同语义，换新 request_id）
    - ⚠️ 调用方（admin API）负责鉴权与审计
    """
    if period_type not in (period_mod.PERIOD_DAILY,
                           period_mod.PERIOD_WEEKLY):
        return SubmitResult(KIND_B, "周期类型必须是 daily / weekly")
    content = (content or "").strip()
    if not content:
        return SubmitResult(KIND_B, "总结内容不能为空")

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.summary_save(
            user_id=user_id, group_id=ctx.group_id, period_key=period_key,
            period_type=period_type, content=content, request_id=request_id,
            entry_source="admin")

    return run_submission(
        ctx, biz=BIZ, call=call, period_key=period_key,
        payload={"period_type": period_type, "content_length": len(content),
                 "entry": "admin"},
        allow_overwrite=True)
