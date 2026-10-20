# -*- coding: utf-8 -*-
"""成员业务完成情况：状态口径（⭐ 2026-10-20 用户指令）。

被测口径（用户原话拆解）：
1. **总结 / 报数当天没有截止时刻** -> 当天缺记录显示「未完成」，
   ⛔ 绝不显示「逾期（待补录）」；只有第二天回看昨天才叫逾期。
2. **触发过提醒**（群里确实收到，status=sent）-> 「未完成（已提醒）」；
   推送失败（unsent）⛔ 不算提醒过。
3. **打卡未到时段结束** -> 「未完成（时段区间）」；过了结束时刻才算逾期。
4. 周报群的总结按**整周**判定：本周内缺记录是「未完成」，
   只有跨到下一周回看才是「逾期」。

这些用例锁死的是「状态文案与判定时机」，⛔ 不是 UI 文案的自嗨：
管理员据此决定补录还是催办，判错会重复记账 / 漏催。
"""

from __future__ import annotations

from datetime import datetime

from app.admin.board import board_rows, build_board
from app.period import TZ
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, enable_all

TODAY = "2026-10-20"       # 周二
YESTERDAY = "2026-10-19"   # 周一（与前一日同属 2026-W43）


def _now(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ)


def _state(tmp_db, *, period_type: str = "daily"):
    enable_all(tmp_db, period_type=period_type)
    tmp_db.upsert_member_task(GROUP, PHONE, "张三", "开发", True)
    return tmp_db


def _board(tmp_db, backend=None, *, date_str=TODAY, now="2026-10-20 10:00"):
    return build_board(tmp_db, backend or FakeBackend(), GROUP, date_str,
                       now=_now(now))


def _remind(tmp_db, biz: str, period_key: str, *, sent: bool = True,
            session=None):
    tmp_db.record_reminder_plan(group_id=GROUP, biz=biz,
                                period_key=period_key, phone=PHONE,
                                session=session, now=_now("2026-10-20 09:00"))
    if sent:
        tmp_db.mark_reminder_sent(GROUP, biz, period_key, PHONE, session,
                                  now=_now("2026-10-20 09:00"))
    else:
        tmp_db.mark_reminder_unsent(GROUP, biz, period_key, PHONE, session,
                                    "平台拒收", now=_now("2026-10-20 09:00"))


class TestSameDayIsNotOverdue:
    def test_today_missing_summary_and_report_is_pending(self, tmp_db):
        """当天没交 -> 未完成（⛔ 不是逾期/待补录）。"""
        _state(tmp_db)
        member = _board(tmp_db)["members"][0]
        assert member["summary"] is None
        assert member["summary_status"] == "pending"
        assert member["report_status"] == "pending"

    def test_yesterday_missing_is_overdue(self, tmp_db):
        """第二天回看昨天 -> 逾期（待补录）。"""
        _state(tmp_db)
        member = _board(tmp_db, date_str=YESTERDAY,
                        now="2026-10-20 10:00")["members"][0]
        assert member["summary_status"] == "overdue"
        assert member["report_status"] == "overdue"

    def test_weekly_summary_not_overdue_inside_the_week(self, tmp_db):
        """周报群：本周内（哪怕已过当天）都还是「未完成」。"""
        _state(tmp_db, period_type="weekly")
        member = _board(tmp_db, date_str=YESTERDAY,
                        now="2026-10-20 10:00")["members"][0]
        # 2026-10-19 属 2026-W43，周日 10-25 才结束
        assert member["summary_status"] == "pending"

    def test_weekly_summary_overdue_after_week_end(self, tmp_db):
        """周报群：跨到下下周回看上一周 -> 逾期。"""
        _state(tmp_db, period_type="weekly")
        member = _board(tmp_db, date_str="2026-10-13",
                        now="2026-10-26 10:00")["members"][0]
        # 2026-10-13 属 W42（10-12~10-18），10-26 已过
        assert member["summary_status"] == "overdue"


class TestRemindedFlag:
    def test_sent_reminder_marks_reminded(self, tmp_db):
        _state(tmp_db)
        _remind(tmp_db, "summary", TODAY)
        member = _board(tmp_db)["members"][0]
        assert member["summary_status"] == "reminded"
        # 报数没催过 -> 仍是未完成
        assert member["report_status"] == "pending"

    def test_unsent_reminder_is_not_reminded(self, tmp_db):
        """推送失败（unsent）= 群里没收到，⛔ 不能标「已提醒」。"""
        _state(tmp_db)
        _remind(tmp_db, "summary", TODAY, sent=False)
        member = _board(tmp_db)["members"][0]
        assert member["summary_status"] == "pending"

    def test_reminded_does_not_override_overdue(self, tmp_db):
        """过期该补录：已提醒 ⛔ 不遮盖「逾期（待补录）」。"""
        _state(tmp_db)
        _remind(tmp_db, "summary", YESTERDAY)
        member = _board(tmp_db, date_str=YESTERDAY)["members"][0]
        assert member["summary_status"] == "overdue"


class TestCheckinWindow:
    def test_before_slot_end_shows_window(self, tmp_db):
        """10:00 未到 12:00 -> 未完成（00:00-12:00）。"""
        _state(tmp_db)
        member = _board(tmp_db)["members"][0]
        cell = member["checkin"]["am"]
        assert cell["status"] == "waiting"
        assert cell["window"] == "00:00-12:00"
        assert member["checkin_status"] == "pending"

    def test_after_slot_end_is_overdue(self, tmp_db):
        """13:00 已过上午截止 -> 上午逾期；下午还没到点仍是未完成。"""
        _state(tmp_db)
        member = _board(tmp_db, now="2026-10-20 13:00")["members"][0]
        assert member["checkin"]["am"]["status"] == "overdue"
        assert member["checkin"]["pm"]["status"] == "waiting"
        # 还有时段能打 -> 整体不算逾期，仍是未完成
        assert member["checkin_status"] == "pending"

    def test_all_slots_passed_is_overdue(self, tmp_db):
        """23:59 两个时段都过点且没打 -> 整体逾期。"""
        _state(tmp_db)
        member = _board(tmp_db, now="2026-10-20 23:59")["members"][0]
        assert member["checkin"]["am"]["status"] == "overdue"
        assert member["checkin"]["pm"]["status"] == "overdue"
        assert member["checkin_status"] == "overdue"

    def test_past_day_checkin_is_overdue(self, tmp_db):
        _state(tmp_db)
        member = _board(tmp_db, date_str=YESTERDAY)["members"][0]
        assert member["checkin"]["am"]["status"] == "overdue"


class TestBoardRowsLabels:
    def test_export_row_uses_same_wording(self, tmp_db):
        """导出表与页面同源：未到点写「未完成（区间）」，当天未交写「未完成」。"""
        _state(tmp_db)
        headers, rows = board_rows(_board(tmp_db))
        row = rows[0]
        am_status = row[headers.index("上午打卡状态")]
        summary_time = row[headers.index("今日总结提交时间")]
        assert am_status == "未完成（00:00-12:00）"
        assert summary_time == ""
        # 总结列本身不带状态文案，但综合判定必须说「未提交」而不是「逾期」
        assert "总结未提交" in row[headers.index("全天综合达标判定")]
