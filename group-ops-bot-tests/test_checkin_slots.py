# -*- coding: utf-8 -*-
"""打卡多时间段测试（用户指令验收项 2 / 3：多区间 / 禁重叠 / 立即生效 / 两种话术）。

与 test_checkin_phase2.py 的分工：那里测产品默认截止（am 09:00 / pm 14:50）
与状态 / 补录；本文件测**多区间模型本身**：
- 配置解析（新格式 / 旧格式兼容 / 坏配置回落默认）
- 保存时禁重叠（后台 API 400）+ 运行时禁重叠（脏配置回落默认）双防线
- 立即生效语义：开始已过的区间今天仍执行；结束已过的区间今天不补触发
- 两种触发话术（下一区间开放时间 / 今日已全部完成）
- 区间即额度：一个区间一次机会，⛔ 不跨区间提交
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from app import commands
from app.handlers import checkin as checkin_handler
from app.handlers.base import KIND_A, KIND_B
from doubles.fakes import FakeBackend
from helpers import (GROUP, PHONE, TZ, extract_token, make_config, make_ctx,
                     make_upload)

from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

DEFAULTS = checkin_handler.DEFAULT_CHECKIN_SLOTS
T_0830 = datetime(2026, 10, 13, 8, 30, tzinfo=TZ)   # 三个测试时刻共用
T_1000 = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
T_2000 = datetime(2026, 10, 13, 20, 0, tzinfo=TZ)


def ctx_at(tmp_db, backend, now, group_id=GROUP, phone=PHONE):
    return make_ctx(tmp_db, backend, make_config(Path(tmp_db.db_path).parent),
                    now=now, group_id=group_id, phone=phone)


def config_client(tmp_db, monkeypatch):
    """引导 + 过改密门 + 建群的后台客户端（群配置 PUT 用）。"""
    bootstrap(tmp_db, monkeypatch)
    client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
    assert login(client).status_code == 200
    mutate(client, "POST", "/api/admin/change-password",
           {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert login(client, password=NEW_PASS).status_code == 200
    mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
    return client


def slots_of(tmp_db, group_id=GROUP):
    row = tmp_db.get_group_config(group_id)
    return checkin_handler.slots_from_row(row)


def put_slots(client, slots):
    return mutate(client, "PUT",
                  "/api/admin/groups/%s/config" % GROUP,
                  {"checkin_deadlines": {"slots": slots}})


# ---------- 配置解析 ----------

class TestSlotsParsing:
    def test_new_format_three_slots(self, tmp_db):
        """三个区间：按开始时刻排序后返回（⛔ 不依赖调用方传入顺序）。"""
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "noon", "name": "午间", "start": "12:00", "end": "13:00"},
            {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
            {"id": "pm", "name": "下午", "start": "14:00", "end": "15:00"},
        ]})
        slots = slots_of(tmp_db)
        assert [s["id"] for s in slots] == ["am", "noon", "pm"]
        assert slots[0] == {"id": "am", "name": "上午",
                            "start": "08:00", "end": "09:30"}

    def test_legacy_format_compat(self, tmp_db):
        """旧格式 {"am":"09:00","pm":"14:50"} -> 两个区间（am 00:00~09:00）。"""
        tmp_db.upsert_group_config(GROUP,
                                   checkin_deadlines={"am": "09:00",
                                                      "pm": "14:50"})
        slots = slots_of(tmp_db)
        assert [(s["id"], s["start"], s["end"]) for s in slots] == [
            ("am", "00:00", "09:00"), ("pm", "12:00", "14:50")]

    def test_overlapping_falls_back_to_defaults(self, tmp_db):
        """运行时防线：库里存了重叠配置 -> 回落产品默认，⛔ 不把区间永久藏掉。"""
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "a", "name": "A", "start": "08:00", "end": "10:00"},
            {"id": "b", "name": "B", "start": "09:00", "end": "11:00"},
        ]})
        assert slots_of(tmp_db) == [dict(s) for s in DEFAULTS]

    def test_adjacent_slots_allowed(self, tmp_db):
        """相邻（上一段结束 == 下一段开始）不算重叠。"""
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "a", "name": "A", "start": "08:00", "end": "09:30"},
            {"id": "b", "name": "B", "start": "09:30", "end": "11:00"},
        ]})
        assert len(slots_of(tmp_db)) == 2

    def test_bad_configs_fall_back(self, tmp_db):
        """start >= end / id 重复 / 超过 8 个 / 坏 JSON -> 回落默认。"""
        bad_values = [
            {"slots": [{"id": "a", "start": "10:00", "end": "09:00"}]},
            {"slots": [{"id": "a", "start": "08:00", "end": "09:00"},
                       {"id": "a", "start": "10:00", "end": "11:00"}]},
            {"slots": [{"id": "s%d" % i, "start": "%02d:00" % i,
                        "end": "%02d:30" % i} for i in range(9)]},
            "not-json",
        ]
        for raw in bad_values:
            tmp_db.upsert_group_config(GROUP, checkin_deadlines=raw)
            assert slots_of(tmp_db) == [dict(s) for s in DEFAULTS], raw

    def test_deadlines_compat_view(self, tmp_db):
        """deadlines() 兼容视图：区间 id -> 截止 HH:MM。"""
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "am", "start": "08:00", "end": "09:30"},
            {"id": "pm", "start": "14:00", "end": "15:00"},
        ]})
        ctx = ctx_at(tmp_db, FakeBackend(), T_0830)
        assert checkin_handler.deadlines(ctx) == {"am": "09:30", "pm": "15:00"}


# ---------- 保存时禁重叠（后台 API） ----------

class TestSlotsApiValidation:
    def test_overlap_rejected_400(self, tmp_db, monkeypatch):
        """保存层：区间重叠 -> 400（第一道防线）。"""
        client = config_client(tmp_db, monkeypatch)
        resp = put_slots(client, [
            {"id": "a", "name": "A", "start": "08:00", "end": "10:00"},
            {"id": "b", "name": "B", "start": "09:30", "end": "11:00"},
        ])
        assert resp.status_code == 400
        # ⛔ 未落库（仍是默认）
        assert slots_of(tmp_db) == [dict(s) for s in DEFAULTS]

    def test_valid_multi_slots_saved(self, tmp_db, monkeypatch):
        """保存层：合法多区间 -> 200 + 归一化落库 + 回显 checkin_slots。"""
        client = config_client(tmp_db, monkeypatch)
        resp = put_slots(client, [
            {"id": "pm", "name": "下午", "start": "14:00", "end": "15:00"},
            {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
        ])
        assert resp.status_code == 200
        assert [(s["id"], s["start"], s["end"]) for s in slots_of(tmp_db)] == [
            ("am", "08:00", "09:30"), ("pm", "14:00", "15:00")]
        assert [s["id"] for s in resp.json()["config"]["checkin_slots"]] == [
            "am", "pm"]

    def test_cross_night_and_bad_id_rejected(self, tmp_db, monkeypatch):
        """start >= end（⛔ 不做跨夜区间）/ 坏 id -> 400。"""
        client = config_client(tmp_db, monkeypatch)
        assert put_slots(client, [
            {"id": "night", "start": "22:00", "end": "02:00"}]).status_code == 400
        assert put_slots(client, [
            {"id": "坏区间", "start": "08:00",
             "end": "09:00"}]).status_code == 400

    def test_too_many_slots_rejected(self, tmp_db, monkeypatch):
        """超过 8 个区间 -> 400。"""
        client = config_client(tmp_db, monkeypatch)
        resp = put_slots(client, [
            {"id": "s%d" % i, "start": "%02d:00" % i, "end": "%02d:30" % i}
            for i in range(9)])
        assert resp.status_code == 400


# ---------- 立即生效语义（用户指令验收项 2） ----------

class TestImmediateEffect:
    def test_started_slot_still_runs_today(self, tmp_db):
        """新增区间的开始时刻已过（08:00 < 08:30）：今天仍执行。"""
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
            ]})
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, T_0830))
        assert "#t=" in reply  # 08:30 落在 08:00~09:30 内 -> 正常签发

    def test_ended_slot_not_reopened_today(self, tmp_db):
        """结束时刻已过的区间（07:00~08:00）今天不补开放、不补触发。"""
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "early", "name": "早场", "start": "06:00", "end": "08:00"},
            ]})
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, T_0830))
        assert reply == ("当前不在打卡时段，今日开放时间为 06:00-08:00，"
                         "已过时段不支持补交，请找管理员补交或修复")

    def test_new_slot_effective_same_day_via_api(self, tmp_db, monkeypatch):
        """后台 10:00 前新增 10:30~11:30 区间：当天 10:35 即可打卡（不等次日）。"""
        client = config_client(tmp_db, monkeypatch)
        mutate(client, "PUT", "/api/admin/groups/%s/config" % GROUP,
               {"switches": {"checkin": True}})
        resp = put_slots(client, [
            {"id": "late", "name": "加场", "start": "10:30", "end": "11:30"},
        ])
        assert resp.status_code == 200
        backend = FakeBackend()
        # 10:35：新区间已开始 -> 立即可用
        reply = checkin_handler.handle_command(
            commands.parse("打卡"),
            ctx_at(tmp_db, backend, datetime(2026, 10, 13, 10, 35, tzinfo=TZ)))
        assert "#t=" in reply

    def test_next_open_uses_next_slot_start(self, tmp_db):
        """多区间未到点：提示 = 下一区间开始时间（不是上一区间截止）。"""
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "09:00"},
                {"id": "noon", "name": "午间", "start": "10:30", "end": "11:30"},
            ]})
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, T_1000))
        assert reply == ("当前不在打卡时段，开放时间为 10:30（午间）")


# ---------- 两种话术（多区间场景） ----------

class TestTwoPhrasingsMultiSlot:
    def test_all_done_after_last_slot(self, tmp_db):
        """20:00 三个区间全部结束 -> 自解释话术（含今日全部开放时间）。"""
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
                {"id": "pm", "name": "下午", "start": "14:00", "end": "15:00"},
            ]})
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, T_2000))
        # 2026-10-16 指令 2「禁止静默」：必须报时段 + 开放时间
        assert reply == ("当前不在打卡时段，今日开放时间为 08:00-09:30、"
                         "14:00-15:00，已过时段不支持补交，请找管理员补交或修复")
        # ⛔ 未签发令牌
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 0

    def test_h5_submit_outside_slots_blocked(self, tmp_db):
        """第二道防线：区间内签发、区间外 H5 提交 -> 同样两种话术。

        时刻约束：签发 09:20 / 提交 09:35 必须同在令牌 30 分钟有效期内
        （否则先命中过期话术，测不到区间判定）。
        """
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
                {"id": "pm", "name": "下午", "start": "14:00", "end": "15:00"},
            ]})
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend,
                     datetime(2026, 10, 13, 9, 20, tzinfo=TZ))
        token = extract_token(checkin_handler.handle_command(
            commands.parse("打卡"), ctx))
        late = ctx_at(tmp_db, backend,
                      datetime(2026, 10, 13, 9, 35, tzinfo=TZ))
        result = checkin_handler.submit_from_h5(late, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "当前不在打卡时段" in result.message
        assert "14:00" in result.message  # 下一区间开始时间
        info = checkin_handler.token_info(late, token)
        assert info["ok"] is False and info["reason"] == "not_open"


# ---------- 区间即额度 ----------

class TestSlotScopedQuota:
    SLOTS = [{"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
             {"id": "pm", "name": "下午", "start": "14:00", "end": "15:00"}]

    def _setup(self, tmp_db):
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": self.SLOTS})

    def test_each_slot_is_separate_quota(self, tmp_db):
        """上午打卡完成不影响下午：两个区间各一次机会。"""
        self._setup(tmp_db)
        backend = FakeBackend()
        am_ctx = ctx_at(tmp_db, backend, T_0830)
        token = extract_token(checkin_handler.handle_command(
            commands.parse("打卡"), am_ctx))
        assert checkin_handler.submit_from_h5(
            am_ctx, token, make_upload(tmp_db, token), None).kind == KIND_A
        # 同区间再打 -> 已完成
        assert "已完成" in checkin_handler.handle_command(
            commands.parse("打卡"), am_ctx)
        # 换到下午区间 -> 可再打一次
        pm_ctx = ctx_at(tmp_db, backend,
                        datetime(2026, 10, 13, 14, 30, tzinfo=TZ))
        reply = checkin_handler.handle_command(commands.parse("打卡"), pm_ctx)
        assert "#t=" in reply and "下午" in reply

    def test_am_token_rejected_in_pm_window(self, tmp_db):
        """上午令牌拿到下午窗口提交 -> 拒绝（⛔ 不跨区间）。

        时刻约束：两区间相邻（09:30 分界），令牌 09:20 签发、09:40 提交，
        仍在 30 分钟有效期内 -> 命中时段绑定拒绝而非过期。
        """
        tmp_db.upsert_group_config(
            GROUP, biz_switches={"checkin": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
                {"id": "pm", "name": "下午", "start": "09:30", "end": "10:30"},
            ]})
        backend = FakeBackend()
        am_ctx = ctx_at(tmp_db, backend,
                        datetime(2026, 10, 13, 9, 20, tzinfo=TZ))
        token = extract_token(checkin_handler.handle_command(
            commands.parse("打卡"), am_ctx))
        pm_ctx = ctx_at(tmp_db, backend,
                        datetime(2026, 10, 13, 9, 40, tzinfo=TZ))
        result = checkin_handler.submit_from_h5(pm_ctx, token, "img/1.jpg",
                                                None)
        assert result.kind == KIND_B
        assert "不适用于当前周期" in result.message
        # ⛔ 未落任何提交
        assert len([c for c in backend.calls if c[0] == "checkin_submit"]) == 0

    def test_ended_slot_ids_for_reminder(self, tmp_db):
        """提醒判定：已结束的区间 id（10:00 时上午已结束、下午未结束）。"""
        self._setup(tmp_db)
        ctx = ctx_at(tmp_db, FakeBackend(), T_1000)
        slots = checkin_handler.configured_slots(ctx)
        assert checkin_handler.ended_slot_ids(slots, ctx.now) == ["am"]
