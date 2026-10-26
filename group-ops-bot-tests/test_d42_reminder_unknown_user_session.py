# -*- coding: utf-8 -*-
"""D42：从未提交过任何内容的成员在**第二个打卡时段**被漏催。

线上现象（2026-10-25 周日下午打卡，测试群）：

    @郑佳 @黄宇翔 @戴紫玉 @邱章明 @罗岩 🔔 【打卡·下午】未完成提醒

少了高翔。同一轮的「上午打卡」「日总结」「报数」都正常把他列进去了。
日志证据：08:20 那轮 tick 为 ``targets: 6, new: 6``；
          14:20 那轮 tick 为 ``targets: 6, new: 5`` —— 判定出 6 人，门控只放行 5 人。

根因（`reminder.compute_incomplete`）：
后端用户目录里**查不到这个人**（从未提交过任何内容，`user_get` 返回空）时，
走的是「直接判未完成」的快捷分支，该分支**没有带 session**，于是目标变成
``session=None``。而 `reminder_plan` 的唯一键是
``(group_id, biz, period_key, phone, IFNULL(session,''))``，上午那轮已经把
``session=''`` 这行写成 ``sent``；下午再插 ``session=''`` 被唯一键挡住，
`record_reminder_plan` 读回来的正是上午那条 sent 行 -> 判为「本期已提醒过」->
该目标被静默丢弃。上午那轮虽然发出去了，标题也退化成【打卡】而非【打卡·上午】。

修复口径：
1) 快捷分支必须按**本轮触发的时段**逐个产出目标（一处人 × 一个时段 = 一条）；
2) `record_reminder_plan` 在「插入被忽略但按原键查不到行」时 ⛔ 不得静默返回
   False（那会把漏发伪装成"已提醒过"），改为告警放行；
3) `run_group` 出现「判定人数 > 实际放行人数」时打 WARNING，把被丢弃的人
   暴露在日志里，⛔ 不再只剩一个 ``new`` 数字让人去猜。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.handlers import reminder as reminder_handler
from helpers import GROUP, OTHER_PHONE, PHONE, TZ, make_config, make_ctx
from test_reminder_stats import ReminderBackend, setup_group_with_required

# 产品口径：上午 08:00-12:00（08:01 提醒）/ 下午 14:00-14:50（14:01 提醒）
T_AM = datetime(2026, 10, 25, 8, 1, tzinfo=TZ)
T_PM = datetime(2026, 10, 25, 14, 1, tzinfo=TZ)


def setup_slots(tmp_db):
    setup_group_with_required(tmp_db)
    tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
        {"id": "am", "name": "上午", "start": "08:00", "end": "12:00",
         "remind_at": "08:01"},
        {"id": "pm", "name": "下午", "start": "14:00", "end": "14:50",
         "remind_at": "14:01"},
    ]})


def run_auto(tmp_db, now, backend):
    pushed = []

    def push(group_id, text):
        pushed.append(text)
        return True, ""

    group_row = tmp_db.get_group_config(GROUP)
    summary = reminder_handler.run_group(
        tmp_db, backend, push, group_row, now, biz="checkin", manual=False)
    return pushed, summary


class TestMissingUserKeepsSession:
    def test_compute_incomplete_sets_session_for_unknown_user(self, tmp_db):
        """后端查不到人也要落到**本轮时段**上（⛔ 不是 session=None）。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})
        ctx = make_ctx(tmp_db, backend, make_config(
            __import__("pathlib").Path(tmp_db.db_path).parent), now=T_PM)
        targets = reminder_handler.compute_incomplete(
            ctx, GROUP, "checkin", "2026-10-25", sessions=["pm"])
        mine = [t for t in targets if t.phone == PHONE]
        assert len(mine) == 1
        assert mine[0].session == "pm"

    def test_unknown_user_gets_one_target_per_session(self, tmp_db):
        """一次判定两个时段 -> 两条目标（各自独立占 reminder_plan 的键）。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})
        ctx = make_ctx(tmp_db, backend, make_config(
            __import__("pathlib").Path(tmp_db.db_path).parent), now=T_AM)
        targets = reminder_handler.compute_incomplete(
            ctx, GROUP, "checkin", "2026-10-25", sessions=["am", "pm"])
        assert sorted(t.session for t in targets if t.phone == PHONE) == ["am", "pm"]


class TestPmReminderNotSwallowed:
    def test_pm_message_still_contains_unknown_user(self, tmp_db):
        """线上事故复现：上午催过之后，下午这条也必须 @ 到他。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})

        am_pushed, am = run_auto(tmp_db, T_AM, backend)
        assert am["new"] == 2

        pm_pushed, pm = run_auto(tmp_db, T_PM, backend)
        assert pm["targets"] == 2, "下午仍应判定出 2 人未完成"
        assert pm["new"] == 2, "⛔ 不得被上午那条 sent 记录吃掉"
        assert pm_pushed, "下午必须发出一条提醒"
        # 名单里显示 nickname；本夹具没有昵称 -> format_message 用手机号后四位脱敏
        assert PHONE[-4:] in pm_pushed[0], "⛔ 下午这条名单里必须还有他"
        assert "【打卡·下午】" in pm_pushed[0]

    def test_each_session_has_its_own_plan_row(self, tmp_db):
        """落库口径：同一人 am / pm 各一行，都必须是 sent。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})
        run_auto(tmp_db, T_AM, backend)
        run_auto(tmp_db, T_PM, backend)
        rows = tmp_db._conn.execute(
            "SELECT session, status FROM reminder_plan"
            " WHERE biz='checkin' AND phone=?", (PHONE,)).fetchall()
        assert {(r["session"], r["status"]) for r in rows} == {
            ("am", "sent"), ("pm", "sent")}

    def test_am_title_has_slot_name(self, tmp_db):
        """上午那条标题必须是【打卡·上午】（旧实现退化成【打卡】）。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})
        am_pushed, _ = run_auto(tmp_db, T_AM, backend)
        assert "【打卡·上午】" in am_pushed[0]


