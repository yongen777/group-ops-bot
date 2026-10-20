# -*- coding: utf-8 -*-
"""提醒「只催当前时段」回归（⭐ 2026-10-20 用户指令）。

线上现象（用户截图）：

    @郑佳(上午) @郑佳(下午) 🔔 【打卡】未完成提醒
    · 郑佳（上午）
    · 郑佳（下午）
    ⏰ 截止时间：12:00（上午）、14:50（下午）

根因：手动提醒走的是 `_open_slot_ids`（**全部未结束时段**），于是 09:53 点
「立即提醒」时把还没到点的下午也一起催了。改成 `_current_slot_ids`：
- 当前时刻落在某时段内 -> 只催该时段；
- 时段之间 / 首场之前 -> 只催下一场；
- 全部结束 -> 不催（该走补录）。

同时锁住文案口径：名单里 ⛔ 不再带「（上午）」，时段写进标题【打卡·上午】。
"""

from __future__ import annotations

from datetime import datetime

from app.handlers import reminder as reminder_handler
from helpers import GROUP, TZ, enable_all
from test_reminder_stats import ReminderBackend, setup_group_with_required

# 产品口径：上午 08:00-12:00 / 下午 14:00-14:50
T_0953 = datetime(2026, 10, 13, 9, 53, tzinfo=TZ)     # 上午时段内
T_1230 = datetime(2026, 10, 13, 12, 30, tzinfo=TZ)    # 两场之间
T_1530 = datetime(2026, 10, 13, 15, 30, tzinfo=TZ)    # 全部结束
T_0800 = datetime(2026, 10, 13, 8, 0, tzinfo=TZ)      # 上午场开始（自动口径）


def setup_slots(tmp_db):
    setup_group_with_required(tmp_db)
    # ⭐ 2026-10-20（D26）：提醒时刻 = 各时段 remind_at（这里钉在开始后 1 分钟，
    # 保持既有「刚开场就催」的断言口径）
    tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
        {"id": "am", "name": "上午", "start": "08:00", "end": "12:00",
         "remind_at": "08:01"},
        {"id": "pm", "name": "下午", "start": "14:00", "end": "14:50",
         "remind_at": "14:01"},
    ]})


def run(tmp_db, now, manual=True):
    pushed = []

    def push(group_id, text):
        pushed.append(text)
        return True, ""

    group_row = tmp_db.get_group_config(GROUP)
    summary = reminder_handler.run_group(
        tmp_db, ReminderBackend(), push, group_row, now,
        biz="checkin", manual=manual)
    return pushed, summary


def planned_sessions(tmp_db):
    rows = tmp_db._conn.execute(
        "SELECT DISTINCT session FROM reminder_plan WHERE biz='checkin'"
    ).fetchall()
    return {r[0] for r in rows}


class TestManualReminderScopedToCurrentSlot:
    def test_only_current_slot_is_reminded(self, tmp_db):
        """09:53 只该催上午（⛔ 不把 14:00 那场一起列上）。"""
        setup_slots(tmp_db)
        pushed, summary = run(tmp_db, T_0953)
        assert len(pushed) == 1
        text = pushed[0]
        assert "【打卡·上午】未完成提醒" in text
        assert "下午" not in text          # ⛔ 不再出现下午
        assert planned_sessions(tmp_db) == {"am"}

    def test_names_have_no_slot_suffix(self, tmp_db):
        """⭐ 名单里 ⛔ 不再写「郑佳（上午）」。"""
        setup_slots(tmp_db)
        pushed, _ = run(tmp_db, T_0953)
        assert "（上午）" not in pushed[0]
        assert "(上午)" not in pushed[0]
        assert "· " in pushed[0]

    def test_between_slots_reminds_next_one(self, tmp_db):
        """12:30（上午已结束、下午未开始）只催下午。"""
        setup_slots(tmp_db)
        pushed, _ = run(tmp_db, T_1230)
        assert len(pushed) == 1
        assert "【打卡·下午】未完成提醒" in pushed[0]
        assert "上午" not in pushed[0]
        assert planned_sessions(tmp_db) == {"pm"}

    def test_all_slots_ended_sends_nothing(self, tmp_db):
        """15:30 全部结束 -> 不催（该补录，⛔ 不误发催办）。"""
        setup_slots(tmp_db)
        pushed, summary = run(tmp_db, T_1530)
        assert pushed == []
        assert summary["skipped"] == "checkin_all_done"
        assert planned_sessions(tmp_db) == set()

    def test_auto_reminder_still_single_slot(self, tmp_db):
        """自动调度口径不变：08:01（配置的提醒点）只触发上午。"""
        setup_slots(tmp_db)
        pushed, _ = run(tmp_db, T_0800.replace(minute=1), manual=False)
        assert len(pushed) == 1
        assert "【打卡·上午】未完成提醒" in pushed[0]
        assert planned_sessions(tmp_db) == {"am"}
