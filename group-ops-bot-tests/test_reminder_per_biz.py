# -*- coding: utf-8 -*-
"""提醒按业务独立配置测试（用户指令验收项 5：按业务独立开关 / 时间 / 文本）。

口径（用户原话）：提醒配置按业务（checkin/summary/report）独立配置，
每个业务独立开启关闭、提醒时间、提醒文本。

与 test_reminder_phase5.py 的分工：那里测群级开关门控 / 冷却 / C 挂起；
本文件测**规则结构本身**（按业务独立）+ 后台保存校验 + 新旧格式兼容。
"""

from __future__ import annotations

from datetime import datetime

from app.handlers import reminder as reminder_handler
from doubles.fakes import FakeBackend
from helpers import GROUP, OTHER_PHONE, PHONE, TZ, enable_all
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)
from test_reminder_stats import (AM, LATE, ReminderBackend,
                                 setup_group_with_required)

T_1010 = datetime(2026, 10, 13, 10, 10, tzinfo=TZ)   # 09:00 已过、14:50/19:00 未过
# ⭐ 2026-10-16 用户指令四：打卡提醒时刻 = **时段开始时间**（默认 am 00:00-09:00
# -> 00:00 提醒，pm 12:00-14:50 -> 12:00 提醒；触发窗口 8 分钟。
T_AM_START = datetime(2026, 10, 13, 0, 3, tzinfo=TZ)    # am 时段开始后窗口内
T_PM_START = datetime(2026, 10, 13, 12, 3, tzinfo=TZ)   # pm 时段开始后窗口内
T_0603 = datetime(2026, 10, 13, 6, 3, tzinfo=TZ)     # 自定义早场 06:00 开始后窗口内
T_1930 = datetime(2026, 10, 13, 19, 30, tzinfo=TZ)   # 全部默认时刻已过


def _push_ok(group_id, text):
    return True, ""


def rules_of(tmp_db, group_id=GROUP):
    row = tmp_db.get_group_config(group_id)
    return reminder_handler.normalize_reminder_rules(
        row["reminder_rules"] if row else None)


def set_rules(tmp_db, rules):
    tmp_db.upsert_group_config(GROUP, reminder_rules=rules)


def planned_biz_set(tmp_db):
    rows = tmp_db._conn.execute(
        "SELECT DISTINCT biz FROM reminder_plan").fetchall()
    return {r["biz"] for r in rows}


