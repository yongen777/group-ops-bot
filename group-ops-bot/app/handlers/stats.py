# -*- coding: utf-8 -*-
"""统计导出处理器（§4.6 / Q-BIZ3 / 2026-10-16 用户指令三.1）。

⭐ **一键导出全部业务**（2026-10-16 用户指令三.1，⛔ 推翻旧「四命令独立导」）：
- 唯一入口 = 「导出」两个字（精确匹配）；一次导出**打卡 / 总结 / 报数**
  **三个独立 XLSX 文件**（每业务一个文件、按业务英文名单独命名），
  ⛔ 不再拆四个命令，也⛔ 不再合成一个多 sheet 文件（2026-10-20 用户指令：
  量子密信电脑端在线预览只显示第一张表，合成文件会让人以为「只有打卡」）
- ⭐ **表格口径 = 全员视角**（2026-10-20 用户指令）：每张表都列出**应交名单
  全员**，未提交的人也有一行并标注「未提交 / 未到时段」，一眼看得出今天谁没交；
  ⛔ 不是只列已提交的记录（那样看不出缺谁）
- 日报与周报**同为总结内容**，只是数据量不同：总结 sheet 按**群配置的周期**
  取数（daily -> 当日、weekly -> 当前 ISO 周），⛔ 不拆日报/周报两个命令
- 权限（2026-10-19 用户指令：单权化）：⛔ 推翻 v4 按业务三权分权——
  白名单权限只有 **ADMIN_ALL** 一种（一键 = 全部管理员操作权限），
  授权对象限本群应交成员（后台从群成员下拉选择）。具备即导，
  不具备即拒，⛔ 不做"部分导出"以免口径含糊
- 报数 sheet **动态字段**：只展示有人填的字段（按 field_id 归并），列名取
  提交时快照（字段历史快照，⛔ 不随字段池后续改名而变）
- 历史数据保留：业务开关关闭不影响导出（导的是历史账）

⭐ **群内投递**（2026-10-20，D12 解除）：平台 file 消息通道已实测接通
（上传取 fileId -> ``{"type":"file","fileMsg":{"fileId":…}}``），
本模块导出后把 xlsx 直接发到群里，**全员可见可下载**。
⛔ 发送失败不当已送达；失败话术也⛔ 不再粘贴服务器绝对路径
（属内部信息，路径只进日志）。
"""

from __future__ import annotations

import io
import logging
import threading
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .. import commands
from .. import period as period_mod
from .base import HandlerContext, group_period_type

LOG = logging.getLogger("group-ops.handlers.stats")

BIZ = "stats"

# 导出所需白名单权限（2026-10-19 用户指令：单权化，⛔ 推翻旧三权分权）
EXPORT_PERMISSION = "ADMIN_ALL"
EXPORT_LABEL = {"checkin": "打卡", "summary": "总结", "report": "报数"}

NOT_IN_WHITELIST_TEXT = "您没有导出权限，请联系后台管理员授予权限。"
QUERY_FAILED_TEXT = "统计查询失败（结果未知），请稍后重试"
# 发送失败：⛔ 不粘贴服务器路径（内部信息不该出现在群里），路径只进日志
EXPORT_SEND_FAILED_TEXT = ("统计表已生成，但本次发送到群聊失败，请稍后重试；"
                           "仍失败请到管理后台「统计中心」下载导出。")
# ⭐ 2026-10-20 用户指令：群内回执统一为「@所有人」文本 + 固定文案，
# 且这条回执**不 @ 提问者**（见 server.py 的 broadcast 分支）。
EXPORT_SENT_TEXT = ("@所有人 📊 最新群业务统计表已生成，文件已发送至群聊"
                    "（包含打卡、总结、报数数据），请全体成员查阅。")
# 部分成功：如实说清，⛔ 不能因为发出去一个就报「全部已发送」
EXPORT_PARTIAL_TEXT = ("统计表已生成，%d 个文件发送成功、%d 个发送失败，"
                       "请稍后重试；仍失败请到管理后台「统计中心」下载导出。")
