# -*- coding: utf-8 -*-
"""D26 回归（2026-10-20 用户指令）。

三块内容：

一、**定时提醒不得被手动提醒吃掉**（真实事故：14:00 郑佳没被 @）
    线上 `reminder_plan` 实据：

        phone        session  status  created_at / sent_at
        17313771297  am       sent    2026-10-20T09:32:55   <- 手动催的
        17313771297  pm       sent    2026-10-20T09:32:55   <- 手动催的（旧逻辑越界催）
        17742898293  pm       sent    2026-10-20T14:00:51   <- 定时提醒正常

    旧口径：自动调度对任何 `sent` 行一律拒绝 -> 手动催过 = 当天定时提醒消失。
    新口径：`reminder_plan.source` 区分 auto / manual，自动调度只被
    「自动已发」的行拦；遇到 manual 已发 -> 接管重发一次（⛔ 不受冷却拦）。

二、**提醒时间点 remind_at**（用户方案）
    不再固定等于时段开始；每个时段单独配置，边界必须
    **开始 < remind_at < 结束**（08:00-08:30 -> 08:01~08:29）。
    未配置时默认 = 结束前 5 分钟。

三、**后台新增能力**（回调地址二次密码校验 / 管理员改名）
"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from app.admin import auth as admin_auth
from app.admin.server import create_admin_app
from app.handlers import checkin as checkin_handler
from app.handlers import reminder as reminder_handler
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, TZ, make_config
from test_reminder_stats import ReminderBackend, setup_group_with_required

SLOTS = {"slots": [
    {"id": "am", "name": "上午", "start": "08:00", "end": "12:00",
     "remind_at": "11:30"},
    {"id": "pm", "name": "下午", "start": "14:00", "end": "14:50",
     "remind_at": "14:30"},
]}

T_1230 = datetime(2026, 10, 13, 12, 30, tzinfo=TZ)   # 两场之间（手动催下一场）
T_1430 = datetime(2026, 10, 13, 14, 30, tzinfo=TZ)   # pm 的提醒时间点
T_1431 = datetime(2026, 10, 13, 14, 31, tzinfo=TZ)   # 同一窗口内再跑一轮
T_0800 = datetime(2026, 10, 13, 8, 0, tzinfo=TZ)     # am 开始时刻（新口径不触发）


def setup(tmp_db):
    setup_group_with_required(tmp_db)
    tmp_db.upsert_group_config(GROUP, checkin_deadlines=SLOTS)


def run(tmp_db, now, manual=False):
    pushed = []

    def push(group_id, text):
        pushed.append(text)
        return True, ""

    group_row = tmp_db.get_group_config(GROUP)
    summary = reminder_handler.run_group(
        tmp_db, ReminderBackend(), push, group_row, now,
        biz="checkin", manual=manual)
    return pushed, summary


def plan_map(tmp_db):
    rows = tmp_db._conn.execute(
        "SELECT phone, session, status, source FROM reminder_plan"
        " WHERE biz='checkin'").fetchall()
    return {(r["phone"], r["session"]): (r["status"], r["source"])
            for r in rows}


class TestManualDoesNotEatScheduled:
    """source 门控：手动提醒过 ≠ 当天定时提醒免发。"""

    def test_manual_marks_source_manual(self, tmp_db):
        setup(tmp_db)
        run(tmp_db, T_1230, manual=True)
        rows = plan_map(tmp_db)
        assert rows and all(v == ("sent", "manual") for v in rows.values())

    def test_scheduled_reminder_still_fires_after_manual(self, tmp_db):
        """12:30 手动催过下午 -> 14:30 定时提醒必须照发（事故回归）。"""
        setup(tmp_db)
        pushed_manual, _ = run(tmp_db, T_1230, manual=True)
        assert len(pushed_manual) == 1

        pushed_auto, summary = run(tmp_db, T_1430, manual=False)
        assert len(pushed_auto) == 1, "定时提醒被手动提醒吃掉了"
        assert summary["skipped_cooled"] == 0
        rows = plan_map(tmp_db)
        # 接管后来源翻成 auto（这行代表「定时提醒已发」）
        assert all(v == ("sent", "auto") for v in rows.values())

    def test_scheduled_not_repeated_twice(self, tmp_db):
        """自动已发 -> 同一窗口内再跑一轮 ⛔ 不再发（防重仍生效）。"""
        setup(tmp_db)
        run(tmp_db, T_1430, manual=False)
        pushed, summary = run(tmp_db, T_1431, manual=False)
        assert pushed == []
        assert summary["skipped_cooled"] >= 1

    def test_manual_cooldown_still_blocks_double_click(self, tmp_db):
        """手动连点仍受 5 分钟冷却（⛔ 不因 source 改动变成轰炸）。"""
        setup(tmp_db)
        run(tmp_db, T_1230, manual=True)
        pushed, _ = run(
            tmp_db, T_1230.replace(minute=31), manual=True)
        assert pushed == []


class TestRemindAtBoundaries:
    """提醒时间点边界：开始 < remind_at < 结束（用户方案）。"""

    def test_equal_to_start_rejected(self):
        problem = checkin_handler.remind_at_error("08:00", "08:30", "08:00")
        assert problem and "晚于" in problem

    def test_equal_to_end_rejected(self):
        problem = checkin_handler.remind_at_error("08:00", "08:30", "08:30")
        assert problem and "早于" in problem

    def test_inside_range_accepted(self):
        for value in ("08:01", "08:15", "08:29"):
            assert checkin_handler.remind_at_error(
                "08:00", "08:30", value) is None

    def test_bad_format_rejected(self):
        assert checkin_handler.remind_at_error("08:00", "08:30", "8点") is not None

    def test_default_is_five_minutes_before_end(self):
        assert checkin_handler.default_remind_at("08:00", "08:30") == "08:25"
        assert checkin_handler.default_remind_at("14:00", "14:50") == "14:45"

    def test_default_keeps_inside_short_slot(self):
        # 区间 4 分钟 -> 取开始后 1 分钟（仍严格在区间内）
        assert checkin_handler.default_remind_at("08:00", "08:04") == "08:01"
        # 区间仅 1 分钟 -> 无可选中间点，退回开始（极端退化）
        assert checkin_handler.default_remind_at("08:00", "08:01") == "08:00"

    def test_slot_normalization_fills_default(self):
        slots = checkin_handler.normalize_slots_config({"slots": [
            {"id": "s1", "name": "一班", "start": "08:00", "end": "08:30"},
            {"id": "s2", "name": "二班", "start": "09:00", "end": "10:00",
             "remind_at": "09:40"},
        ]})
        assert slots[0]["remind_at"] == "08:25"
        assert slots[1]["remind_at"] == "09:40"

    def test_out_of_range_config_falls_back_to_default(self):
        """越界值 ⛔ 不丢弃整个时段，也不进调度 —— 回落默认值。"""
        slots = checkin_handler.normalize_slots_config({"slots": [
            {"id": "s1", "name": "一班", "start": "08:00", "end": "08:30",
             "remind_at": "08:00"},
        ]})
        assert slots[0]["remind_at"] == "08:25"


class TestReminderTriggerUsesRemindAt:
    """自动调度的触发时刻 = remind_at（⛔ 不再等于 start）。"""

    def _hits(self, tmp_db, now):
        return reminder_handler._checkin_reminder_times(
            tmp_db.get_group_config(GROUP), now)

    def test_start_time_not_trigger_anymore(self, tmp_db):
        setup(tmp_db)
        assert self._hits(tmp_db, T_0800) == []

    def test_remind_at_triggers(self, tmp_db):
        setup(tmp_db)
        assert self._hits(tmp_db, T_1430) == ["pm"]

    def test_unconfigured_slot_uses_default(self, tmp_db):
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "am", "name": "快速打卡", "start": "08:00", "end": "08:30"},
        ]})
        assert self._hits(
            tmp_db, datetime(2026, 10, 13, 8, 25, tzinfo=TZ)) == ["am"]


# ---------------- 后台接口 ----------------


def make_client(tmp_db, tmp_path, username="admin", password="initial-pass-1"):
    tmp_db.create_admin_user(
        username, admin_auth.hash_password(password),
        must_change_password=False)
    app = create_admin_app(state=tmp_db, backend=FakeBackend(),
                           sender=RecordingSender(),
                           config=make_config(tmp_path))
    client = TestClient(app)
    resp = client.post("/api/admin/login",
                       json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return client


def mutate(client, method, path, body=None):
    me = client.get("/api/admin/me").json()
    return client.request(method, path, json=body,
                          headers={"X-CSRF-Token": me["csrf_token"]})


class TestCallbackUrlEndpoint:
    def test_requires_password(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/callback-url", {})
        assert resp.status_code == 400

    def test_wrong_password_rejected(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/callback-url",
                      {"password": "wrong-pass"})
        assert resp.status_code == 403

    def test_returns_readonly_address(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/callback-url",
                      {"password": "initial-pass-1"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "http://127.0.0.1:18002/api/callback"
        assert data["path"] == "/api/callback"
        assert data["method"] == "POST"
        assert data["readonly"] is True


class TestUsernameEndpoint:
    def test_rename_and_login_with_new_name(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/username",
                      {"new_username": "郑佳", "password": "initial-pass-1"})
        assert resp.status_code == 200
        assert resp.json()["username"] == "郑佳"
        assert tmp_db.get_admin_user("郑佳") is not None
        assert tmp_db.get_admin_user("admin") is None

        fresh = TestClient(client.app)
        assert fresh.post("/api/admin/login", json={
            "username": "郑佳", "password": "initial-pass-1"
        }).status_code == 200
        assert fresh.post("/api/admin/login", json={
            "username": "admin", "password": "initial-pass-1"
        }).status_code == 401

    def test_wrong_password_rejected(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/username",
                      {"new_username": "newname", "password": "bad-pass"})
        assert resp.status_code == 400

    def test_same_name_rejected(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/username",
                      {"new_username": "admin", "password": "initial-pass-1"})
        assert resp.status_code == 400

    def test_space_rejected(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/username",
                      {"new_username": "bad name", "password": "initial-pass-1"})
        assert resp.status_code == 400

    def test_too_short_rejected(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        resp = mutate(client, "POST", "/api/admin/username",
                      {"new_username": "a", "password": "initial-pass-1"})
        assert resp.status_code == 400


class TestSlotConfigRejectsBadRemindAt:
    def test_api_rejects_remind_at_equal_start(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        setup(tmp_db)
        resp = mutate(client, "PUT", "/api/admin/groups/%s/config" % GROUP, {
            "checkin_deadlines": {"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "08:30",
                 "remind_at": "08:00"},
            ]},
        })
        assert resp.status_code == 400
        assert "晚于" in resp.json()["detail"]

    def test_api_accepts_valid_remind_at(self, tmp_db, tmp_path):
        client = make_client(tmp_db, tmp_path)
        setup(tmp_db)
        resp = mutate(client, "PUT", "/api/admin/groups/%s/config" % GROUP, {
            "checkin_deadlines": {"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "08:30",
                 "remind_at": "08:20"},
            ]},
        })
        assert resp.status_code == 200
        assert resp.json()["config"]["checkin_slots"][0]["remind_at"] == "08:20"
