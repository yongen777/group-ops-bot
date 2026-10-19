# -*- coding: utf-8 -*-
"""打卡业务处理器（§2.3 时段 / §4.2 / §4.10 H5 / 用户指令第二阶段 + P1 多时间段）。

群级配置（后台维护，§4.7）：
- checkin_deadlines  {"slots":[{"id","name","start","end"},...]} 多时间段
  （旧格式 {"am":"09:00","pm":"14:50"} 自动兼容转换为两个区间）
- checkin_require_image 1=需上传图片（走 H5）；0=群内命令直接完成

时间段语义（用户指令验收项 2 / 3）：
- 每个区间 = 一次独立打卡机会（额度键带区间 id，⛔ 一个区间只算一次）
- 保存时禁止区间重叠（后台校验）；start < end（⛔ 不做跨夜区间）
- 新增 / 修改**立即对今天生效**：开始时间已过的区间今天仍执行；
  已结束（end 已过）的区间今天不补触发、不补开放
- 用户触发话术三种（2026-10-16 用户指令 2 修订，禁止静默吞消息）：
  还有后续区间 ->「当前不在打卡时段，开放时间为 XX:XX」（= 下一区间开始）
  全部区间已结束 ->「当前不在打卡时段，今日开放时间为 …，已过时段不支持补交…」
  ⛔ 每条都必须自解释开放时间；⛔ 不得让用户发完命令毫无感知

流程（需图，默认）：
    命令 -> 开关/区间/额度校验 -> 签发令牌（fragment 传输）
    -> H5 上传图片 -> submit -> A/B/C 归类；A 后立即消费令牌（§2.4）
流程（免图）：
    命令 -> 同样校验 -> 直接提交（无令牌、无 H5）

硬规则（用户指令第二阶段）：
- ⛔ 截止时间后用户主动补交（命令与 H5 提交双重阻断，固定话术）
- ✅ 管理员后台补录（entry_source=admin，status=ADMIN_EDIT，见 api.py）
- 状态三值：NORMAL（按时用户提交）/ LATE（迟到提交，防御性保留）/
  ADMIN_EDIT（后台补录）
- ⛔ 不用「无图提交」变相绕过产品定义：需图开启时 image_ref 必须来自上传通道
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from .. import period as period_mod
from .. import tokens as tokens_mod
from .base import (KIND_A, KIND_B, KIND_C, HandlerContext, SubmitResult,
                   ALREADY_DONE_TEXT, biz_enabled, ensure_user_id,
                   run_submission, scope_key, token_record_from_row)
from ..tokens import RejectReason, TokenState

LOG = logging.getLogger("group-ops.handlers.checkin")

BIZ = "checkin"

# 产品默认时间段（后台可改）。区间 id 保持 am/pm：与历史额度键 / 既有数据兼容，
# 12:00 分界与旧「12:00 前 am」口径一致（§2.3）。
DEFAULT_CHECKIN_SLOTS = [
    {"id": "am", "name": "上午", "start": "00:00", "end": "09:00"},
    {"id": "pm", "name": "下午", "start": "12:00", "end": "14:50"},
]
MAX_SLOTS = 8
_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")

NOT_ENABLED_TEXT = "本群未开启打卡，请联系管理员"
# 链接话术（用户指令 2026-10-16 二：带有效期提醒）。
# ⚠️ 有效期口径单一真源 = tokens.TOKEN_TTL_MINUTES（30 分钟硬上限，
# ⛔ 不在这里另写一个数字，否则两处必然漂移）。
LINK_TEXT = ("请点击链接完成{label}打卡（上传一张图片）：{url}\n"
             "（链接 {ttl} 分钟内有效，逾期失效，需重新发送「打卡」）")
# 用户指令 2026-10-19：完成话术统一为陈述句（⛔ 不再「您的…辛苦啦」）。
# 平台 @ 由回复 / 推送通道注入，⛔ 文本里不写 @。
SUBMIT_OK_TEXT = "✅ {label}打卡已成功完成。"
IMAGE_REQUIRED_TEXT = "请先上传打卡图片"
IMAGE_REF_INVALID_TEXT = "图片凭证无效或已失效，请重新上传后再提交"
# 非开放时段的三种触发话术（2026-10-16 用户指令 2「禁止静默吞消息」）：
# ⛔ 每一条都必须自解释「现在不在时段 + 开放时间是什么」，
# ⛔ 不得让用户发完命令毫无感知（原「今日打卡已全部完成」只说"已过"，
# ⛔ 不说开放时间，用户无从判断该什么时候再发）。
NOT_YET_OPEN_TEXT = "当前不在打卡时段，开放时间为 {time}（{name}）"
NOT_YET_OPEN_PLAIN_TEXT = "当前不在打卡时段，开放时间为 {time}"
ALL_DONE_TEXT = ("当前不在打卡时段，今日开放时间为 {windows}，"
                 "已过时段不支持补交，请找管理员补交或修复")

# 打卡状态三值（用户指令第二阶段）
STATUS_NORMAL = "NORMAL"
STATUS_LATE = "LATE"
STATUS_ADMIN_EDIT = "ADMIN_EDIT"
ENTRY_USER = "user"
ENTRY_ADMIN = "admin"


def _reject_text(reason: RejectReason) -> str:
    """拒绝话术（⛔ 单一真源在 tokens.reject_message，此处只传业务名）。"""
    return tokens_mod.reject_message(reason, BIZ)


def _audit_reject(ctx: HandlerContext, reason: RejectReason) -> None:
    """越权 / 篡改类拒绝留痕（§2.4）。info 与 submit 两条路径都要调。

    ⛔ 令牌明文不写日志；过期 / 提交中属正常业务事件，不当安全事件刷告警。
    """
    event = tokens_mod.REJECT_SECURITY_EVENT.get(reason)
    if not event:
        return
    ctx.state.record_security_event(event, ctx.group_id, ctx.phone,
                                    "biz=%s" % BIZ)


# 公开别名（Web 上传端点复用同一套拒绝话术 / 留痕，⛔ 不另造一套）
reject_text = _reject_text
audit_token_reject = _audit_reject


# ---------- 群级配置读取（多时间段） ----------

def _normalize_slot(raw) -> Optional[dict]:
    """单区间校验归一化；不合法返回 None（调用方决定 fail 行为）。"""
    if not isinstance(raw, dict):
        return None
    slot_id = raw.get("id")
    if not isinstance(slot_id, str) or not _SLOT_ID_RE.match(slot_id):
        return None
    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 32):
        return None
    start = period_mod.parse_hhmm(raw.get("start"))
    end = period_mod.parse_hhmm(raw.get("end"))
    if start is None or end is None or start >= end:
        return None  # ⛔ 不做跨夜区间：start 必须早于 end
    return {"id": slot_id, "name": (name or "").strip(),
            "start": "%02d:%02d" % start, "end": "%02d:%02d" % end}


def normalize_slots_config(raw) -> Optional[list]:
    """checkin_deadlines 配置值 -> 规范区间列表（不可解析返回 None）。

    新格式 {"slots":[...]}；旧格式 {"am":"09:00","pm":"14:50"} 兼容转换
    （am=00:00~上午截止，pm=12:00~下午截止；旧数据里 pm 开始被上午截止
    吞掉时自动后移，⛔ 不让一个坏配置把全天都判成「已结束」）。
    """
    try:
        configured = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if not isinstance(configured, dict):
        return None
    if "slots" in configured:
        slots = configured.get("slots")
        if not isinstance(slots, list) or not (1 <= len(slots) <= MAX_SLOTS):
            return None
        result, seen = [], set()
        for item in slots:
            slot = _normalize_slot(item)
            if slot is None or slot["id"] in seen:
                return None
            seen.add(slot["id"])
            result.append(slot)
        result.sort(key=lambda s: s["start"])
        # 运行时同样禁重叠（与后台保存口径一致）：脏配置回落产品默认值，
        # ⛔ 不让一个重叠配置把某个区间永久藏掉
        for prev, cur in zip(result, result[1:]):
            if cur["start"] < prev["end"]:
                return None
        return result
    # ---- 旧格式兼容（v4 及以前的 {"am": "09:00", "pm": "14:50"}）----
    legacy = {}
    for key, default in (("am", "09:00"), ("pm", "14:50")):
        value = configured.get(key)
        parsed = period_mod.parse_hhmm(value) if isinstance(value, str) else None
        legacy[key] = "%02d:%02d" % parsed if parsed else default
    am_end = legacy["am"]
    pm_start = max("12:00", am_end)
    result = [{"id": "am", "name": "上午", "start": "00:00", "end": am_end}]
    if pm_start < legacy["pm"]:
        result.append({"id": "pm", "name": "下午",
                       "start": pm_start, "end": legacy["pm"]})
    return result


def slots_from_row(row) -> list:
    """群配置行 -> 区间列表（坏配置回落产品默认值，fail-closed）。"""
    raw = row["checkin_deadlines"] if row is not None else None
    slots = normalize_slots_config(raw)
    if slots is None:
        return [dict(s) for s in DEFAULT_CHECKIN_SLOTS]
    return slots


def configured_slots(ctx: HandlerContext) -> list:
    """本群打卡区间（后台配置；缺省 / 非法回落产品默认值）。"""
    return slots_from_row(ctx.state.get_group_config(ctx.group_id))


def deadlines(ctx: HandlerContext) -> dict:
    """兼容视图：区间 id -> 截止 HH:MM（{"am": "09:00", ...}）。"""
    return {s["id"]: s["end"] for s in configured_slots(ctx)}


def require_image(ctx: HandlerContext) -> bool:
    """本群打卡是否必须上传图片（默认开，§用户指令第二阶段「默认：图片上传开启」）。"""
    row = ctx.state.get_group_config(ctx.group_id)
    if row is None:
        return True
    try:
        return bool(row["checkin_require_image"])
    except (IndexError, KeyError):  # pragma: no cover - 旧库无该列
        return True


def _day_of(now: datetime) -> datetime:
    return period_mod.parse_daily_key(period_mod.daily_key(now))


def _slot_window(day: datetime, hhmm: str) -> datetime:
    hour, minute = period_mod.parse_hhmm(hhmm)
    return day.replace(hour=hour, minute=minute)


def resolve_slot(slots: list, now: datetime):
    """当前开放的区间 + 下一个待开放区间。

    返回 (slot, next_slot)；开放中 -> (slot, None)；
    未到任何区间 -> (None, 下一区间)；全部结束 -> (None, None)。
    """
    day = _day_of(now)
    for slot in sorted(slots, key=lambda s: s["start"]):
        start = _slot_window(day, slot["start"])
        end = _slot_window(day, slot["end"])
        if now < start:
            return None, slot
        if now < end:
            return slot, None
    return None, None


def current_slot(ctx: HandlerContext):
    """本群当前打卡区间（(slot, next_slot)，语义同 resolve_slot）。"""
    return resolve_slot(configured_slots(ctx), ctx.now)


def slot_by_id(ctx: HandlerContext, slot_id: str) -> Optional[dict]:
    for slot in configured_slots(ctx):
        if slot["id"] == slot_id:
            return slot
    return None


def ended_slot_ids(slots: list, now: datetime) -> list:
    """已结束的区间 id（提醒判定用：end 已过 = 应完成）。"""
    day = _day_of(now)
    return [s["id"] for s in slots if now >= _slot_window(day, s["end"])]


def _slot_label(slot: dict) -> str:
    return slot.get("name") or slot["id"]


def _windows_text(slots: list) -> str:
    """本群今日全部开放区间（"08:00-09:00、14:00-14:50"）。

    ⛔ 已结束区间也一并列出：用户需要知道"今天本来是什么时段"，
    才能判断是自己错过了还是配置不对。坏区间跳过（不让脏配置破话术）。
    """
    parts = ["%s-%s" % (s["start"], s["end"]) for s in slots
             if s.get("start") and s.get("end")]
    return "、".join(parts)


def _not_open_text(next_slot: Optional[dict], slots: Optional[list] = None
                   ) -> str:
    """未在开放时段的话术（⛔ 每条都自带开放时间，绝不静默）。

    有后续区间 -> 报下一场开放时间；全部区间已结束 -> 报今日全部开放时段。
    """
    if next_slot is None:
        windows = _windows_text(slots or [])
        if not windows:
            return ALL_DONE_TEXT.format(windows="未配置")
        return ALL_DONE_TEXT.format(windows=windows)
    if next_slot.get("name"):
        return NOT_YET_OPEN_TEXT.format(time=next_slot["start"],
                                        name=next_slot["name"])
    return NOT_YET_OPEN_PLAIN_TEXT.format(time=next_slot["start"])


def session_deadline_dt(ctx: HandlerContext, session: str) -> datetime:
    """当日该区间截止时刻（TZ 感知；配置值 + 当日日期）。"""
    slot = slot_by_id(ctx, session)
    if slot is None:
        raise KeyError("unknown checkin slot: %r" % (session,))
    return _slot_window(_day_of(ctx.now), slot["end"])


def is_past_deadline(ctx: HandlerContext, session: str) -> bool:
    return ctx.now >= session_deadline_dt(ctx, session)


def _status_for_user(ctx: HandlerContext, slot: dict) -> str:
    """用户路径状态：按时 NORMAL；迟到的防御性标记 LATE（开放判定处已阻断）。"""
    return STATUS_LATE if is_past_deadline(ctx, slot["id"]) else STATUS_NORMAL


# ---------- 入口命令 ----------

def handle_command(parsed, ctx: HandlerContext) -> str:
    """「打卡」入口命令：需图 -> 签发 H5 令牌；免图 -> 直接提交。"""
    if not biz_enabled(ctx, BIZ):
        return NOT_ENABLED_TEXT
    slots = configured_slots(ctx)
    slot, next_slot = resolve_slot(slots, ctx.now)
    if slot is None:
        return _not_open_text(next_slot, slots)
    period_k = period_mod.daily_key(ctx.now)
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_k, slot["id"])
    if ctx.state.has_successful_scope(scope):
        return ALREADY_DONE_TEXT[BIZ]
    if not require_image(ctx):
        return _submit_direct(ctx, slot, period_k, scope)
    value = tokens_mod.generate_token_value()
    # 令牌过期 = 区间截止 + 24h（§2.4，截止点取群配置；30 分钟封顶在 new_record 内）
    expires_at = _slot_window(_day_of(ctx.now), slot["end"]) + timedelta(hours=24)
    record = tokens_mod.new_record(value, ctx.phone, ctx.group_id, BIZ,
                                   period_k, slot["id"], ctx.now,
                                   expires_at=expires_at)
    ctx.state.put_token(record)
    url = "%s/h5/checkin#t=%s" % (ctx.config.external_base_url, value)
    return LINK_TEXT.format(label=_slot_label(slot), url=url,
                            ttl=tokens_mod.TOKEN_TTL_MINUTES)


def _submit_direct(ctx: HandlerContext, slot: dict, period_k: str,
                   scope: str) -> str:
    """免图模式：命令即提交（image_ref 为空串，⛔ 不是绕过需图规则）。"""
    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.checkin_submit(
            user_id=user_id, group_id=ctx.group_id, period_key=period_k,
            session=slot["id"], image_ref="", image_meta=None,
            status=_status_for_user(ctx, slot), entry_source=ENTRY_USER,
            request_id=request_id)

    result = run_submission(
        ctx, biz=BIZ, call=call, period_key=period_k, session=slot["id"],
        payload={"session": slot["id"], "entry": "direct"})
    if result.kind == KIND_A and not result.duplicate:
        return SUBMIT_OK_TEXT.format(label=_slot_label(slot))
    return result.message


# ---------- H5 提交 ----------

def validate_token(ctx: HandlerContext, value: str,
                   with_session: bool = True):
    """H5 提交前的令牌校验。返回 (record, reject_reason)。

    with_session=False：跳过「令牌时段 == 当前开放区间」校验，只做与时段
    无关的硬鉴权（存在性 / 越权 / 周期 / 状态）。调用方必须先跑这一版，
    确保任何拒绝都留痕；时段绑定在解析出当前区间后单独判（§2.4 顺序）。
    """
    if not value:
        return None, RejectReason.NOT_FOUND
    record = token_record_from_row(ctx.state.get_token(
        tokens_mod.token_id_of(value)))
    period_k = period_mod.daily_key(ctx.now)
    slot, _next = current_slot(ctx) if with_session else (None, None)
    session = slot["id"] if slot else None
    reason = tokens_mod.check_authorization(
        record, value, ctx.phone, ctx.group_id, BIZ, period_k, session,
        ctx.now)
    return record, reason


def _validate_image_ref(ctx: HandlerContext, record, image_ref: str):
    """校验 image_ref 属于**当前令牌**签发的上传（§4.11.4 D4 定夺）。

    返回 (upload_row, None) 或 (None, 拒绝话术)。
    ⛔ 不认客户端自报的 image_meta：name/size 以服务端凭证行为准。
    ⛔ 安全重试放行：已 consumed 的行仍认（防刷新页面重提交丢图），
    但 token_id 必须与当前令牌一致（⛔ 不认他人链接的图）。
    """
    row = ctx.state.get_upload(image_ref)
    if row is None or row["token_id"] != record.token_id:
        return None, IMAGE_REF_INVALID_TEXT
    if row["group_id"] != ctx.group_id or row["phone"] != ctx.phone:
        return None, IMAGE_REF_INVALID_TEXT
    return row, None


def _notify_group_checkin(ctx: HandlerContext, slot: dict) -> bool:
    """打卡成功后群内 @ 通知（用户指令 2026-10-16 二）。

    内容形如「✅ 您的上午打卡已成功完成，辛苦啦！」，并带**真实手机号** @
    （⛔ 不靠 content 文本假装 @，见 platform/qmx.py push_text）。
    2026-10-16 四：文案统一为总结格式风格（与 summary/report 同骨架，
    ⛔ 不再拼姓名——真实 @ 已指明提交人）。

    best-effort：推送失败只记日志，⛔ 不改写已完成的 A/B/C 归类
    （打卡结果以 MCP 回执为准，通知是衍生动作）。
    """
    sender = getattr(ctx, "sender", None)
    push = getattr(sender, "push_text", None) if sender else None
    if push is None:
        LOG.info("checkin notify skipped: no push port wired")
        return False
    text = SUBMIT_OK_TEXT.format(label=_slot_label(slot))
    try:
        ok, _reason = push(ctx.group_id, text,
                           mention_phones=[ctx.phone])
    except TypeError:
        # 旧签名 push(group_id, text) 兼容（⛔ 测试替身 / 未升级端口）
        try:
            ok, _reason = push(ctx.group_id, text)
        except Exception as exc:  # 推送端口异常不当已发送
            LOG.warning("checkin notify push error: %s", exc)
            return False
    except Exception as exc:
        LOG.warning("checkin notify push error: %s", exc)
        return False
    return bool(ok)


def submit_from_h5(ctx: HandlerContext, value: str, image_ref: str,
                   image_meta: Optional[dict] = None) -> SubmitResult:
    """H5 提交打卡。image_ref 由上传通道签发（§4.11.4 D4 定夺后已接通）。

    顺序纪律（§2.4）：先时段无关的硬鉴权（越权/状态一律先拒并留痕），
    再判业务开放区间（两种话术），最后校验令牌时段绑定 + 图片凭证。
    """
    period_k = period_mod.daily_key(ctx.now)
    record, reason = validate_token(ctx, value, with_session=False)
    if reason is not None and reason != RejectReason.ALREADY_CONSUMED:
        _audit_reject(ctx, reason)
        return SubmitResult(KIND_B, _reject_text(reason))

    slots = configured_slots(ctx)
    slot, next_slot = resolve_slot(slots, ctx.now)
    if slot is None:
        # 未在开放时段：明确话术（⛔ 含开放时间，禁止静默）
        return SubmitResult(KIND_B, _not_open_text(next_slot, slots))
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_k, slot["id"])

    if reason == RejectReason.ALREADY_CONSUMED:
        # 安全重试（§2.4）：不得判为全新提交；查范围键已成功 -> 归 A 语义
        if ctx.state.has_successful_scope(scope):
            return SubmitResult(KIND_A, SUBMIT_OK_TEXT.format(
                label=_slot_label(slot)), duplicate=True)
        # 令牌已消费但本地无成功记录：结果未知，走恢复（⛔ 不自动重试）
        return SubmitResult(KIND_C,
                            "该链接的提交状态未知，系统将自动核对。请勿重复提交")

    if record.session != slot["id"]:
        # 令牌签发时绑定的是另一个区间：换链接，⛔ 不得跨区间提交
        _audit_reject(ctx, RejectReason.WRONG_PERIOD)
        return SubmitResult(KIND_B, _reject_text(RejectReason.WRONG_PERIOD))

    if not image_ref:
        return SubmitResult(KIND_B, IMAGE_REQUIRED_TEXT)
    upload_row, invalid = _validate_image_ref(ctx, record, image_ref)
    if upload_row is None:
        return SubmitResult(KIND_B, invalid)
    # 图片元数据以服务端凭证行为准（⛔ 不信客户端自报 name/size）
    meta = {"name": upload_row["file_name"], "size": upload_row["size"]}

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.checkin_submit(
            user_id=user_id, group_id=ctx.group_id, period_key=period_k,
            session=slot["id"], image_ref=image_ref, image_meta=meta,
            status=_status_for_user(ctx, slot), entry_source=ENTRY_USER,
            request_id=request_id)

    result = run_submission(
        ctx, biz=BIZ, call=call, period_key=period_k, session=slot["id"],
        payload={"session": slot["id"], "image_name": meta["name"]},
        consume_token_id=record.token_id)
    if result.kind == KIND_A and not result.duplicate:
        ctx.state.mark_upload_consumed(image_ref)
        _notify_group_checkin(ctx, slot)
        return SubmitResult(KIND_A, SUBMIT_OK_TEXT.format(
            label=_slot_label(slot)), request_id=result.request_id,
            record_id=result.record_id)
    return result


def token_info(ctx: HandlerContext, value: str) -> dict:
    """H5 页面初始化（只本地校验，⛔ 不调 MCP）。

    顺序纪律同 submit_from_h5：先硬鉴权（留痕），再开放区间话术。
    """
    period_k = period_mod.daily_key(ctx.now)
    record, reason = validate_token(ctx, value, with_session=False)
    if reason is not None and reason != RejectReason.ALREADY_CONSUMED:
        _audit_reject(ctx, reason)
        return {"ok": False, "reason": reason.value,
                "message": _reject_text(reason)}
    slots = configured_slots(ctx)
    slot, next_slot = resolve_slot(slots, ctx.now)
    if slot is None:
        return {"ok": False,
                "reason": "all_done" if next_slot is None else "not_open",
                "message": _not_open_text(next_slot, slots)}
    done = ctx.state.has_successful_scope(
        scope_key(ctx.group_id, ctx.phone, BIZ, period_k, slot["id"]))
    if reason is None and record.session != slot["id"]:
        # 活跃令牌但绑定的是别的区间：拒绝并留痕（已消费令牌不在此列，
        # 走 done/consumed 语义，提交时按 C 类结果未知处理）
        _audit_reject(ctx, RejectReason.WRONG_PERIOD)
        return {"ok": False, "reason": RejectReason.WRONG_PERIOD.value,
                "message": _reject_text(RejectReason.WRONG_PERIOD)}
    return {"ok": True, "session": slot["id"],
            "session_label": _slot_label(slot),
            "period_key": period_k, "done": done,
            "deadline": slot["end"],
            # 链接过期时刻（H5 倒计时 / 逾期禁用用；ISO 带时区）
            "expires_at": (record.expires_at.isoformat() if record else None),
            "ttl_minutes": tokens_mod.TOKEN_TTL_MINUTES,
            "state": (record.state if record else TokenState.CONSUMED.value)}


# ---------- 管理后台补录（用户指令第二阶段：允许管理员后台补录） ----------

def admin_backfill(ctx: HandlerContext, period_key: str, session: str,
                   status: str = STATUS_ADMIN_EDIT,
                   checkin_time: Optional[str] = None) -> SubmitResult:
    """后台补录打卡（ctx.phone = 目标用户）。

    - entry_source=admin；status 由管理员选择（默认 ADMIN_EDIT）
    - ⛔ 不检查截止（补录的本质就是补截止后的账）
    - ✅ 仍遵守额度：同范围已成功 -> B（⛔ 不覆盖已有记录）
    - session 必须是本群已配置的区间 id（多时间段后不再是固定 am/pm）
    - checkin_time（2026-10-19 新增）：管理员指定的补录时间 HH:MM[:SS]，
      ⛔ 不传则由服务端记录实际补录时刻（等价旧行为）；传值只用于「管理员
      操作补录时间」留痕，不改额度判定
    - ⚠️ 调用方（admin API）负责鉴权与审计；本函数只做业务管线
    """
    slot = slot_by_id(ctx, session)
    if slot is None:
        return SubmitResult(KIND_B, "时段必须是本群已配置的时间段标识")
    if status not in (STATUS_NORMAL, STATUS_LATE, STATUS_ADMIN_EDIT):
        return SubmitResult(KIND_B, "状态必须是 NORMAL / LATE / ADMIN_EDIT")
    scope = scope_key(ctx.group_id, ctx.phone, BIZ, period_key, slot["id"])
    if ctx.state.has_successful_scope(scope):
        return SubmitResult(KIND_B, "该用户本时段已有打卡记录，不能重复补录")

    extra = {}
    if checkin_time:
        extra["checkin_time"] = checkin_time

    def call(request_id: str) -> dict:
        user_id = ensure_user_id(ctx)
        return ctx.backend.checkin_submit(
            user_id=user_id, group_id=ctx.group_id, period_key=period_key,
            session=slot["id"], image_ref="", image_meta=None,
            status=status, entry_source=ENTRY_ADMIN, request_id=request_id,
            **extra)

    result = run_submission(
        ctx, biz=BIZ, call=call, period_key=period_key, session=slot["id"],
        payload={"session": slot["id"], "entry": ENTRY_ADMIN, "status": status})
    return result