# 群内文件名：**业务英文 + 时间**（2026-10-20 用户指令：三个业务各一个文件，
# 单独命名）。示例：checkin_20261020_1115.xlsx
FILE_STEM = "%s_%s"
# 一个业务一个文件（顺序 = 导出与发送顺序）
BIZ_FILE = {"checkin": "打卡", "summary": "总结", "report": "报数"}
# 中文标签与后台导出（app/admin/stats_export.py）**同值**：
# 群成员收到的文件与管理员下载的文件必须能互相对上，
# ⛔ 改一处必须同步另一处（test_stats_phase6 有断言锁相等）。
SESSION_LABEL = {"am": "上午", "pm": "下午"}
STATUS_LABEL = {"NORMAL": "按时", "LATE": "迟到", "ADMIN_EDIT": "后台补录"}
SOURCE_LABEL = {"user": "群员", "admin": "管理员"}

# ⭐ D31 修复 1（2026-10-21）：群内导出的**进程内互斥锁**。
# 落盘文件名只按 `{biz}_{YYYYMMDD_HHMM}.xlsx` 命名（⛔ 不含 group_id，保持
# 既有命名口径不动），而同一次上传+发送要 7~28 秒。两个群在同一分钟并发
# 「导出」时，后写的一方会覆写磁盘上同名文件 → A 群收到 B 群的表（串表）。
# 锁粒度 = 「生成三个文件 + 逐个发送」的整段：拿到锁的群做完三件再放行。
# ⚠️ 单进程串行化，多个 uvicorn worker 时仍需按进程锁（本项目单进程双监听）。
_EXPORT_LOCK = threading.Lock()


def handle_command(parsed, ctx: HandlerContext) -> str:
    """「导出」入口：一键导出全部业务（打卡 / 总结 / 报数）——**各一个 XLSX**。

    ⭐ 2026-10-20 用户指令：三个业务三个文件（按业务英文名 + 时间命名）。
    理由：量子密信电脑端打不开 CSV；而合成一个多 sheet 的 xlsx，在线预览
    只显示第一张表，用户会以为「只导出了打卡」。
    """
    # ---- 权限：单权 ADMIN_ALL（2026-10-19 用户指令，⛔ fail-closed）----
    if not ctx.state.has_whitelist_permission(ctx.group_id, ctx.phone,
                                              EXPORT_PERMISSION):
        return NOT_IN_WHITELIST_TEXT

    # ---- 取数：打卡 / 报数 = 当日；总结 = 群配置周期（日报当日 / 周报本周）----
    summary_ptype = group_period_type(ctx)
    records = {}
    periods = {}
    for biz in commands.EXPORT_ALL_BIZS:
        period_k = _period_for(biz, summary_ptype, ctx.now)
        try:
            records[biz] = _query_records(ctx, biz, period_k)
        except Exception:
            # 查询类失败：⛔ 不猜数据、不生成空表冒充成功
            return QUERY_FAILED_TEXT
        periods[biz] = period_k

    out_dir = Path(ctx.config.data_dir) / "exports"
    # 三个文件共用同一个时间戳，便于成组识别
    stamp = ctx.now.strftime("%Y%m%d_%H%M")
    sender = getattr(ctx.sender, "send_file", None)
    # ⭐ 时段显示名按本群配置解析（自定义时段如「晚班」必须显真名）
    slot_labels = _slot_labels(ctx)
    # ⭐ 全员视角：名单 + 时段配置（未提交的人也进表，否则看不出缺谁）
    roster = _roster(ctx)
    slots = _configured_slots(ctx)

    sent = 0
    failed = 0
    # ⭐ 2026-10-20 用户指令：群若配成「免图直提」，凭证列整列不出现
    from . import checkin as checkin_handler
    require_image = checkin_handler.require_image(ctx)
    # ⭐ D31 修复 1：落盘 + 发送整段加锁 —— 文件名不含 group_id，
    # 多群同分钟并发会覆写同名文件、把别人的表发进本群（串表）。
    with _EXPORT_LOCK:
        out_dir.mkdir(parents=True, exist_ok=True)
        for biz in commands.EXPORT_ALL_BIZS:
            path = out_dir / (FILE_STEM % (biz, stamp) + ".xlsx")
            path.write_bytes(build_export_workbook(
                biz, summary_ptype, periods[biz], records[biz], slot_labels,
                roster=roster, slots=slots, now=ctx.now,
                require_image=require_image))
            if _try_send_file(sender, ctx.group_id, path):
                sent += 1
            else:
                failed += 1

    if failed == 0:
        return EXPORT_SENT_TEXT
    if sent == 0:
        return EXPORT_SEND_FAILED_TEXT
    return EXPORT_PARTIAL_TEXT % (sent, failed)


