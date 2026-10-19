# -*- coding: utf-8 -*-
"""定时提醒业务处理器（§2.5.5 判定依据 / §4.5 / 用户指令第五阶段 + 验收项 4）。

判定：群级应交名单 − 已完成记录（§2.5.5：只认后端确认的成功记录，
⛔ 不以本地 intent / 令牌状态替代）。应完成时间取群级提醒规则
（reminder_rules），缺省 09:00 / 14:50 / 19:00（产品定义）。

提醒规则**按业务独立配置**（用户指令验收项 4）：
    {"checkin": {"enabled": true, "times": ["09:00","14:50"], "text": ""},
     "summary": {"enabled": true, "times": ["19:00"], "text": ""},
     "report":  {"enabled": true, "times": ["19:00"], "text": ""}}
- enabled：该业务独立开关（关 = 不计不发送，即使群提醒总开关开着）
- times：该业务的提醒时刻表（HH:MM，多个；到点才判定）
  ⚠️ 2026-10-16 用户指令三：**打卡业务的 times 不再作为触发时刻** ——
  打卡提醒时刻固定 = 各时段**开始时间**（见 _checkin_reminder_times）；
  times 仅为兼容保留（旧配置不炸），⛔ 不得再拿它当打卡触发时刻。
- text：该业务的提醒文本（留空 = 默认文案；非空 = 拼接未完成名单）
旧格式（{"checkin": ["09:00", ...]} 纯时间列表）自动兼容转换。

用户指令第五阶段新增规则：
- ⛔ 只对**开启中**的业务发提醒：群停用 / 提醒开关关 / 业务开关关
  （checkin / summary / report 各自）-> 对应目标不计不发送
- ✅ C 类待核验用户本轮**暂不判定**为未完成（§4.5 第 5 条：
  挂起期间等恢复结论，⛔ 不误提醒、⛔ 不静默当未完成）
- ✅ 防重（2026-10-17 修复「上午 8 次 / 下午 7 次」，门控**先于**推送）：
  自动调度每期每人每时段**只发一次** —— 已有 status='sent' 计划记录的
  目标 ⛔ 不再发（record_reminder_plan(resend_sent=False)）；
  unsent / pending 按 5 分钟冷却（state.REMINDER_COOLDOWN_MINUTES）重试，
  那是推送失败的容错，⛔ 不是重复打扰。根因：旧顺序先 push 后判冷却，
  8 分钟触发窗口 × 60s 轮询 = 窗口内每轮都重发。
- ✅ 手动提醒（2026-10-16 用户指令三）：管理后台按业务「立即提醒」
  （run_group(biz=..., manual=True)）—— 跳过时间门控，取全部未结束时段
  （打卡）/ 直接判定（总结 / 报数），与自动调度同一套判定 / 冷却 / 落库，
  ⛔ 不搞第二套逻辑。手动路径 5 分钟冷却已过**可重发**（含已发送记录，
  管理员主动催；只防连点轰炸）。

⚠️ 推送端口（2026-10-16 已实测接通）：发送经注入的 push 端口，
未接通 / 失败时如实记 unsent + 原因，⛔ 不伪造已发送。
⛔ 2026-10-17 用户指令：提醒推送**不带平台 @**（mentionedMobileList）——
消息开头那个 @ 由平台按手机号渲染，用户要求移除，只保留文案内
「@姓名(上午/下午)」名单（format_message 已含）。群聊**回复**的 @
（server.py 回话点）不受影响，仍走 mention_phones。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .. import period as period_mod
from ..backend.base import Backend
from ..state import StateStore
from .base import BOT_MENTION_NAME

BIZ = "reminder"

DEFAULT_REMINDER_RULES = {
    "checkin": ["09:00", "14:50"],
    "summary": ["19:00"],
    "report": ["19:00"],
}
# 提醒按业务独立配置（验收项 4）：可配置业务 / 时刻表上限 / 文本上限
REMINDER_BIZ = ("checkin", "summary", "report")
MAX_REMINDER_TIMES = 5
MAX_REMINDER_TEXT = 200
# 打卡产品截止（与令牌时段边界 12:00 是两回事，§2.3 / period.DEFAULT_AM_CUTOFF）
CHECKIN_DEADLINES = {"am": "09:00", "pm": "14:50"}
# 打卡提醒触发窗口（分钟）：时段开始后的这段时间内算"应提醒"。
# ⛔ 取 8 而非 1：调度轮询间隔 60s，窗口必须 > 间隔才能保证
# 「时段开始那一刻」一定被某轮覆盖，又不至于把提醒拖到时段中途。
REMINDER_TRIGGER_WINDOW_MINUTES = 8


@dataclass
class ReminderTarget:
    group_id: str
    biz: str
    period_key: str
    phone: str
    name: Optional[str]
    session: Optional[str] = None


def _passed(now: datetime, hhmm: str) -> bool:
    """now 是否已过今日 hhmm 时刻（服务端时区，§2.3）。"""
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except ValueError:
        return False
    return (now.hour, now.minute) >= (hour, minute)


def _minute_of_day(hhmm: str) -> Optional[int]:
    try:
        hour, minute = (int(x) for x in hhmm.split(":"))
    except (ValueError, AttributeError):
        return None
    return hour * 60 + minute


def _checkin_reminder_times(group_row, now: datetime) -> list:
    """打卡业务本轮触发的**时段 id**（触发时刻 = 该时段开始时间）。

    2026-10-16 用户指令四：提醒时间 = 打卡时段开始时间
    （如 09:00-12:00 -> 09:00 提醒，⛔ 不是「开始后 30 分钟」）。

    触发窗口语义（解决「09:30 才提醒」的根因）：
    - 落在 ``[start, start + 窗口)`` 内 -> 该时段本轮应提醒；
    - 窗口默认 **8 分钟**（> 调度 60s 的粒度，保证开始那一刻必被某轮覆盖）；
    - ⛔ 已结束时段不再提醒（指令五）；⛔ 超窗不补发；
    - 每个时段每天只提醒一次 ⛔ —— 由 reminder_plan 唯一索引保证。
    """
    from . import checkin as checkin_handler
    slots = checkin_handler.slots_from_row(group_row)
    now_min = now.hour * 60 + now.minute
    hits = []
    for slot in slots:
        start = _minute_of_day(slot["start"])
        end = _minute_of_day(slot["end"])
        if start is None or end is None or now_min >= end:
            continue  # ⛔ 时段已结束 -> 不再提醒
        if start <= now_min < start + REMINDER_TRIGGER_WINDOW_MINUTES:
            hits.append(slot["id"])   # ⭐ 返回 id（compute_incomplete 按 id 查）
    return hits


def _open_slot_ids(group_row, now: datetime) -> list:
    """**未结束**的打卡时段 id（手动提醒用：已结束时段 submission 已阻断，
    提醒无意义；未开始的时段也列上 —— 手动催一下无妨）。

    ⛔ 与 _checkin_reminder_times 的分工：那个是「到点才触发」的自动口径
    （时段开始后 8 分钟窗口内）；本函数是「只要还没过截止就算」的手动口径。
    """
    from . import checkin as checkin_handler
    slots = checkin_handler.slots_from_row(group_row)
    now_min = now.hour * 60 + now.minute
    hits = []
    for slot in slots:
        end = _minute_of_day(slot["end"])
        if end is None or now_min >= end:
            continue  # ⛔ 时段已结束 -> 不提醒（补交请走管理员补录）
        hits.append(slot["id"])
    return hits


def _default_rule(biz: str) -> dict:
    return {"enabled": True,
            "times": list(DEFAULT_REMINDER_RULES.get(biz, [])),
            "text": ""}


def normalize_reminder_rules(raw) -> dict:
    """reminder_rules 配置值 -> 按业务规范化的规则 dict（坏值回落默认）。

    新格式 {biz: {"enabled", "times", "text"}}；
    旧格式 {biz: ["09:00", ...]}（纯时间列表）兼容转换。
    """
    result = {biz: _default_rule(biz) for biz in REMINDER_BIZ}
    try:
        configured = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return result
    if not isinstance(configured, dict):
        return result
    for biz in REMINDER_BIZ:
        value = configured.get(biz)
        if isinstance(value, list):  # 旧格式：时间列表
            times = [str(x) for x in value if isinstance(x, str)]
            if times:
                result[biz]["times"] = times[:MAX_REMINDER_TIMES]
            continue
        if not isinstance(value, dict):
            continue
        if "enabled" in value:
            result[biz]["enabled"] = bool(value["enabled"])
        times = value.get("times")
        if isinstance(times, list):
            clean = [str(x) for x in times if isinstance(x, str)]
            if clean:
                result[biz]["times"] = clean[:MAX_REMINDER_TIMES]
        text = value.get("text")
        if isinstance(text, str):
            result[biz]["text"] = text.strip()[:MAX_REMINDER_TEXT]
    return result


def _rules_for(group_row, biz: str) -> dict:
    """该业务的提醒规则（enabled / times / text）。"""
    rules = normalize_reminder_rules(
        group_row["reminder_rules"] if group_row else None)
    return rules.get(biz) or _default_rule(biz)


def compute_incomplete(ctx, group_id: str, biz: str, period_key: str,
                       sessions: Optional[list] = None) -> list:
    """应交名单 − 已完成（§2.5.5）。BackendError 向上抛（整轮放弃发送）。

    名单只取 enabled=1（§4.7：停用 = 停止未来任务）；昵称取 nickname 列。

    §4.5 第 5 条：存在 C 类待核验记录的用户本轮**暂不判定**为未完成
    （挂起期间等恢复结论，⛔ 不误提醒）。
    """
    # C 类挂起用户的 scope 前缀集合（group|phone|biz|...）
    pending_scopes = [row["scope_key"]
                      for row in ctx.state.pending_recovery_tasks()]
    targets = []
    for person in ctx.state.list_member_tasks(group_id, enabled_only=True):
        phone = person["user_id"]
        scope_prefix = "%s|%s|%s|" % (group_id, phone, biz)
        if any(s.startswith(scope_prefix) for s in pending_scopes):
            continue  # C 待核验：本轮暂不判定为未完成
        user_row = ctx.backend.user_get(phone=phone).get("user")
        if user_row is None:
            # 从未提交过任何内容的人员目录里不存在 -> 未完成
            targets.append(ReminderTarget(group_id, biz, period_key, phone,
                                          person["nickname"]))
            continue
        user_id = int(user_row["id"])
        if biz == "checkin":
            for session in (sessions or list(CHECKIN_DEADLINES)):
                status = ctx.backend.checkin_query_status(
                    user_id=user_id, group_id=group_id, period_key=period_key,
                    session=session)
                if not status.get("completed"):
                    targets.append(ReminderTarget(
                        group_id, biz, period_key, phone,
                        person["nickname"], session))
        else:
            query = (ctx.backend.summary_query_status if biz == "summary"
                     else ctx.backend.report_query_status)
            status = query(user_id=user_id, group_id=group_id,
                           period_key=period_key)
            if not status.get("completed"):
                targets.append(ReminderTarget(
                    group_id, biz, period_key, phone, person["nickname"]))
    return targets


_BIZ_LABEL = {"checkin": "打卡", "summary": "总结", "report": "报数"}


def format_message(targets: list, text: str = "",
                   deadline_text: str = "",
                   slot_names: Optional[dict] = None) -> str:
    """提醒文案（⭐ 2026-10-19 用户指令：统一为固定五段式）。

        @张三(上午) @李四(下午) 🔔 【打卡】未完成提醒
        📋 待提交名单：
        · 张三（上午）
        · 李四（下午）
        ⏰ 截止时间：12:00（上午）、14:50（下午）
        👉 快速处理：请发送「@general-bot 打卡」完成提交。

    ⚠️ 头部的 @姓名 是**文案内**的名单，⛔ 不是平台的 mention：
    2026-10-17 用户指令「移除第一个艾特，保留一个」仍然有效，
    推送时 ⛔ 不传 mention_phones（见 run_group）。

    text 非空 = 该业务配置的自定义文本（仍作为开头一行）。
    deadline_text = 截止说明（打卡按时段结束时间；总结/报数固定今晚 24:00）。
    slot_names = {时段 id: 时段名}，用于把 am/pm 显示成「上午/下午」。
    """
    if not targets:
        return ""
    names = []
    for t in targets:
        name = t.name or (t.phone[-4:].rjust(11, "*"))
        if t.session:
            slot_label = (slot_names or {}).get(t.session) or (
                "上午" if t.session == "am" else "下午")
            name = "%s(%s)" % (name, slot_label)
        names.append(name)
    biz = targets[0].biz
    biz_label = _BIZ_LABEL.get(biz, biz)
    lines = []
    if text:
        lines.append(text)
    # 头部：文案内 @ 名单（⛔ 不触发平台 mention）
    lines.append("%s 🔔 【%s】未完成提醒"
                 % (" ".join("@%s" % n for n in names), biz_label))
    lines.append("📋 待提交名单：")
    for name in names:
        lines.append("· %s" % name.replace("(", "（").replace(")", "）"))
    if deadline_text:
        lines.append("⏰ 截止时间：%s" % deadline_text)
    lines.append("👉 快速处理：请发送「@%s %s」完成提交。"
                 % (BOT_MENTION_NAME, biz_label))
    return "\n".join(lines)


def _checkin_deadline_text(group_row, now: datetime, targets: list) -> str:
    """打卡截止说明（⭐ 2026-10-19：⏰ 行只写未结束时段的结束时刻）。

    ⛔ 只说**仍未结束**的时段：已结束时段不应再劝用户"请在 X 前完成"，
    那种话已经无意义（截止时间已过）。全部已结束则给出补交出口。
    """
    from . import checkin as checkin_handler
    slots = checkin_handler.slots_from_row(group_row)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def _at(hhmm: str) -> datetime:
        hour, minute = hhmm.split(":")
        return day.replace(hour=int(hour), minute=int(minute))

    ended_ids = {s["id"] for s in slots if now >= _at(s["end"])}
    wanted = {t.session for t in targets if t.session}
    open_slots = [s for s in slots
                  if s["id"] in wanted and s["id"] not in ended_ids]
    if open_slots:
        return "、".join("%s（%s）" % (s["end"], s["name"])
                         for s in open_slots)
    return "今日打卡时段已结束，如需补交请联系群管理员"


def run_group(state: StateStore, backend: Backend, push, group_row,
              now: datetime, biz: Optional[str] = None,
              manual: bool = False) -> dict:
    """单群一轮提醒（用户指令第五阶段 + 验收项 4：开关门控 + 防重 + C 挂起排除）。

    返回 {"targets", "new", "sent", "unsent", "errors", "skipped", "biz",
    "manual", "skipped_cooled"}；
    skipped 非空表示本群被门控跳过（群停用 / 提醒开关关）；
    skipped_cooled = 被防重门控拦下的目标数（⛔ 消息也不会发）。

    ⭐ 发送顺序（2026-10-17 修复「上午 8 次 / 下午 7 次」）：
    先逐目标过 record_reminder_plan 门控，**只有 fresh 目标才推送**
    （一条合并消息）；⛔ 禁止先把消息推出去再判冷却 / 已计划。

    参数（用户指令 2026-10-16 三：按业务一键手动提醒）：
    - biz：只跑该业务（None = 三个业务全跑；自动调度用 None）
    - manual：管理员手动触发 -> ⛔ 跳过「到点才判定」的时间门控：
        打卡取**全部未结束时段**（已结束时段 submission 已阻断，提醒无意义）；
        总结 / 报数直接判定一轮。防重走 5 分钟冷却（连点不轰炸），
        冷却已过**可重发**（含本期已发送过的记录）。

    手动提醒与自动调度（tick）共用本函数，⛔ 不搞第二套判定逻辑。
    """
    from .base import HandlerContext  # 局部导入避免环

    summary = {"targets": 0, "new": 0, "sent": 0, "unsent": 0,
               "errors": [], "skipped": None, "biz": biz,
               "manual": manual, "skipped_cooled": 0}
    group_id = group_row["group_id"]
    if not state.group_active(group_id):
        summary["skipped"] = "group_disabled"
        return summary
    switches = state.normalized_switches(group_row)
    if not switches.get("reminder"):
        summary["skipped"] = "reminder_disabled"
        return summary
    ctx = HandlerContext(
        group_id=group_id, phone="", name=None, platform_user_id=None,
        text="", state=state, backend=backend, config=None, now=now)
    ptype = (group_row["period_type"]
             if group_row["period_type"] in ("daily", "weekly") else "daily")
    period_keys = {"checkin": period_mod.daily_key(now),
                   "report": period_mod.daily_key(now),
                   "summary": period_mod.period_key(ptype, now)}
    for one in ("checkin", "summary", "report"):
        if biz and one != biz:
            continue
        # ⛔ 只对开启中的业务发提醒（用户指令第五阶段 + 验收项 4：
        #    群业务开关 + 该业务自己的提醒开关，两道门）
        if not switches.get(one):
            continue
        rule = _rules_for(group_row, one)
        if not rule["enabled"]:
            continue
        # ⭐ 打卡业务：提醒时刻 = 时段开始时间（2026-10-16 用户指令四）
        # ⛔ 不再用「已过 rule.times 即触发」——那会在时段中途反复命中，
        #    配合冷却就表现为用户报的「配了 09:00 却 09:30 才提醒」。
        # ⭐ 手动触发（manual=True）：跳过时间门控，取全部未结束时段。
        if one == "checkin":
            if manual:
                triggered = _open_slot_ids(group_row, now)
            else:
                triggered = _checkin_reminder_times(group_row, now)
        elif manual:
            triggered = rule["times"] or ["00:00"]
        else:
            triggered = [t for t in rule["times"] if _passed(now, t)]
        if not triggered:
            if manual and one == "checkin":
                summary["skipped"] = "checkin_all_done"
            continue
        # ⭐ 判定对象 = **本轮触发的时段**（而非"所有已结束时段"）：
        #    提醒在时段开始时发，就要提醒这个时段里还没打卡的人；
        #    ⛔ 若仍按"已结束"取，时段刚开始那一刻拿不到任何人 -> 永不发。
        sessions = triggered if one == "checkin" else None
        targets = compute_incomplete(ctx, group_id, one,
                                     period_keys[one], sessions)
        summary["targets"] += len(targets)
        if not targets:
            continue
        # ⭐ 门控**先行**（2026-10-17 修复「上午 8 次 / 下午 7 次」）：
        # 旧顺序把 push 放在 record_reminder_plan **之前** —— 冷却 / 已计划
        # 判定完全挡不住发送：8 分钟触发窗口 × 60s 轮询 = 窗口内每轮都重发
        # （用户报的「上午分别八次、下午分别七次」正是这么来的）。
        # 现在先逐目标过门控，只有 fresh 目标才配发消息；
        # ⛔ 任何人不得再把推送挪回门控之前。
        #   - 自动调度（manual=False）：本期已成功发送过的目标不再发
        #     （record_reminder_plan(resend_sent=False) 对 sent 行一律拒绝）；
        #     unsent / pending 按 5 分钟冷却重试（推送失败容错，⛔ 不是重复打扰）
        #   - 手动提醒（manual=True）：5 分钟冷却已过可重发（连点不轰炸）
        fresh = []
        for target in targets:
            allowed = state.record_reminder_plan(
                group_id=target.group_id, biz=target.biz,
                period_key=target.period_key, phone=target.phone,
                session=target.session, now=now,
                resend_sent=manual)
            if allowed:
                fresh.append(target)
            else:
                # 冷却中 / 本期已提醒过：⛔ 不重复打扰（消息也不会发）
                summary["skipped_cooled"] = summary.get("skipped_cooled", 0) + 1
        if not fresh:
            continue   # ⛔ 全员门控未过 -> 一条都不发
        # ⭐ 只发**一条**合并消息（2026-10-16 用户指令五）：@ 未完成的全体，
        # ⛔ 不按 target 逐条循环发（原实现会对每个未完成者各发一条 N 条消息）。
        # ⭐ 2026-10-19 用户指令：⏰ 截止时间统一 ——
        #    打卡 = 未结束时段的结束时刻；总结 / 报数 = 固定今晚 24:00。
        slot_names = {}
        if one == "checkin":
            from . import checkin as checkin_handler
            slot_names = {s["id"]: s["name"]
                          for s in checkin_handler.slots_from_row(group_row)}
            deadline_text = _checkin_deadline_text(group_row, now, fresh)
        else:
            deadline_text = "今晚 24:00"
        text = format_message(fresh, rule["text"], deadline_text, slot_names)
        # ⛔ 2026-10-17 用户指令：提醒推送**不再带平台 @**（mentionedMobileList）
        # —— 消息开头那个 @ 是量子密信按手机号渲染的，用户要求移除，
        # 只保留文案内「@姓名(上午/下午)」名单（format_message 已含，
        # 用户明确「保留一个就行了」）。⛔ 不得再把 mention_phones 加回来。
        try:
            ok, reason = push(group_id, text)
        except Exception as exc:  # 推送端口异常不当已发送
            ok, reason = False, "push error: %s" % exc
        for target in fresh:
            summary["new"] += 1
            if ok:
                state.mark_reminder_sent(group_id, target.biz,
                                         target.period_key,
                                         target.phone, target.session,
                                         now=now)
                summary["sent"] += 1
            else:
                state.mark_reminder_unsent(group_id, target.biz,
                                           target.period_key,
                                           target.phone, target.session,
                                           reason, now=now)
                summary["unsent"] += 1
    return summary


def tick(state: StateStore, backend: Backend, push, now: datetime) -> dict:
    """全部群一轮提醒计算 + 发送。push(group_id, text) -> (ok, reason)。

    ⛔ 不静默：后端不可用则整轮放弃；推送未接通记 unsent。
    单群细节（门控 / 冷却 / 落库）见 run_group。
    """
    summary = {"groups": 0, "targets": 0, "new": 0, "sent": 0,
               "unsent": 0, "errors": []}
    for group_row in state.list_groups():
        summary["groups"] += 1
        part = run_group(state, backend, push, group_row, now)
        for key in ("targets", "new", "sent", "unsent"):
            summary[key] += part[key]
        summary["errors"].extend(part["errors"])
    return summary
