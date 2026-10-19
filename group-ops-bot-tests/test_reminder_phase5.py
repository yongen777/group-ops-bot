# -*- coding: utf-8 -*-
"""提醒业务第五阶段测试（用户指令：开关门控、防重、C 类挂起排除、
管理后台手动提醒）。

与 test_reminder_stats.py 的分工：那里测差集计算与既有去重；
本文件测第五阶段新增规则（门控 / 防重 / C 挂起 / 手动触发）。

⭐ 2026-10-17（修复用户报「上午 8 次 / 下午 7 次」）重写防重口径：
- 门控**先于**推送（⛔ 不得先把消息推出去再判冷却 / 已计划）；
- 自动调度：本期已发送过的目标 ⛔ 不再发（冷却到期也不重发）；
  unsent / pending 按 5 分钟冷却重试（推送失败容错）；
- 手动提醒：5 分钟冷却已过可重发（含已发送记录，防连点轰炸）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app.handlers import report as report_handler
from app.handlers import reminder as reminder_handler
from app.handlers.base import KIND_C
from doubles.fakes import FakeBackend
from helpers import GROUP, OTHER_PHONE, PHONE, TZ, enable_all, make_config, make_ctx
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)
from test_reminder_stats import LATE, AM, ReminderBackend, setup_group_with_required

COOLDOWN = timedelta(minutes=5)

# ⭐ 2026-10-16 用户指令四：打卡提醒时刻 = 时段开始时间（默认 am 00:00-09:00
# / pm 12:00-14:50）-> 分别在 00:00 与 12:00 提醒，触发窗口 8 分钟。
T_PM_START = datetime(2026, 10, 13, 12, 3, tzinfo=TZ)


def cfg_for(tmp_db):
    from pathlib import Path
    return make_config(Path(str(tmp_db.db_path)).parent)


def set_switch(tmp_db, **overrides):
    """合并式改开关（裸 upsert_group_config 是全量写，⛔ 直接传单键会清掉其他）。"""
    row = tmp_db.get_group_config(GROUP)
    switches = tmp_db.normalized_switches(row)
    switches.update(overrides)
    tmp_db.upsert_group_config(GROUP, biz_switches=switches)


def _push_ok(group_id, text):
    return True, ""


def _push_fail(group_id, text):
    return False, "platform push capability unconfirmed"


class TestSwitchGating:
    """⛔ 只对开启中的业务发提醒（用户指令第五阶段）。"""

    def test_business_switch_off_skips_that_business(self, tmp_db):
        setup_group_with_required(tmp_db)
        set_switch(tmp_db, report=False)
        # ⭐ summary 默认 19:00，在 12:03 未到点 -> 显式把 summary 时刻
        # 挪到 12:00（本轮要断言 summary 也被提醒），否则该断言恒失败。
        row = tmp_db.get_group_config(GROUP)
        rules = reminder_handler.normalize_reminder_rules(row["reminder_rules"])
        rules["summary"]["times"] = ["12:00"]
        rules["summary"]["enabled"] = True
        tmp_db.upsert_group_config(GROUP, reminder_rules=rules)
        backend = ReminderBackend()
        # ⭐ 打卡提醒改按「时段开始时间」触发（2026-10-16 指令四），
        # 用 pm 时段开始后的 12:03 跑这一轮。
        summary = reminder_handler.tick(tmp_db, backend, _push_fail,
                                        T_PM_START)
        # checkin pm + summary 都有目标；report 被开关挡住
        assert summary["targets"] > 0
        rows = tmp_db._conn.execute(
            "SELECT DISTINCT biz FROM reminder_plan").fetchall()
        biz_set = {r["biz"] for r in rows}
        assert "report" not in biz_set
        assert "checkin" in biz_set and "summary" in biz_set

    def test_checkin_switch_off_skips_checkin(self, tmp_db):
        setup_group_with_required(tmp_db)
        set_switch(tmp_db, checkin=False, report=False)
        backend = ReminderBackend()
        summary = reminder_handler.tick(tmp_db, backend, _push_fail, LATE)
        rows = tmp_db._conn.execute(
            "SELECT DISTINCT biz FROM reminder_plan").fetchall()
        assert {r["biz"] for r in rows} == {"summary"}

    def test_reminder_switch_off_skips_group(self, tmp_db):
        setup_group_with_required(tmp_db)
        set_switch(tmp_db, reminder=False)
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        part = reminder_handler.run_group(tmp_db, backend, _push_fail,
                                          group_row, LATE)
        assert part["skipped"] == "reminder_disabled"
        assert part["new"] == 0 and part["targets"] == 0

    def test_disabled_group_skips(self, tmp_db):
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(GROUP, status="disabled")
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        part = reminder_handler.run_group(tmp_db, backend, _push_fail,
                                          group_row, LATE)
        assert part["skipped"] == "group_disabled"
        assert part["new"] == 0
        # tick 仍遍历该群（groups 计数），但不计目标
        summary = reminder_handler.tick(tmp_db, backend, _push_fail, LATE)
        assert summary["groups"] == 1
        assert summary["targets"] == 0


class TestCooldown:
    """防重门控（2026-10-17 修复「上午 8 次 / 下午 7 次」后重写）。

    ⭐ 新顺序：门控先于推送。自动调度本期已发送过的目标 ⛔ 不再发
    （5 分钟冷却到期也不重发）；unsent / pending 按冷却重试；
    手动提醒冷却已过可重发（含已发送记录）。
    """

    def test_within_cooldown_no_repeat(self, tmp_db):
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        pushes = []

        def push(group_id, text, mention_phones=None):
            pushes.append(text)
            return True, ""

        first = reminder_handler.tick(tmp_db, backend, push, LATE)
        assert first["new"] > 0 and first["sent"] > 0
        sent_pushes = len(pushes)
        assert sent_pushes >= 1
        # 4 分钟后：冷却中 -> ⛔ 不重复打扰，且 ⛔ 不再发出任何消息
        # （旧 bug：消息在冷却判定之前就推出去了，只拦住了记账）
        second = reminder_handler.tick(
            tmp_db, backend, push, LATE + timedelta(minutes=4))
        assert second["new"] == 0
        assert len(pushes) == sent_pushes

    def test_auto_no_resend_even_after_cooldown(self, tmp_db):
        """自动调度：冷却已过也 ⛔ 不重发（本期每人就一次）。

        旧语义「冷却已过周期内可再提醒」配合「先推后判」正是用户报的
        重复提醒根源；长周期催醒改走管理后台手动提醒（5 分钟冷却防连点）。
        summary/report 的触发判定整个下午都成立，若允许冷却后重发
        = 每 5 分钟一条，⛔ 不可接受。
        """
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        pushes = []

        def push(group_id, text, mention_phones=None):
            pushes.append(text)
            return True, ""

        first = reminder_handler.tick(tmp_db, backend, push, LATE)
        assert first["new"] > 0
        sent_pushes = len(pushes)
        second = reminder_handler.tick(
            tmp_db, backend, push, LATE + COOLDOWN + timedelta(minutes=1))
        assert second["new"] == 0
        assert len(pushes) == sent_pushes   # ⛔ 不因冷却到期而重发
        rows = tmp_db._conn.execute(
            "SELECT * FROM reminder_plan WHERE phone=?", (PHONE,)).fetchall()
        assert rows and all(r["status"] == "sent" for r in rows)

    def test_manual_resend_after_cooldown(self, tmp_db):
        """手动提醒：冷却已过可重发（含已发送记录；只防连点轰炸）。"""
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        pushes = []

        def push(group_id, text, mention_phones=None):
            pushes.append(text)
            return True, ""

        group_row = tmp_db.get_group_config(GROUP)
        first = reminder_handler.run_group(
            tmp_db, backend, push, group_row, LATE, biz="summary",
            manual=True)
        assert first["new"] > 0 and len(pushes) == 1
        # 冷却未过（4 分钟）：⛔ 不重发
        second = reminder_handler.run_group(
            tmp_db, backend, push, group_row, LATE + timedelta(minutes=4),
            biz="summary", manual=True)
        assert second["new"] == 0 and len(pushes) == 1
        # 冷却已过：可重发（管理员主动催）
        third = reminder_handler.run_group(
            tmp_db, backend, push, group_row,
            LATE + COOLDOWN + timedelta(minutes=1), biz="summary",
            manual=True)
        assert third["new"] > 0 and len(pushes) == 2

    def test_unsent_retry_after_cooldown(self, tmp_db):
        """推送未接通：unsent 也按冷却重试（⛔ 不伪造已发送）。"""
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        first = reminder_handler.tick(tmp_db, backend, _push_fail, LATE)
        assert first["unsent"] > 0 and first["sent"] == 0
        second = reminder_handler.tick(
            tmp_db, backend, _push_fail, LATE + COOLDOWN + timedelta(minutes=1))
        assert second["new"] > 0
        assert second["unsent"] > 0 and second["sent"] == 0


class TestNoRepeatWithinTriggerWindow:
    """2026-10-17 回归（用户报「上午 8 次 / 下午 7 次」）：
    8 分钟触发窗口内按 60s 轮询间隔连续跑 8 轮，⛔ 只推 1 条。

    根因：旧顺序先 push 后判冷却 -> 窗口内每一轮都重发。
    群配置与用户实际群一致：am 09:00-12:00 / pm 14:00-14:50。
    """

    SLOTS = {"slots": [
        {"id": "am", "name": "上午", "start": "09:00", "end": "12:00"},
        {"id": "pm", "name": "下午", "start": "14:00", "end": "14:50"},
    ]}

    def _setup(self, tmp_db):
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines=self.SLOTS)

    def _counting_push(self, pushes):
        def push(group_id, text, mention_phones=None):
            pushes.append(text)
            return True, ""
        return push

    def test_eight_ticks_in_window_push_once(self, tmp_db):
        """am 窗口 09:00-09:08 连续 8 轮（60s 间隔）-> 只推 1 条。"""
        self._setup(tmp_db)
        pushes = []
        day = datetime(2026, 10, 13, 9, 0, tzinfo=TZ)
        for i in range(8):   # 09:00 / 09:01 / ... / 09:07 —— 用户报的 8 次
            reminder_handler.tick(tmp_db, ReminderBackend(),
                                  self._counting_push(pushes),
                                  day + timedelta(minutes=i))
        assert len(pushes) == 1
        assert "(上午)" in pushes[0]
        rows = tmp_db._conn.execute(
            "SELECT * FROM reminder_plan WHERE session='am'").fetchall()
        assert rows and all(r["status"] == "sent" for r in rows)

    def test_no_push_after_cooldown_inside_window(self, tmp_db):
        """冷却到期（09:05）但仍在窗口内 -> ⛔ 依然不推（旧语义会再推）。"""
        self._setup(tmp_db)
        pushes = []
        reminder_handler.tick(tmp_db, ReminderBackend(),
                              self._counting_push(pushes),
                              datetime(2026, 10, 13, 9, 0, tzinfo=TZ))
        # 09:05 / 09:06 / 09:07：5 分钟冷却已过、窗口未关 -> 旧代码会重发
        for minute in (5, 6, 7):
            reminder_handler.tick(tmp_db, ReminderBackend(),
                                  self._counting_push(pushes),
                                  datetime(2026, 10, 13, 9, minute, tzinfo=TZ))
        assert len(pushes) == 1

    def test_pm_window_pushes_once_more(self, tmp_db):
        """下午是另一个 session -> 各自只推一次（全天共 2 条，⛔ 不是 8+7）。"""
        self._setup(tmp_db)
        pushes = []
        am = datetime(2026, 10, 13, 9, 0, tzinfo=TZ)
        for i in range(8):
            reminder_handler.tick(tmp_db, ReminderBackend(),
                                  self._counting_push(pushes),
                                  am + timedelta(minutes=i))
        pm = datetime(2026, 10, 13, 14, 0, tzinfo=TZ)
        for i in range(8):
            reminder_handler.tick(tmp_db, ReminderBackend(),
                                  self._counting_push(pushes),
                                  pm + timedelta(minutes=i))
        assert len(pushes) == 2
        assert "(上午)" in pushes[0] and "(下午)" in pushes[1]


class TestCPendingExclusion:
    """§4.5 第 5 条：C 类待核验用户本轮暂不判定为未完成。"""

    def _make_c_for_report(self, tmp_db, phone):
        """经真实提交管线造一条 C + pending recovery（report）。"""
        enable_all(tmp_db)
        tmp_db.upsert_field(GROUP, 1, "bug修复数", None, ["开发"], True, 1)
        backend = FakeBackend()
        backend.raise_on.add("report_submit")
        ctx = make_ctx(tmp_db, backend, cfg_for(tmp_db), now=AM, phone=phone)
        reply = report_handler.handle_command(commands.parse("报数"), ctx)
        token = reply.split("#t=")[1]
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 1}])
        assert result.kind == KIND_C
        tasks = tmp_db.pending_recovery_tasks()
        assert any(t["scope_key"].startswith("%s|%s|report|" % (GROUP, phone))
                   for t in tasks)

    def test_c_pending_user_not_reminded(self, tmp_db):
        setup_group_with_required(tmp_db)
        self._make_c_for_report(tmp_db, PHONE)
        backend = ReminderBackend()
        ctx = make_ctx(tmp_db, backend, cfg_for(tmp_db), now=LATE)
        targets = reminder_handler.compute_incomplete(
            ctx, GROUP, "report", "2026-10-13")
        assert [t.phone for t in targets] == [OTHER_PHONE]

    def test_c_other_biz_still_reminded(self, tmp_db):
        """C 挂在 report 上：不影响 summary 的差集判定。"""
        setup_group_with_required(tmp_db)
        self._make_c_for_report(tmp_db, PHONE)
        backend = ReminderBackend()
        ctx = make_ctx(tmp_db, backend, cfg_for(tmp_db), now=LATE)
        targets = reminder_handler.compute_incomplete(
            ctx, GROUP, "summary", "2026-10-13")
        assert sorted(t.phone for t in targets) == sorted([PHONE, OTHER_PHONE])


class TestManualReminderApi:
    # 手动提醒端点跑真实业务时钟判定「规则是否过点」；⛔ 不注入固定时刻的话，
    # 该测试在 09:00 前跑会因三条规则都未过点而 targets=0（时好时坏）。
    # ⭐ 2026-10-16 用户指令四：打卡提醒时刻 = **时段开始时间**（默认
    # am 00:00-09:00 / pm 12:00-14:50）。18:00 时两个时段都已结束，
    # ⛔ 打卡不再提醒（这正是修掉「09:30 才提醒」的口径变更）。
    # 本用例断言手动提醒端点，故改用仍处进行中的 12:03（pm 时段开始）。
    API_CLOCK = datetime(2026, 10, 13, 12, 3, tzinfo=TZ)  # pm 时段开始后窗口内

    def test_run_endpoint_returns_summary(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp",
                              now_fn=lambda: self.API_CLOCK)
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        setup_group_with_required(tmp_db)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP, {})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        s = body["summary"]
        assert s["targets"] > 0
        # 推送未接通：如实记 unsent（⛔ 不伪造已发送）
        assert s["unsent"] > 0 and s["sent"] == 0
        # 审计留痕
        audit = tmp_db._conn.execute(
            "SELECT * FROM admin_audit WHERE action='reminder.run'"
        ).fetchall()
        assert audit

    def test_run_endpoint_disabled_group(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(GROUP, status="disabled")
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP, {})
        assert resp.status_code == 200
        assert "停用" in resp.json()["message"]

    def test_run_endpoint_requires_auth(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP, {})
        assert resp.status_code == 401
