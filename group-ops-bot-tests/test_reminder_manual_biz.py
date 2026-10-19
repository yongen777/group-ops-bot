# -*- coding: utf-8 -*-
"""按业务手动提醒 + 提醒规则合并语义测试（用户指令 2026-10-16 三）。

口径：
- 后台打卡提醒只留开关；提醒时刻固定 = 时段开始时间（⛔ 不可配）
- 每行一个「立即提醒」按钮：一键触发**单个业务**（run_group(biz=...)）
- 手动触发跳过「到点才判定」时间门控（打卡取全部未结束时段），
  但 5 分钟冷却仍生效（⛔ 连点不轰炸）
- 提醒规则部分更新 = 合并语义（只送打卡开关 ⛔ 不得清掉总结/报数规则）
"""

from __future__ import annotations

import json
from datetime import datetime

from app.handlers import reminder as reminder_handler
from helpers import GROUP, OTHER_PHONE, PHONE, TZ, enable_all
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)
from test_reminder_stats import ReminderBackend, setup_group_with_required

# 08:03：am 时段开始后 8 分钟窗口内 -> 自动口径触发
T_0803 = datetime(2026, 10, 13, 8, 3, tzinfo=TZ)
# 08:45：am 时段（08:00-09:30）进行中，但已过 8 分钟触发窗口
# -> 自动调度这轮不触发；手动触发必须触发（取全部未结束时段）
T_0845 = datetime(2026, 10, 13, 8, 45, tzinfo=TZ)
# 07:00：am 未开始 -> 手动触发仍覆盖（未截止时段都算）
T_0700 = datetime(2026, 10, 13, 7, 0, tzinfo=TZ)
# 10:10：am 已结束、无其他时段 -> 手动触发打卡也无对象（checkin_all_done）
T_1010 = datetime(2026, 10, 13, 10, 10, tzinfo=TZ)