def config_client(tmp_db, monkeypatch):
    bootstrap(tmp_db, monkeypatch)
    client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
    assert login(client).status_code == 200
    mutate(client, "POST", "/api/admin/change-password",
           {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert login(client, password=NEW_PASS).status_code == 200
    mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
    return client


# ---------- 规则解析（新格式 / 旧格式 / 坏值） ----------

class TestRulesParsing:
    def test_new_format_per_biz(self, tmp_db):
        """新格式：每个业务独立 enabled / times / text。"""
        set_rules(tmp_db, {
            "checkin": {"enabled": True, "times": ["09:00", "14:50"],
                        "text": "记得打卡"},
            "summary": {"enabled": False, "times": ["19:00"], "text": ""},
            "report": {"enabled": True, "times": ["20:00"],
                       "text": "今晚交数"},
        })
        rules = rules_of(tmp_db)
        assert rules["checkin"] == {"enabled": True,
                                    "times": ["09:00", "14:50"],
                                    "text": "记得打卡"}
        assert rules["summary"]["enabled"] is False
        assert rules["report"]["times"] == ["20:00"]

    def test_legacy_list_format_compat(self, tmp_db):
        """旧格式（纯时间列表）兼容转换：enabled 默认开、text 空。"""
        set_rules(tmp_db, {"checkin": ["09:00", "14:50"],
                           "summary": ["19:00"],
                           "report": ["19:00"]})
        rules = rules_of(tmp_db)
        assert rules["checkin"]["times"] == ["09:00", "14:50"]
        assert rules["checkin"]["enabled"] is True
        assert rules["summary"]["text"] == ""

    def test_defaults_when_absent(self, tmp_db):
        """未配置 -> 产品默认（checkin 09:00/14:50；summary/report 19:00）。"""
        assert rules_of(tmp_db) == {
            "checkin": {"enabled": True, "times": ["09:00", "14:50"],
                        "text": ""},
            "summary": {"enabled": True, "times": ["19:00"], "text": ""},
            "report": {"enabled": True, "times": ["19:00"], "text": ""},
        }

    def test_bad_values_fall_back_per_biz(self, tmp_db):
        """坏值按业务回落默认，⛔ 不因一个业务坏掉全军覆没。"""
        set_rules(tmp_db, {"checkin": "garbage",
                           "summary": {"times": "not-a-list"},
                           "report": {"enabled": True, "times": ["20:00"]}})
        rules = rules_of(tmp_db)
        assert rules["checkin"] == reminder_handler._default_rule("checkin")
        assert rules["summary"] == reminder_handler._default_rule("summary")
        assert rules["report"]["times"] == ["20:00"]

    def test_times_capped_at_five(self, tmp_db):
        """单业务提醒时刻上限 5 个（⛔ 不放任配置膨胀）。"""
        set_rules(tmp_db, {"checkin": {"enabled": True,
                                       "times": ["07:00", "08:00", "09:00",
                                                 "10:00", "11:00", "12:00"]}})
        assert rules_of(tmp_db)["checkin"]["times"] == [
            "07:00", "08:00", "09:00", "10:00", "11:00"]

    def test_text_truncated_at_200(self, tmp_db):
        """提醒文本上限 200 字符。"""
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["09:00"],
                                       "text": "x" * 500}})
        assert len(rules_of(tmp_db)["checkin"]["text"]) == 200


# ---------- 按业务独立生效 ----------