def is_broadcast_receipt(text: str) -> bool:
    """是否是「面向全员」的回执（⭐ 2026-10-20 用户指令）。

    导出成功的回执只 @所有人，⛔ 不再额外 @ 提问者 —— 那条消息是发给大家
    看的，不是回复某个人。失败类回执仍要 @ 提问者（他得知道没发出去）。
    """
    return (text or "").strip() == EXPORT_SENT_TEXT


def _roster(ctx: HandlerContext) -> list:
    """本群应交名单（enabled=1）：[{phone, name}]，按名单顺序。

    读不到名单不影响导出主流程（回落「只列有记录的人」），但要记日志。
    """
    try:
        # ⚠️ list_member_tasks 返回 sqlite3.Row（⛔ 没有 .get），只能用下标
        return [{"phone": str(row["user_id"] or ""),
                 "name": str(row["nickname"] or "")}
                for row in ctx.state.list_member_tasks(ctx.group_id,
                                                       enabled_only=True)
                if row["user_id"]]
    except Exception:
        LOG.warning("export roster fallback: group=%s", ctx.group_id,
                    exc_info=True)
        return []


def _configured_slots(ctx: HandlerContext) -> list:
    """本群打卡时段配置：[{id, name, start, end}]；读不到回落空表。"""
    try:
        from . import checkin as checkin_handler
        return list(checkin_handler.configured_slots(ctx))
    except Exception:
        LOG.warning("export slots fallback: group=%s", ctx.group_id,
                    exc_info=True)
        return []


def _try_send_file(sender, group_id: str, path: Path) -> bool:
    """把文件发到群里；失败只记日志（含服务器路径），⛔ 不进群聊话术。"""
    if sender is None:
        # 无适配器注入（旧测试/裸调用）：与发送失败同口径
        LOG.warning("export file send skipped: no sender.send_file injected "
                    "group=%s path=%s", group_id, path)
        return False
    try:
        sent, reason = sender(group_id, path, filename=path.name)
    except Exception as exc:  # 适配器异常不当已发送
        sent, reason = False, "file send error: %s" % exc
    if sent:
        return True
    LOG.warning("export file send failed: group=%s name=%s path=%s "
                "reason=%s", group_id, path.name, path, reason)
    return False


def _period_for(biz: str, summary_ptype, now) -> str:
    """导出周期：打卡 / 报数 = 当日；总结 = 群配置周期（§2.3）。"""
    if biz == "summary":
        return period_mod.period_key(summary_ptype, now)
    return period_mod.daily_key(now)


def _slot_labels(ctx: HandlerContext) -> dict:
    """本群时段 id -> 显示名（⭐ 2026-10-20 P1-2）。

    旧实现把 session 硬编码成「am=上午 / 其余=下午」，自定义时段（slot_3
    之类的「晚班」）一律被写成「下午」—— 多群各自配时段时导出表直接错。
    现在优先取本群 checkin_slots 配置里的 name；未命中回落产品默认标签，
    再命中不了就原样输出 session 标识（⛔ 不猜）。
    """
    labels = dict(SESSION_LABEL)
    try:
        from . import checkin as checkin_handler
        for slot in checkin_handler.configured_slots(ctx):
            sid = str(slot.get("id") or "").strip()
            name = str(slot.get("name") or "").strip()
            if sid and name:
                labels[sid] = name
    except Exception:  # 配置读不到不影响导出主流程（回落默认标签）
        LOG.warning("slot labels fallback: group=%s", ctx.group_id,
                    exc_info=True)
    return labels


