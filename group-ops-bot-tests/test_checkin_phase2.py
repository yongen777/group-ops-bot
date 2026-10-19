# -*- coding: utf-8 -*-
"""打卡业务第二阶段测试（用户指令：时段/截止/需图配置、免图直提、
NORMAL/LATE/ADMIN_EDIT、截止阻断、后台补录）。

与 test_handlers.py::TestCheckin 的分工：那里用宽松截止测通用链路；
本文件用产品默认截止（am 09:00 / pm 14:50）测新增规则。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app.handlers import checkin as checkin_handler
from app.handlers.base import KIND_A, KIND_B, KIND_C
from app.tokens import token_id_of
from doubles.fakes import FakeBackend
from helpers import (GROUP, PHONE, TZ, enable_all, extract_token, make_config,
                     make_ctx)
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)      # 上午（已过 09:00 默认截止）
PM = datetime(2026, 10, 13, 15, 0, tzinfo=TZ)      # 下午（已过 14:50 默认截止）
EARLY_AM = datetime(2026, 10, 13, 8, 0, tzinfo=TZ)  # 上午（未过 09:00）
# 令牌 30 分钟有效期（第七阶段）：要测「截止后仍持有效令牌」，
# 签发与提交必须落在同一 30 分钟窗口内，故取贴着截止的两个时刻
NEAR_AM_DEADLINE = datetime(2026, 10, 13, 8, 50, tzinfo=TZ)   # 截止前 10 分钟
PAST_AM_DEADLINE = datetime(2026, 10, 13, 9, 5, tzinfo=TZ)    # 截止后 5 分钟


def ctx_at(tmp_db, backend, now, phone=PHONE, group_id=GROUP):
    from pathlib import Path
    return make_ctx(tmp_db, backend,
                    make_config(Path(tmp_db.db_path).parent), now=now,
                    phone=phone, group_id=group_id)


def product_defaults(tmp_db):
    """产品默认截止 am 09:00 / pm 14:50（后台未改配置时的行为）。"""
    enable_all(tmp_db)
    tmp_db.upsert_group_config(
        GROUP, checkin_deadlines={"am": "09:00", "pm": "14:50"})


class TestDeadlineBlocking:
    """截止 / 未开放判定（多时间段后用户话术只有两种，用户指令验收项 3）。"""

    def test_command_outside_slots_shows_next_open(self, tmp_db):
        """10:00 不在上午区间（00:00~09:00），下午场未开始 -> 提示开放时间。"""
        product_defaults(tmp_db)
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, AM))
        assert "当前不在打卡时段" in reply and "12:00" in reply
        # ⛔ 未签发任何令牌
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 0
        assert backend.calls == []

    def test_command_after_all_slots_done(self, tmp_db):
        """15:00 两个区间都已结束 -> 今日已全部完成，请找管理员补交/修复。"""
        product_defaults(tmp_db)
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, PM))
        assert reply == ("当前不在打卡时段，今日开放时间为 00:00-09:00、"
                         "12:00-14:50，已过时段不支持补交，请找管理员补交或修复")
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 0
        assert backend.calls == []

    def test_command_ok_before_deadline(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, EARLY_AM))
        assert "#t=" in reply  # 08:00 在上午区间内 -> 正常签发

    def test_h5_submit_blocked_outside_slot(self, tmp_db):
        """令牌在区间内签发，区间结束后从 H5 提交 -> 阻断（第二道防线）。"""
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, NEAR_AM_DEADLINE)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        token = extract_token(reply)
        # 时钟推到截止后，但仍在令牌 30 分钟有效期内 -> 命中区间判定而非过期
        late_ctx = ctx_at(tmp_db, backend, PAST_AM_DEADLINE)
        result = checkin_handler.submit_from_h5(
            late_ctx, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "当前不在打卡时段" in result.message

    def test_token_info_reports_not_open(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, NEAR_AM_DEADLINE)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        token = extract_token(reply)
        late_ctx = ctx_at(tmp_db, backend, PAST_AM_DEADLINE)
        info = checkin_handler.token_info(late_ctx, token)
        assert info["ok"] is False
        assert info["reason"] == "not_open"

    def test_group_configured_deadline_respected(self, tmp_db):
        """后台把上午区间结束改到 10:30：10:00 可提交，10:45 不在任何区间。"""
        enable_all(tmp_db)
        tmp_db.upsert_group_config(
            GROUP, checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "00:00", "end": "10:30"},
                {"id": "pm", "name": "下午", "start": "12:00", "end": "14:50"}]})
        backend = FakeBackend()
        ok_ctx = ctx_at(tmp_db, backend, AM)
        assert "#t=" in checkin_handler.handle_command(
            commands.parse("打卡"), ok_ctx)
        late_ctx = ctx_at(tmp_db, backend,
                          datetime(2026, 10, 13, 10, 45, tzinfo=TZ))
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), late_ctx)
        assert "当前不在打卡时段" in reply and "12:00" in reply


class TestNoImageDirectSubmit:
    def test_direct_submit_when_image_not_required(self, tmp_db):
        """关闭需图：@机器人 打卡 -> 直接完成（无令牌、无 H5）。"""
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_require_image=0)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, EARLY_AM)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        # 2026-10-16 四：三业务成功话术统一为总结格式风格
        assert reply == "✅ 上午打卡已成功完成。"
        # ⛔ 未签发令牌
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 0
        # 后端收到：免图（image_ref 空串）+ 状态 NORMAL + 来源 user
        name, kwargs = backend.calls[-1]
        assert name == "checkin_submit"
        assert kwargs["image_ref"] == ""
        assert kwargs["status"] == "NORMAL"
        assert kwargs["entry_source"] == "user"

    def test_direct_submit_outside_slot_blocked(self, tmp_db):
        """免图模式同样受区间门控：10:00 不在任何区间 -> 不提交。"""
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_require_image=0,
                                   checkin_deadlines={"am": "09:00",
                                                      "pm": "14:50"})
        backend = FakeBackend()
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, AM))
        assert "当前不在打卡时段" in reply
        assert backend.calls == []

    def test_direct_submit_duplicate_rejected(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_require_image=0)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, EARLY_AM)
        checkin_handler.handle_command(commands.parse("打卡"), ctx)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        assert "已完成" in reply

    def test_direct_submit_backend_down_is_c(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_require_image=0)
        backend = FakeBackend()
        backend.raise_on.add("checkin_submit")
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), ctx_at(tmp_db, backend, EARLY_AM))
        assert "未知" in reply  # C 类话术，⛔ 不当成功
        assert len(tmp_db.pending_recovery_tasks()) == 1


class TestTokenExpiryAnchoredToDeadline:
    def test_token_expires_30min_cap(self, tmp_db):
        """令牌有效期 = 签发后 30 分钟（第七阶段硬上限）。

        ⛔ 打卡原本按「时段截止 + 24h」，第七阶段起一律封顶到 30 分钟：
           链接越短命，转发/滞留的窗口越小。
        """
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, EARLY_AM)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        token = extract_token(reply)
        row = tmp_db.get_token(token_id_of(token))
        expires = datetime.fromisoformat(row["expires_at"])
        assert expires == EARLY_AM + timedelta(minutes=30)


class TestAdminBackfill:
    def _ctx(self, tmp_db, backend, now=AM, phone=PHONE):
        return ctx_at(tmp_db, backend, now, phone=phone)

    def test_backfill_records_admin_edit(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = self._ctx(tmp_db, backend)
        result = checkin_handler.admin_backfill(
            ctx, "2026-10-13", "am", "ADMIN_EDIT")
        assert result.kind == KIND_A
        name, kwargs = backend.calls[-1]
        assert name == "checkin_submit"
        assert kwargs["status"] == "ADMIN_EDIT"
        assert kwargs["entry_source"] == "admin"
        # 补录占额度：同范围用户不能再提交
        reply = checkin_handler.handle_command(
            commands.parse("打卡"), self._ctx(tmp_db, backend, EARLY_AM,
                                              phone=PHONE))
        # 注意：EARLY_AM 是另一时钟，但范围键同（同日 am）-> 已完成
        assert "已完成" in reply

    def test_backfill_duplicate_rejected(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = self._ctx(tmp_db, backend)
        assert checkin_handler.admin_backfill(
            ctx, "2026-10-13", "am").kind == KIND_A
        result = checkin_handler.admin_backfill(
            ctx, "2026-10-13", "am")
        assert result.kind == KIND_B
        assert "不能重复补录" in result.message

    def test_backfill_status_late_allowed(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = self._ctx(tmp_db, backend)
        result = checkin_handler.admin_backfill(
            ctx, "2026-10-13", "pm", "LATE")
        assert result.kind == KIND_A
        assert backend.calls[-1][1]["status"] == "LATE"

    def test_backfill_invalid_inputs(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        ctx = self._ctx(tmp_db, backend)
        assert checkin_handler.admin_backfill(
            ctx, "2026-10-13", "noon").kind == KIND_B
        assert checkin_handler.admin_backfill(
            ctx, "2026-10-13", "am", "WEIRD").kind == KIND_B

    def test_backfill_backend_down_is_c(self, tmp_db):
        product_defaults(tmp_db)
        backend = FakeBackend()
        backend.raise_on.add("checkin_submit")
        ctx = self._ctx(tmp_db, backend)
        result = checkin_handler.admin_backfill(ctx, "2026-10-13", "am")
        assert result.kind == KIND_C


class TestAdminBackfillApi:
    """补录经管理后台 API（鉴权 + CSRF + 审计 + 409 语义）。"""

    def _client(self, tmp_db, monkeypatch):
        from test_admin import admin_client, bootstrap, login, mutate, \
            ADMIN_PASS, NEW_PASS
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        return client

    def test_backfill_endpoint(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        product_defaults(tmp_db)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/checkin" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "session": "am", "status": "ADMIN_EDIT"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 重复补录 -> 409
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/checkin" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "session": "am"})
        assert resp.status_code == 409
        # 审计留痕
        audit = client.get("/api/admin/audit").json()["audit"]
        actions = [a["action"] for a in audit]
        assert "backfill.checkin" in actions

    def test_backfill_requires_auth(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        resp = client.post(
            "/api/admin/groups/%s/backfill/checkin" % GROUP,
            json={"user_id": PHONE, "period_key": "2026-10-13",
                  "session": "am"})
        assert resp.status_code == 401