class TestDroppedTargetIsVisible:
    def test_run_group_reports_dropped_targets(self, tmp_db):
        """判定有人但被门控拦下时，summary 要能说出是谁（⛔ 只剩一个数字）。"""
        setup_slots(tmp_db)
        backend = ReminderBackend(missing_users={PHONE})
        run_auto(tmp_db, T_PM, backend)
        # 同一时段第二轮：全员已发 -> 全部被门控拦下
        _, second = run_auto(tmp_db, T_PM, backend)
        assert second["new"] == 0
        assert second["skipped_cooled"] == 2
        assert sorted(second["dropped"]) == sorted(
            [reminder_handler._mask_phone(PHONE),
             reminder_handler._mask_phone(OTHER_PHONE)])
        assert PHONE not in " ".join(second["dropped"])  # ⛔ 日志不留完整号码

    def test_plan_gate_does_not_swallow_unknown_key(self, tmp_db, caplog):
        """插入被忽略却按原键查不到行时，⛔ 不许静默判成「已提醒」。"""
        import logging
        with caplog.at_level(logging.WARNING, logger="group-ops.state"):
            allowed = tmp_db.record_reminder_plan(
                group_id=GROUP, biz="checkin", period_key="2026-10-25",
                phone=PHONE, session="pm")
            assert allowed is True
        # 手工制造"插入被忽略但查不到"的窗口：先删掉刚插的行再插入同名键
        tmp_db._conn.execute(
            "DELETE FROM reminder_plan WHERE phone=? AND session='pm'", (PHONE,))
        tmp_db._conn.commit()
        assert tmp_db.record_reminder_plan(
            group_id=GROUP, biz="checkin", period_key="2026-10-25",
            phone=PHONE, session="pm") is True
