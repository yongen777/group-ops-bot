# -*- coding: utf-8 -*-
"""成员级业务看板（2026-10-19 新增，配合 React 后台 1:1 复刻）。

一天 × 全员 × 三业务的两维矩阵：
- 行：群成员名单（手动维护）+ 名单外但当天有记录的人（⛔ 不让人凭空消失）
- 列：打卡（按本群配置的时间段动态展开）/ 总结 / 报数

硬约束：
- 纯读：只查 MCP 记录与本地名单，不写任何数据
- **查询失败标记 unknown，⛔ 不当成 0**（否则「没查到」会被显示成「全员未完成」，
  管理员据此补录会重复记账）
- 达标判定只算**本群已开启的业务**（关闭的业务不算未完成）

状态口径（⭐ 2026-10-20 用户指令，推翻旧「当天缺记录即待补录」）：

| 状态 | 含义 | 触发条件 |
|---|---|---|
| completed | 正常完成 | 有记录（打卡 LATE 仍算完成，另标「迟到补卡」） |
| backfilled | 已补录 | 有记录且 ADMIN_EDIT / entry_source=admin |
| overdue | 逾期（待补录） | **周期已结束**仍无记录 |
| reminded | 未完成（已提醒） | 周期未结束，且该人该业务本期**已推送过提醒**（status=sent） |
| waiting | 未完成（时段） | 打卡：当前时刻**尚未到本时段结束**（附时段区间） |
| pending | 未完成 | 周期未结束且没提醒过（总结/报数当天即此态） |

⚠️ 关键区别（用户原话口径）：**总结 / 报数当天没有截止时刻，当天永远不会
是「逾期」**——只有第二天回看昨天（或周报在下一周回看本周）才是逾期；
打卡则按**时段结束时刻**判定，未到点显示「未完成（08:00-12:00）」。
「已提醒」只认 `status='sent'`：unsent 是推送失败，群里没收到，⛔ 不算催过。

导出（CSV 单表）与页面同源：都走 build_board + board_rows，⛔ 不各算一套。
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Optional

from ..backend.base import Backend
from ..state import StateStore

# 默认两时段（群配置缺失时的兜底展示，⛔ 不参与写入判定）
_DEFAULT_SLOTS = [{"id": "am", "name": "上午", "start": "00:00",
                   "end": "12:00"},
                  {"id": "pm", "name": "下午", "start": "12:00",
                   "end": "23:59"}]

STATUS_TEXT = {"completed": "正常完成", "backfilled": "已补录",
               "overdue": "逾期（待补录）", "pending": "未完成",
               "reminded": "未完成（已提醒）", "waiting": "未完成"}

_QUERIES = {"checkin": "checkin_query_records",
            "summary": "summary_query_records",
            "report": "report_query_records"}


def _switches(row) -> dict:
    try:
        raw = row["biz_switches"] if "biz_switches" in row.keys() else None
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        data = {}
    return {"checkin": data.get("checkin", True),
            "summary": data.get("summary", True),
            "report": data.get("report", True)}


def _slots(row) -> list:
    """群配置 -> 时间段列表（兼容旧 {am,pm} 结构与配置缺失）。"""
    try:
        raw = (row["checkin_deadlines"]
               if "checkin_deadlines" in row.keys() else None)
        data = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        data = {}
    slots = data.get("slots") if isinstance(data, dict) else None
    if isinstance(slots, list) and slots:
        out = []
        for item in slots:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            out.append({"id": str(item["id"]),
                        "name": str(item.get("name") or item["id"]),
                        "start": str(item.get("start") or ""),
                        "end": str(item.get("end") or "")})
        if out:
            return out
    if isinstance(data, dict) and ("am" in data or "pm" in data):
        return [{"id": "am", "name": "上午", "start": "00:00",
                 "end": str(data.get("am") or "12:00")},
                {"id": "pm", "name": "下午", "start": "12:00",
                 "end": str(data.get("pm") or "23:59")}]
    return [dict(s) for s in _DEFAULT_SLOTS]


def _clock(value) -> str:
    """ISO 时间 -> HH:MM:SS（展示口径；解析不了就原样返回）。"""
    text = str(value or "")
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.split("+", 1)[0].split("Z", 1)[0]
    return text[:8] if len(text) >= 8 else text


def _is_past(date_str: str, today: str) -> bool:
    return date_str < today


def _minute_of_day(hhmm) -> Optional[int]:
    """'HH:MM' -> 当天第几分钟；非法 / 缺失返回 None（⛔ 不猜）。"""
    from ..period import parse_hhmm
    try:
        parsed = parse_hhmm(str(hhmm or ""))
    except Exception:  # pragma: no cover - 脏配置不该炸整张表
        parsed = None
    if not parsed:
        return None
    hour, minute = parsed
    return hour * 60 + minute


def _slot_due(slot: dict, now_minute: Optional[int]) -> bool:
    """该时段是否**已过结束时刻**（跨零点时段按区间判定）。

    判不出来（配置非法 / 无当前时刻）一律当作「未到点」——宁可显示
    「未完成（时段）」，也不把能补的时段误报成逾期。
    """
    if now_minute is None:
        return False
    start = _minute_of_day(slot.get("start"))
    end = _minute_of_day(slot.get("end"))
    if end is None:
        return False
    if start is not None and end < start:      # 跨零点：如 22:00-02:00
        return end <= now_minute < start
    return now_minute >= end


def _window_text(slot: dict) -> str:
    start = str(slot.get("start") or "").strip()
    end = str(slot.get("end") or "").strip()
    if start and end:
        return "%s-%s" % (start[:5], end[:5])
    return start or end


def _period_end(date_str: str, period_type: str) -> str:
    """周期最后一天：日报 = 当天；周报 = 该 ISO 周的周日。"""
    if period_type != "weekly":
        return date_str
    try:
        from ..period import parse_daily_key
        day = parse_daily_key(date_str)
    except (ValueError, TypeError):  # pragma: no cover - 路由已校验格式
        return date_str
    return (day + timedelta(days=6 - day.weekday())).date().isoformat()


def _missing_status(*, biz: str, slot: Optional[dict], date_str: str,
                    today: str, now_minute: Optional[int],
                    reminded: bool, overdue: bool) -> str:
    """无记录时的状态（⭐ 2026-10-20 用户口径，见模块 docstring 状态表）。

    优先级：逾期 > 已提醒 > 未到点（打卡）> 未完成。
    - overdue 由调用方按「周期是否结束」算好传入（总结按周期、打卡按时段）
    - reminded 只对**周期未结束**生效：已经过期该催的是补录，不是再提醒
    """
    if overdue:
        return "overdue"
    if reminded:
        return "reminded"
    if biz == "checkin" and slot is not None and not _slot_due(slot, now_minute):
        return "waiting"
    return "pending"


def _checkin_cell(rec, slot_id: str) -> Optional[dict]:
    raw_status = str(rec.get("status") or "")
    if raw_status == "ADMIN_EDIT":
        status = "backfilled"
        method = "后台补录"
    else:
        status = "completed"
        method = "手机拍照上传" if rec.get("image_name") else "手动打卡"
    return {"time": _clock(rec.get("checkin_time")),
            "status": status,
            "method": method,
            "raw_status": raw_status,
            # D17：后台凭证缩略图（图片本体在机器人数据目录，用
            # /api/admin/uploads/{ref} 鉴权读取；⛔ 不走静态目录）
            "image_ref": str(rec.get("image_ref") or ""),
            "image_name": str(rec.get("image_name") or ""),
            "window": "",
            "reminded": False}


def _summary_cell(rec) -> dict:
    content = str(rec.get("content") or "")
    return {"content": content,
            "words": len(content),
            # 2026-10-19 用户指令：总结/报数时间格式与打卡统一（HH:MM:SS），
            # ⛔ 不再把后端 ISO 原样丢给前端
            "at": _clock(rec.get("updated_time") or rec.get("created_time")),
            "entry_source": str(rec.get("entry_source") or ""),
            "period_type": str(rec.get("period_type") or "")}


def _report_cell(rec) -> dict:
    items = []
    total = 0.0
    for item in rec.get("items") or []:
        try:
            qty = float(item.get("quantity") or 0)
        except (TypeError, ValueError):
            qty = 0.0
        total += qty
        items.append({"field_id": item.get("field_id"),
                      "field_name": str(item.get("field_name") or ""),
                      "unit": str(item.get("unit") or ""),
                      "quantity": item.get("quantity")})
    return {"items": items,
            "total": int(total) if float(total).is_integer() else total,
            "at": _clock(rec.get("created_time") or rec.get("updated_time")),
            "entry_source": str(rec.get("entry_source") or "")}


def build_board(state: StateStore, backend: Backend, group_id: str,
                date_str: str, *, today: Optional[str] = None,
                now=None) -> dict:
    """构建一天 × 全员 × 三业务的看板（纯读）。

    now：可注入的当前时刻（测试用；缺省取 period.now()）。
    today：可注入的「今天」（跨零点 / 跨时区测试用）。
    """
    row = state.get_group_config(group_id)
    if row is None:
        raise LookupError("group_not_found")

    from ..period import parse_daily_key, period_key as _period_key
    from .. import period as period_mod

    switches = _switches(row)
    period_type = (row["period_type"] if "period_type" in row.keys()
                   and row["period_type"] else "daily")
    slots = _slots(row)
    group_name = (row["group_name"] if "group_name" in row.keys()
                  and row["group_name"] else group_id)
    status = (row["status"] if "status" in row.keys()
              and row["status"] else "active")

    now = now or period_mod.now()
    today = today or now.date().isoformat()
    now_minute = now.hour * 60 + now.minute
    _dt = parse_daily_key(date_str)
    period_keys = {"checkin": date_str, "report": date_str,
                   "summary": _period_key(period_type, _dt)}

    # ---- 三业务取数（单业务失败只标 unknown，不拖垮整张表） ----
    unknown = {"checkin": False, "summary": False, "report": False}
    records: dict = {}
    for biz, method_name in _QUERIES.items():
        try:
            query = getattr(backend, method_name)
            payload = query(group_id=group_id,
                            period_key=period_keys[biz]) or {}
            records[biz] = payload.get("records") or []
        except Exception:  # pragma: no cover - 后端不可用属运行时情形
            records[biz] = []
            unknown[biz] = True

    # ---- 已送达的提醒（「未完成（已提醒）」的唯一判据） ----
    # ⛔ 取不到就按「没提醒过」渲染：宁可少标已提醒，也不谎报催过。
    sent_sessions: set = set()
    sent_phones = {"summary": set(), "report": set()}
    try:
        for item in state.list_sent_reminders(group_id, "checkin",
                                              period_keys["checkin"]):
            sent_sessions.add((str(item["phone"]),
                               str(item["session"] or "")))
    except Exception:  # pragma: no cover - 只读兜底
        pass
    for biz in ("summary", "report"):
        try:
            sent_phones[biz] = {str(item["phone"]) for item in
                                state.list_sent_reminders(
                                    group_id, biz, period_keys[biz])}
        except Exception:  # pragma: no cover
            sent_phones[biz] = set()

    # ---- 名单（含停用成员：历史记录仍要能对上号） ----
    roster = {}
    try:
        for item in state.list_member_tasks(group_id, enabled_only=False):
            uid = str(item["user_id"])
            roster[uid] = {"user_id": uid,
                           "nickname": str(item["nickname"] or ""),
                           "role": str(item["role"] or ""),
                           "enabled": bool(item["enabled"])}
    except Exception:  # pragma: no cover
        pass
    # ⭐ 2026-10-20（P0-4）：分母先落定，分子只数「在岗考核」的人。
    # enabled_total == 0（全员未配置 / 全员暂停）时保留旧兜底口径 ——
    # 否则分子恒 0，页面会出现「0/0 已完成」的假象。
    enabled_total = sum(1 for item in roster.values() if item["enabled"])

    # ---- 记录归并 ----
    checkin_map: dict = {}
    for rec in records["checkin"]:
        phone = str(rec.get("phone") or "")
        if not phone:
            continue
        session = str(rec.get("session") or "")
        checkin_map.setdefault(phone, {})[session] = rec

    summary_map = {str(r.get("phone")): r for r in records["summary"]
                   if r.get("phone")}
    report_map = {str(r.get("phone")): r for r in records["report"]
                  if r.get("phone")}

    # ---- 行集合：名单优先，再接名单外但当天有记录的人 ----
    order = list(roster.keys())
    extras = set(checkin_map) | set(summary_map) | set(report_map)
    for phone in sorted(extras - set(order)):
        order.append(phone)

    # ---- 周期是否已结束（决定「逾期」还是「未完成」） ----
    summary_over = today > _period_end(date_str, period_type)
    report_over = today > _period_end(date_str, "daily")
    day_past = _is_past(date_str, today)

    members = []
    checkin_done = summary_done = report_done = 0
    for phone in order:
        base = roster.get(phone)
        cells = checkin_map.get(phone, {})
        member = {
            "user_id": phone,
            "nickname": (base or {}).get("nickname")
            or _name_of(cells, summary_map, report_map, phone),
            "role": (base or {}).get("role") or "",
            "enabled": bool((base or {}).get("enabled", True)),
            "in_roster": base is not None,
            "checkin": {},
        }
        slot_done = slot_seen = 0
        for slot in slots:
            rec = cells.get(slot["id"])
            if rec is None:
                reminded = (phone, str(slot["id"])) in sent_sessions
                # 打卡的「逾期」= 当天且该时段已过结束时刻（或整体是过去日期）
                st = _missing_status(
                    biz="checkin", slot=slot, date_str=date_str, today=today,
                    now_minute=now_minute, reminded=reminded,
                    overdue=day_past or (date_str == today
                                         and _slot_due(slot, now_minute)))
                member["checkin"][slot["id"]] = {
                    "time": "", "status": st, "method": "",
                    "raw_status": "", "image_ref": "", "image_name": "",
                    "window": _window_text(slot), "reminded": reminded}
                slot_seen += 1
            else:
                cell = _checkin_cell(rec, slot["id"])
                cell["window"] = _window_text(slot)
                cell["reminded"] = (phone, str(slot["id"])) in sent_sessions
                member["checkin"][slot["id"]] = cell
                slot_done += 1
                slot_seen += 1
        if slot_seen and slot_done == slot_seen:
            # 全部时段都有记录才算整体完成（缺一个时段就是未完成）
            member["checkin_status"] = (
                "backfilled"
                if all(c["status"] == "backfilled"
                       for c in member["checkin"].values())
                else "completed")
            # ⭐ 2026-10-20（P0-4）：已暂停考核的成员提交了也⛔ 不计入分子
            # （否则分子可能 > 分母 enabled_total，页面显示「3/2 已完成」）。
            if member["enabled"] or not enabled_total:
                checkin_done += 1
        else:
            states = [c["status"] for c in member["checkin"].values()]
            if day_past or (date_str == today
                            and all(_slot_due(s, now_minute) for s in slots)):
                member["checkin_status"] = "overdue"
            elif "reminded" in states:
                member["checkin_status"] = "reminded"
            else:
                # 还有时段没到点 -> 整体「未完成」（不是待补录）
                member["checkin_status"] = "pending"

        srec = summary_map.get(phone)
        if srec is not None:
            member["summary"] = _summary_cell(srec)
            member["summary_status"] = ("backfilled"
                                        if str(srec.get("entry_source"))
                                        == "admin" else "completed")
            if member["enabled"] or not enabled_total:
                summary_done += 1
        else:
            member["summary"] = None
            member["summary_status"] = _missing_status(
                biz="summary", slot=None, date_str=date_str, today=today,
                now_minute=now_minute,
                reminded=phone in sent_phones["summary"],
                overdue=summary_over)

        rrec = report_map.get(phone)
        if rrec is not None:
            member["report"] = _report_cell(rrec)
            member["report_status"] = ("backfilled"
                                       if str(rrec.get("entry_source"))
                                       == "admin" else "completed")
            if member["enabled"] or not enabled_total:
                report_done += 1
        else:
            member["report"] = None
            member["report_status"] = _missing_status(
                biz="report", slot=None, date_str=date_str, today=today,
                now_minute=now_minute,
                reminded=phone in sent_phones["report"],
                overdue=report_over)

        members.append(member)

    return {
        "ok": True,
        "date": date_str,
        "period_keys": period_keys,
        "period_type": period_type,
        "group": {"group_id": group_id, "group_name": group_name,
                  "status": status, "switches": switches},
        "slots": slots,
        "members": members,
        "counts": {"members": len(members),
                   "total": enabled_total or len(members),
                   "checkin_done": checkin_done,
                   "summary_done": summary_done,
                   "report_done": report_done},
        "unknown": unknown,
    }


def _name_of(cells, summary_map, report_map, phone) -> str:
    for rec in (cells or {}).values():
        if rec.get("name"):
            return str(rec["name"])
    for table in (summary_map, report_map):
        rec = table.get(phone)
        if rec and rec.get("name"):
            return str(rec["name"])
    return phone


def _today() -> str:
    from ..period import now as _now
    return _now().date().isoformat()


def _achieve(member: dict, switches: dict, slots: list) -> str:
    """全天综合达标判定：只算已开启业务。

    ⭐ D31 修复 4（2026-10-21）：**暂停考核**的成员不按「未打卡」记账 ——
    他们本来就没有考核义务，写成「上午未打卡、总结未提交…」等于把人算成
    缺勤。口径与看板 `enabled_total` 一致：
    - 已暂停考核 + 各项都提交了 -> 仍判「今日达标」（记录真实完成情况）
    - 已暂停考核 + 有缺项     -> **「已暂停考核」**（⛔ 不列缺项）
    """
    pending = []
    if switches.get("checkin", True):
        for slot in slots:
            cell = (member.get("checkin") or {}).get(slot["id"]) or {}
            if not cell.get("time"):
                pending.append("%s未打卡" % slot["name"])
    if switches.get("summary", True) and not member.get("summary"):
        pending.append("总结未提交")
    if switches.get("report", True) and not member.get("report"):
        pending.append("报数未提交")
    if not pending:
        return "今日达标"
    if member.get("enabled") is False:
        return "已暂停考核"
    return "、".join(pending)


def board_rows(board: dict) -> tuple:
    """看板 -> (表头, 数据行)，用于「全天全部业务」单表导出。"""
    slots = board.get("slots") or []
    switches = (board.get("group") or {}).get("switches") or {}
    group_name = (board.get("group") or {}).get("group_name") or ""
    headers = ["群聊名称", "成员姓名", "手机号码", "岗位角色"]
    for slot in slots:
        headers.append("%s打卡时间" % slot["name"])
        headers.append("%s打卡状态" % slot["name"])
    headers += ["今日总结提交时间", "今日总结字数", "今日总结正文",
                "今日报数提交时间", "今日报数量化指标", "报数总项数",
                "全天综合达标判定"]

    rows = []
    for member in board.get("members") or []:
        row = [group_name, member.get("nickname") or "",
               member.get("user_id") or "", member.get("role") or ""]
        for slot in slots:
            cell = (member.get("checkin") or {}).get(slot["id"]) or {}
            time_text = cell.get("time") or ""
            status = cell.get("status") or ""
            if time_text:
                # 迟到补卡如实标注（⛔ 不把 LATE 说成正常完成）
                label = ("迟到补卡" if cell.get("raw_status") == "LATE"
                         else STATUS_TEXT.get(status, status))
            elif status == "waiting":
                # 打卡未到点：把时段区间一并写出（导出表里也要能看懂）
                label = "未完成（%s）" % (cell.get("window") or "未到时段")
            else:
                label = STATUS_TEXT.get(status, "未完成")
            row.append(time_text or "未打卡")
            row.append(label)

        summary = member.get("summary")
        if summary:
            row.append(_cell_clock(summary.get("at")))
            row.append(summary.get("words") or 0)
            row.append(summary.get("content") or "")
        else:
            row.append("")
            row.append("")
            row.append("")

        report = member.get("report")
        if report:
            row.append(_cell_clock(report.get("at")))
            row.append("；".join(
                "%s%s%s" % (item.get("field_name") or "",
                            item.get("quantity"),
                            item.get("unit") or "")
                for item in report.get("items") or []))
            row.append(len(report.get("items") or []))
        else:
            row.append("")
            row.append("")
            row.append("")

        row.append(_achieve(member, switches, slots))
        rows.append(row)
    return headers, rows


def _cell_clock(value) -> str:
    """ISO 时间 -> MM-DD HH:MM（导出表格里的提交时刻）。"""
    text = str(value or "")
    if "T" not in text:
        return text
    date_part, time_part = text.split("T", 1)
    time_part = time_part.split("+", 1)[0].split("Z", 1)[0]
    return "%s %s" % (date_part[5:], time_part[:5])