def _query_records(ctx: HandlerContext, biz: str, period_k: str) -> list:
    """按业务查一个周期的记录（⛔ 不跨业务混查）。"""
    query = {"checkin": ctx.backend.checkin_query_records,
             "summary": ctx.backend.summary_query_records,
             "report": ctx.backend.report_query_records}[biz]
    return query(group_id=ctx.group_id,
                 period_key=period_k).get("records", [])


def _fmt_time(value) -> str:
    """时间原样展示成人能读的形态：2026-10-20T09:39:58.879 -> 2026-10-20 09:39:58。"""
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("T", " ").split(".")[0]


def _fmt_clock(value) -> str:
    """只取时刻（09:39:58）。打卡表一行一时段，日期由文件名/表内周期表达，
    单元格里再塞一遍日期会把列撑得很宽（⭐ 2026-10-20 用户指令：合并成一行）。"""
    text = _fmt_time(value)
    if not text:
        return ""
    parts = text.split(" ")
    return parts[1] if len(parts) > 1 else text


def _num(value):
    """数量按**数值**写进单元格（Excel 里能直接求和/排序）。

    ⛔ 不要写成字符串 —— 之前 `_fmt_qty` 返回 "3"，Excel 会当成文本，
    整列「求和」直接是 0，群里看表的人会以为数据错了。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    return int(num) if num.is_integer() else num


# ---- 表格视觉：让群里下载的人一眼看清（2026-10-20 用户指令「一目了然」）----
_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
_PENDING_FILL = PatternFill("solid", fgColor="FDE8E8")   # 未提交：整行浅红
_CENTER = Alignment(horizontal="center", vertical="center")
_WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)

STATE_DONE = "已提交"
STATE_MISSING = "未提交"
# ⭐ 2026-10-20 用户指令：状态文案要明确是「打卡」这件事
# （下午还没到点 -> 「未到打卡时间段」，⛔ 不是含糊的「未到时段」）
STATE_NOT_STARTED = "未到打卡时间段"
# ⭐ 2026-10-20 用户指令：报数已提交、但某个字段没填 -> 显式写「未选择」
REPORT_UNSELECTED = "未选择"
_PERIOD_TYPE_LABEL = {"daily": "日报", "weekly": "周报"}


def build_export_workbook(biz: str, period_type, period_key: str,
                          records: list, slot_labels: dict = None,
                          roster: list = None, slots: list = None,
                          now=None, require_image: bool = True) -> bytes:
    """生成**单业务** XLSX（纯函数，便于测试）。

    ⭐ 2026-10-20 用户指令：三个业务各一个文件。
    ⭐ 表格口径 = **全员视角**：应交名单里的人全部出现在表里，
    没交的那一行标「未提交」并整行浅红，一眼看出今天谁没交。

    - checkin：每人 × 每时段一行
    - summary / report：每人一行
    - report：动态字段列，只展示有人填的字段（列名取提交时快照）

    roster 为空时回落「只列有记录的人」（旧调用/后台单业务导出），
    ⛔ 但列结构不变 —— 全系统只有这一套表格格式。
    """
    wb = Workbook()
    ws = wb.active
    _fill_sheet(ws, biz, period_type, records, slot_labels,
                roster=roster, slots=slots, now=now, period_key=period_key,
                require_image=require_image)
    _decorate(ws)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _fill_sheet(ws, biz: str, period_type, records: list,
                slot_labels: dict = None, roster: list = None,
                slots: list = None, now=None, period_key: str = "",
                require_image: bool = True) -> None:
    """把一个业务写进给定 sheet（单业务与一键导出共用 ⛔ 单一真相）。"""
    if biz == "checkin":
        _fill_checkin(ws, records, slot_labels, roster, slots, now,
                      require_image)
    elif biz == "summary":
        _fill_summary(ws, records, period_type, roster, period_key, now)
    else:
        _fill_report(ws, records, roster, period_key)


# ---------- 打卡：每人一行，每个时段一列（未提交也在表里） ----------

def _fill_checkin(ws, records: list, slot_labels, roster, slots, now,
                  require_image: bool = True) -> None:
    """一人一行，每时段一列。

    ⭐ 2026-10-20 用户指令：同一成员的多个时段**合并成同一行**
    （旧实现是「一人 × 每时段一行」，同一个人会重复出现两行）。
    ⛔ 不再有「提交状态 / 考核结果 / 录入来源」列 —— 打卡时间本身就是提交证据，
    迟到 / 补录直接标在时间后面；凭证列只说有没有交（⛔ 不显示文件名）。
    """
    ws.title = "打卡"
    labels = dict(SESSION_LABEL)
    labels.update(slot_labels or {})
    slot_list = _slot_list(slots, records, labels)

    header = ["序号", "手机号", "姓名"] + [s["name"] for s in slot_list]
    if require_image:
        header.append("凭证图片")
    header.append("备注")
    ws.append(header)

    index = {}
    for rec in records:
        key = (str(rec.get("phone") or ""), str(rec.get("session") or ""))
        index[key] = rec

    people = roster or _people_from(records)
    listed = {str(p.get("phone") or "") for p in people}

    rows = []
    for person in people:
        rows.append(_checkin_row(person, slot_list, index, now,
                                 require_image, ""))
    # 有记录但不在应交名单的人：照样列出来，⛔ 不让数据在表里消失
    for rec in records:
        phone = str(rec.get("phone") or "")
        if phone in listed:
            continue
        rows.append(_checkin_row(
            {"phone": phone, "name": str(rec.get("name") or "")},
            slot_list, index, now, require_image, "不在应交名单"))

    for seq, row in enumerate(rows, start=1):
        ws.append([seq] + row)


def _checkin_row(person, slot_list, index, now, require_image,
                 note_extra="") -> list:
    """一人一行：每个时段一列（打卡时间 / 未提交 / 未到打卡时间段）。"""
    cells = []
    notes = []
    image_flags = []
    any_done = False
    for slot in slot_list:
        rec = index.get((person["phone"], str(slot.get("id") or "")))
        if rec:
            any_done = True
            image_flags.append(bool(rec.get("image_name")))
            cells.append(_checkin_cell(rec))
            continue
        state, note = _missing_state(slot, now)
        cells.append(state)
        if note and note not in notes:
            notes.append(note)
    row = [person["phone"], person.get("name") or ""] + cells
    if require_image:
        # ⭐ 用户指令：凭证列只说「已提交 / 未提交」，⛔ 不显示图片文件名
        complete = bool(image_flags) and all(image_flags)
        row.append(STATE_DONE if complete else STATE_MISSING)
        if any_done and not complete:
            notes.append("未上传凭证")
    if note_extra:
        notes.insert(0, note_extra)
    row.append("；".join(notes))
    return row


def _checkin_cell(rec) -> str:
    """已提交的时段单元格：时刻 + 必要标注（迟到 / 补录）。"""
    text = _fmt_clock(rec.get("checkin_time"))
    marks = []
    if str(rec.get("status") or "") == "LATE":
        marks.append("迟到")
    if str(rec.get("entry_source") or "") == "admin":
        marks.append("补录")
    return "%s（%s）" % (text, "、".join(marks)) if marks else text


def _missing_state(slot, now) -> tuple:
    """未提交时区分「未到打卡时间段 / 时段进行中 / 时段已结束」。

    状态本身已经把「还没到点」说清楚了，备注列 ⛔ 不再重复写「时段未开始」。
    """
    start = str(slot.get("start") or "")
    end = str(slot.get("end") or "")
    hhmm = now.strftime("%H:%M") if now is not None else ""
    if hhmm and start and hhmm < start:
        return STATE_NOT_STARTED, ""
    if hhmm and end and hhmm > end:
        return STATE_MISSING, "时段已结束"
    return STATE_MISSING, ""


def _slot_list(slots, records: list, labels: dict) -> list:
    """导出用时段序列：优先本群配置；无配置时用记录里出现过的时段。"""
    if slots:
        return [{"id": str(s.get("id") or ""),
                 "name": str(s.get("name") or labels.get(str(s.get("id") or ""),
                                                         s.get("id") or "")),
                 "start": str(s.get("start") or ""),
                 "end": str(s.get("end") or "")}
                for s in slots if s.get("id")]
    seen = []
    for rec in records:
        sid = str(rec.get("session") or "")
        if sid and sid not in seen:
            seen.append(sid)
    ordered = [s for s in ("am", "pm") if s in seen] + \
              [s for s in seen if s not in ("am", "pm")]
    return [{"id": sid, "name": labels.get(sid, sid), "start": "", "end": ""}
            for sid in ordered]


# ---------- 总结 / 报数：每人一行 ----------

def _fill_summary(ws, records: list, period_type, roster, period_key,
                  now) -> None:
    ws.title = "总结"
    ws.append(["序号", "提交状态", "手机号", "姓名", "周期类型", "周期",
               "提交时间", "内容", "备注"])

    by_phone = {}
    for rec in records:
        by_phone[str(rec.get("phone") or "")] = rec

    people = roster or _people_from(records)
    listed = {str(p.get("phone") or "") for p in people}
    rows = []
    for person in people:
        rec = by_phone.get(person["phone"])
        rows.append(_summary_row(person, rec, period_type, period_key,
                                 now, ""))
    for rec in records:
        phone = str(rec.get("phone") or "")
        if phone in listed:
            continue
        rows.append(_summary_row(
            {"phone": phone, "name": str(rec.get("name") or "")}, rec,
            period_type, period_key, now, "不在应交名单"))

    for seq, row in enumerate(rows, start=1):
        ws.append([seq] + row)


def _summary_row(person, rec, period_type, period_key, now,
                 note_extra="") -> list:
    ptype = (rec or {}).get("period_type") or period_type or ""
    ptype_label = _PERIOD_TYPE_LABEL.get(str(ptype), str(ptype))
    period = (rec or {}).get("period_key") or period_key or ""
    if rec:
        # ⭐ 2026-10-20 用户指令：总结表移除「录入来源」列
        return [STATE_DONE, person["phone"], person.get("name") or "",
                ptype_label, period,
                _fmt_time(rec.get("updated_time")),
                _one_line(rec.get("content")), note_extra]
    return [STATE_MISSING, person["phone"], person.get("name") or "",
            ptype_label, period, "", "", note_extra]


def _fill_report(ws, records: list, roster, period_key) -> None:
    ws.title = "报数"
    columns = []          # [(field_id, 列名)]，按 field_id 升序
    seen = set()
    for rec in records:
        for item in rec.get("items") or []:
            fid = item.get("field_id")
            if fid in seen:
                continue
            seen.add(fid)
            name = item.get("field_name") or ("字段%s" % fid)
            unit = item.get("unit")
            columns.append((fid, "%s（%s）" % (name, unit) if unit else name))
    columns.sort(key=lambda c: (c[0] is None, c[0]))
    ws.append(["序号", "提交状态", "手机号", "姓名", "提交时间"]
              + [header for _, header in columns] + ["备注"])

    by_phone = {}
    for rec in records:
        by_phone[str(rec.get("phone") or "")] = rec

    people = roster or _people_from(records)
    listed = {str(p.get("phone") or "") for p in people}
    rows = []
    for person in people:
        rows.append(_report_row(person, by_phone.get(person["phone"]),
                                columns, ""))
    for rec in records:
        phone = str(rec.get("phone") or "")
        if phone in listed:
            continue
        rows.append(_report_row(
            {"phone": phone, "name": str(rec.get("name") or "")}, rec,
            columns, "不在应交名单"))

    for seq, row in enumerate(rows, start=1):
        ws.append([seq] + row)


def _report_row(person, rec, columns, note_extra="") -> list:
    if not rec:
        # ⚠️ 列数必须与「已提交」分支严格一致（state/phone/name/时间 = 4 列）
        #   —— 多一个空串就会让未提交行比表头多一列，
        #   Excel 里表现为右端冒出一个空列（2026-10-20 线上实测踩到）
        return [STATE_MISSING, person["phone"], person.get("name") or "",
                ""] + ["" for _ in columns] + [note_extra]
    values = {}
    for item in rec.get("items") or []:
        # 同字段多次出现取最后一条（防御性；一周期一次提交下不会发生）
        values[item.get("field_id")] = item.get("quantity")
    notes = [note_extra] if note_extra else []
    role = str(rec.get("role") or "")
    if role:  # D7 起角色绑定已取消；旧数据如实标注，⛔ 不丢信息
        notes.append("角色：%s" % role)
    # ⭐ 2026-10-20 用户指令：报数表移除「录入来源」列
    return [STATE_DONE, person["phone"], person.get("name") or "",
            _fmt_time(rec.get("created_time"))] + \
           [_report_value(values, fid) for fid, _ in columns] + \
           ["；".join(notes)]


def _report_value(values: dict, fid):
    """单字段取值：已提交但这一格没填 -> 「未选择」（⛔ 不留空白让人猜）。"""
    if fid not in values:
        return REPORT_UNSELECTED
    value = values[fid]
    if value is None or (isinstance(value, str) and not value.strip()):
        return REPORT_UNSELECTED
    return _num(value)


# ---------- 公共小工具 ----------

def _people_from(records: list) -> list:
    """无名单时的回落：按记录出现顺序去重出人（phone + name）。"""
    people = []
    seen = set()
    for rec in records:
        phone = str(rec.get("phone") or "")
        if not phone or phone in seen:
            continue
        seen.add(phone)
        people.append({"phone": phone,
                       "name": str(rec.get("name") or "")})
    return people


def _one_line(value) -> str:
    """多行正文压成一行（Excel 单元格里换行会让行高爆掉）。"""
    text = str(value or "")
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def _decorate(ws) -> None:
    """表头样式 + 冻结首行 + 自动筛选 + 列宽 + 未提交整行浅红。"""
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = _CENTER
    ws.freeze_panes = "A2"
    if ws.max_row >= 1:
        ws.auto_filter.ref = ws.dimensions

    headers = [str(c.value or "") for c in ws[1]]
    for idx, width in enumerate(_column_widths(ws), start=1):
        letter = get_column_letter(idx)
        ws.column_dimensions[letter].width = width
        if idx <= len(headers) and headers[idx - 1] == "内容":
            ws.column_dimensions[letter].width = 60
            for cell in ws[get_column_letter(idx)][1:]:
                cell.alignment = _WRAP

    # ⭐ 2026-10-20 用户指令：⛔ 不再整行铺粉 —— 一个人没交就整行变粉，
    # 多人未交时表格看着像坏了（用户实测：首行正常、后面全是粉底）。
    # 只给「未提交 / 未到打卡时间段」这些**状态单元格**上浅底，其余保持白底。
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if cell.value in (STATE_MISSING, STATE_NOT_STARTED):
                cell.fill = _PENDING_FILL


def _column_widths(ws) -> list:
    """按内容估列宽（中文按 2 个字符宽），夹在 8~42 之间。"""
    widths = []
    for col in ws.iter_cols():
        best = 8
        for cell in col:
            text = "" if cell.value is None else str(cell.value)
            width = sum(2 if ord(ch) > 127 else 1 for ch in text)
            best = max(best, min(width + 2, 42))
        widths.append(best)
    return widths