class TestPerBizIndependent:
    def test_disabled_biz_not_reminded(self, tmp_db):
        """summary 提醒独立关掉 -> 只提醒 checkin / report（群提醒总开关仍开）。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["00:00"]},
                           "summary": {"enabled": False, "times": ["19:00"]},
                           "report": {"enabled": True, "times": ["19:00"]}})
        backend = ReminderBackend()
        summary = reminder_handler.tick(tmp_db, backend, _push_ok, T_AM_START)
        assert planned_biz_set(tmp_db) == {"checkin"}

    def test_independent_times_gate(self, tmp_db):
        """同一时刻表按业务各自判定：10:10 只 checkin（09:00 已过），
        summary/report 的 19:00 未到 -> 不提醒。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["00:00"]},
                           "summary": {"enabled": True, "times": ["19:00"]},
                           "report": {"enabled": True, "times": ["19:00"]}})
        backend = ReminderBackend()
        reminder_handler.tick(tmp_db, backend, _push_ok, T_AM_START)
        assert planned_biz_set(tmp_db) == {"checkin"}
        # 19:30 再跑：summary/report 的时刻到了，checkin 时刻（09:00）已过也会再判定
        reminder_handler.tick(tmp_db, backend, _push_ok, T_1930)
        assert {"checkin", "summary", "report"} <= planned_biz_set(tmp_db)

    def test_independent_text_in_message(self, tmp_db):
        """每个业务用自己的文本：checkin 用自定义文本，summary 用默认文案。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["00:00"],
                                       "text": "早课记得打卡"},
                           "summary": {"enabled": True, "times": ["19:00"]},
                           "report": {"enabled": True, "times": ["19:00"]}})
        backend = ReminderBackend()
        pushed = []

        def push(group_id, text):
            pushed.append(text)
            return True, ""

        summary = reminder_handler.tick(tmp_db, backend, push, T_AM_START)
        assert summary["sent"] > 0
        # ⭐ 2026-10-16 用户指令三：新文案为分段结构，自定义文本作为**首行**
        assert pushed and all(t.startswith("早课记得打卡") for t in pushed)
        assert all("🔔 【打卡】未完成提醒" in t for t in pushed)
        assert any(PHONE[-4:] in t for t in pushed)  # 拼接未完成名单

    def test_summary_reminder_matches_checkin_format(self, tmp_db):
        """总结提醒与打卡同一套分段格式（用户指令 2026-10-16 五：
        收纳成打卡一样的格式）。⛔ 除打卡多一行时段说明外结构完全相同；
        格式由 format_message 单路径产出，本测试防回退成第二套文案。"""
        setup_group_with_required(tmp_db)
        set_rules(tmp_db, {"checkin": {"enabled": False},
                           "summary": {"enabled": True, "times": ["19:00"]},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        pushed = []

        def push(group_id, text):
            pushed.append(text)
            return True, ""

        reminder_handler.tick(tmp_db, backend, push, T_1930)
        assert len(pushed) == 1   # 一条合并消息（⛔ 不逐人各发一条）
        text = pushed[0]
        assert "【总结】未完成提醒" in text
        assert "📋 待提交名单：" in text
        assert "@" in text                     # 未完成名单（展示文本）
        assert "⏰ 截止时间：今晚 24:00" in text
        assert "👉 快速处理：请发送「@general-bot 总结」完成提交。" in text

    def test_checkin_sessions_follow_configured_slots(self, tmp_db):
        """多时间段后：按群配置区间判定应完成（自定义单区间 06:00~08:30）。

        ⭐ 2026-10-16 用户指令四：提醒时刻 = **时段开始时间**
        -> 该时段 06:00 开始，故 tick 时刻取 06:03（触发窗口内），
        ⛔ 不再是「09:00 已过就算」那种口径。
        """
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "morning", "name": "早场", "start": "06:00", "end": "08:30"},
        ]})
        set_rules(tmp_db, {"checkin": {"enabled": True, "times": ["06:00"]},
                           "summary": {"enabled": False},
                           "report": {"enabled": False}})
        backend = ReminderBackend()
        reminder_handler.tick(tmp_db, backend, _push_ok, T_0603)
        sessions = {r["session"] for r in tmp_db._conn.execute(
            "SELECT session FROM reminder_plan WHERE biz='checkin'")}
        assert sessions == {"morning"}

# ---------- 后台保存校验 ----------

class TestRulesApiValidation:
    def _put(self, client, rules):
        return mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"reminder_rules": rules})

    def test_per_biz_saved_and_echoed(self, tmp_db, monkeypatch):
        client = config_client(tmp_db, monkeypatch)
        resp = self._put(client, {
            "checkin": {"enabled": True, "times": ["09:00", "14:50"],
                        "text": "记得打卡"},
            "summary": {"enabled": False, "times": ["19:00"]},
        })
        assert resp.status_code == 200
        echoed = resp.json()["config"]["reminder_rules"]
        assert echoed["checkin"]["times"] == ["09:00", "14:50"]
        assert echoed["checkin"]["text"] == "记得打卡"
        assert echoed["summary"]["enabled"] is False
        assert echoed["report"]["enabled"] is True  # 未提供的业务不动

    def test_bad_time_rejected(self, tmp_db, monkeypatch):
        client = config_client(tmp_db, monkeypatch)
        assert self._put(client, {
            "checkin": {"times": ["25:00"]}}).status_code == 400
        assert self._put(client, {
            "checkin": {"times": ["9点"]}}).status_code == 400

    def test_too_many_times_rejected(self, tmp_db, monkeypatch):
        client = config_client(tmp_db, monkeypatch)
        assert self._put(client, {
            "checkin": {"times": ["07:00", "08:00", "09:00",
                                  "10:00", "11:00", "12:00"]}}).status_code == 400

    def test_long_text_rejected(self, tmp_db, monkeypatch):
        client = config_client(tmp_db, monkeypatch)
        assert self._put(client, {
            "report": {"text": "x" * 201}}).status_code == 400

    def test_empty_rules_rejected(self, tmp_db, monkeypatch):
        client = config_client(tmp_db, monkeypatch)
        assert self._put(client, {}).status_code == 400