SINGLE_SLOT = {"slots": [
    {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
]}


def _push_ok(group_id, text, mention_phones=None):
    return True, ""


def planned(tmp_db, biz=None):
    rows = tmp_db._conn.execute(
        "SELECT biz, phone, session FROM reminder_plan").fetchall()
    if biz:
        rows = [r for r in rows if r["biz"] == biz]
    return rows


def set_rules(tmp_db, rules):
    tmp_db.upsert_group_config(GROUP, reminder_rules=rules)


def setup_single_slot_group(tmp_db):
    """单时段群（08:00-09:30）：手动 / 自动口径差异可无歧义断言。"""
    setup_group_with_required(tmp_db)
    tmp_db.upsert_group_config(GROUP, checkin_deadlines=SINGLE_SLOT)


# ---------- run_group：按业务过滤 ----------

class TestRunGroupBizFilter:
    def test_biz_filter_only_that_biz(self, tmp_db):
        """biz="checkin" -> 只计划打卡（总结/报数规则同刻已过也不动）。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": True, "times": ["00:00"]},
                           "report": {"enabled": True, "times": ["00:00"]}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        summary = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0803, biz="checkin")
        assert summary["biz"] == "checkin"
        assert summary["manual"] is False   # 未传 manual = 自动口径
        assert {r["biz"] for r in planned(tmp_db)} == {"checkin"}
        assert summary["sent"] > 0

    def test_biz_filter_summary(self, tmp_db):
        """biz="summary" -> 只计划总结（打卡即使到点也不动）。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": True, "times": ["00:00"]},
                           "report": {"enabled": False, "times": ["00:00"]}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        summary = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="summary")
        assert {r["biz"] for r in planned(tmp_db)} == {"summary"}
        assert summary["sent"] > 0

    def test_unknown_biz_plans_nothing(self, tmp_db):
        """⛔ 未提供/未知业务 = 不计划任何东西（fail-closed）。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        reminder_handler.run_group(tmp_db, backend, _push_ok, group_row,
                                   T_0845, biz="nope")
        assert planned(tmp_db) == []


# ---------- 手动触发：跳过时间门控 + 冷却仍生效 ----------

class TestManualTriggerSemantics:
    def test_manual_fires_outside_trigger_window(self, tmp_db):
        """08:45 超出 8 分钟窗口：自动不触发，手动触发（用户指令三）。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        # 自动口径：窗口外 -> 无触发
        auto = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="checkin")
        assert auto["targets"] == 0
        # 手动口径：未结束时段全部算
        manual = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="checkin",
            manual=True)
        assert manual["targets"] > 0
        assert manual["sent"] > 0
        sessions = {r["session"] for r in planned(tmp_db, "checkin")}
        assert sessions == {"am"}

    def test_manual_covers_not_yet_started_slot(self, tmp_db):
        """07:00 时段未开始：手动也覆盖（⛔ 只排除已结束时段）。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        manual = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0700, biz="checkin",
            manual=True)
        assert manual["targets"] > 0
        assert {r["session"] for r in planned(tmp_db, "checkin")} == {"am"}

    def test_manual_skips_ended_slots(self, tmp_db):
        """10:10：am 已结束 -> 手动打卡提醒也无对象（⛔ 不催已截止时段）。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        summary = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_1010, biz="checkin",
            manual=True)
        assert summary["skipped"] == "checkin_all_done"
        assert planned(tmp_db) == []

    def test_manual_respects_cooldown(self, tmp_db):
        """连点两次：第二次全员落在 5 分钟冷却内，⛔ 不重复打扰。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        first = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="checkin",
            manual=True)
        assert first["new"] > 0 and first["skipped_cooled"] == 0
        # 同一时刻再点一次（冷却 5 分钟未过）
        second = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="checkin",
            manual=True)
        assert second["new"] == 0
        assert second["skipped_cooled"] > 0
        assert len(planned(tmp_db, "checkin")) == first["new"]

    def test_manual_gated_by_reminder_switch(self, tmp_db):
        """群提醒总开关关 -> 手动也不发（⛔ 不搞第二套门控）。"""
        setup_single_slot_group(tmp_db)
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True, "reminder": False})
        backend = ReminderBackend()
        group_row = tmp_db.get_group_config(GROUP)
        summary = reminder_handler.run_group(
            tmp_db, backend, _push_ok, group_row, T_0845, biz="checkin",
            manual=True)
        assert summary["skipped"] == "reminder_disabled"
        assert planned(tmp_db) == []

    def test_manual_message_mentions_only_incomplete(self, tmp_db):
        """⛔ 2026-10-17 用户指令：手动提醒同样不带平台 @（消息开头那个
        @ 已移除）；文案内名单只含未完成者（PHONE 已完成 -> 只出现
        OTHER_PHONE，用户「保留一个就行了」的那一个）。"""
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend(completed={(PHONE, "checkin", "am")})
        captured = {}

        def push(group_id, text, mention_phones=None):
            captured["phones"] = mention_phones
            captured["text"] = text
            return True, ""

        group_row = tmp_db.get_group_config(GROUP)
        reminder_handler.run_group(tmp_db, backend, push, group_row, T_0845,
                                   biz="checkin", manual=True)
        assert captured["phones"] is None       # ⛔ 不再带平台 @
        assert OTHER_PHONE[-4:] in captured["text"]
        assert PHONE[-4:] not in captured["text"]   # 已完成者不进名单


# ---------- 后台 API：按业务触发 + 规则合并 ----------

class TestManualReminderApiPerBiz:
    API_CLOCK = datetime(2026, 10, 13, 8, 45, tzinfo=TZ)

    def _client(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp",
                              now_fn=lambda: self.API_CLOCK)
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        setup_single_slot_group(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["08:00"]},
                           "summary": {"enabled": True, "times": ["00:00"]},
                           "report": {"enabled": True, "times": ["00:00"]}})
        return client

    def _run(self, client, biz=None):
        body = {} if biz is None else {"biz": biz}
        return mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP, body)

    def test_run_with_biz_only_plans_that_biz(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = self._run(client, "summary")
        assert resp.status_code == 200
        assert "总结" in resp.json()["message"]
        assert {r["biz"] for r in planned(tmp_db)} == {"summary"}

    def test_run_checkin_manual_at_off_window(self, tmp_db, monkeypatch):
        """08:45 超出自动窗口：端点手动口径仍能催打卡（用户指令三）。"""
        client = self._client(tmp_db, monkeypatch)
        resp = self._run(client, "checkin")
        assert resp.status_code == 200
        assert {r["biz"] for r in planned(tmp_db)} == {"checkin"}

    def test_run_bad_biz_rejected(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = self._run(client, "unknown-biz")
        assert resp.status_code == 400

    def test_run_audit_records_biz(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        self._run(client, "report")
        rows = tmp_db._conn.execute(
            "SELECT detail FROM admin_audit WHERE action='reminder.run'"
        ).fetchall()
        assert rows
        assert any(json.loads(r["detail"]).get("biz") == "report"
                   for r in rows)

    def test_checkin_only_rules_update_preserves_siblings(
            self, tmp_db, monkeypatch):
        """只送打卡开关 -> 总结/报数规则原样保留（合并语义）。"""
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"reminder_rules": {"checkin": {"enabled": False}}})
        assert resp.status_code == 200
        echoed = resp.json()["config"]["reminder_rules"]
        assert echoed["checkin"]["enabled"] is False
        # ⛔ 未被清空：时间与文本保持原值
        assert echoed["summary"]["times"] == ["00:00"]
        assert echoed["report"]["enabled"] is True
        stored = reminder_handler.normalize_reminder_rules(
            tmp_db.get_group_config(GROUP)["reminder_rules"])
        assert stored["summary"]["times"] == ["00:00"]
        assert stored["report"]["times"] == ["00:00"]

    def test_empty_rules_still_rejected(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"reminder_rules": {}})
        assert resp.status_code == 400
