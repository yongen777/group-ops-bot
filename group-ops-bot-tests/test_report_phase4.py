# -*- coding: utf-8 -*-
"""报数业务第四阶段测试（用户指令：非负整数校验、截止阻断、
每日一次、后台补录）。

与 test_handlers.py::TestReport 的分工：那里用默认 19:00 截止测通用链路；
本文件用群级 daily_cutoff（Q-BIZ5：报数截止默认随日报）测新增规则。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app.handlers import report as report_handler
from app.handlers.base import KIND_A, KIND_B, KIND_C
from app.tokens import token_id_of
from doubles.fakes import FakeBackend
from helpers import (GROUP, PHONE, TZ, enable_all, make_config, make_ctx,
                     setup_fields)
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
EARLY = datetime(2026, 10, 13, 9, 30, tzinfo=TZ)
LATE = datetime(2026, 10, 13, 10, 45, tzinfo=TZ)
# 令牌 30 分钟有效期（第七阶段）：测「截止后仍持有效令牌」时，
# 签发与提交必须落在同一 30 分钟窗口内
NEAR_CUTOFF = datetime(2026, 10, 13, 9, 50, tzinfo=TZ)    # 截止前 10 分钟
PAST_CUTOFF = datetime(2026, 10, 13, 10, 10, tzinfo=TZ)   # 截止后 10 分钟


def ctx_at(tmp_db, backend, now, phone=PHONE, group_id=GROUP):
    from pathlib import Path
    return make_ctx(tmp_db, backend,
                    make_config(Path(tmp_db.db_path).parent), now=now,
                    phone=phone, group_id=group_id)


def cutoff_at(tmp_db, hhmm):
    """配群级 daily_cutoff（报数截止权威，Q-BIZ5）。"""
    enable_all(tmp_db)
    setup_fields(tmp_db)
    tmp_db.upsert_group_config(GROUP, daily_cutoff=hhmm)


def issue_token(tmp_db, now, hhmm="23:59"):
    cutoff_at(tmp_db, hhmm)
    backend = FakeBackend()
    ctx = ctx_at(tmp_db, backend, now)
    reply = report_handler.handle_command(commands.parse("报数"), ctx)
    token = reply.split("#t=")[1].split("\n")[0].strip()
    return backend, ctx, token


class TestNonNegativeInteger:
    """quantity 必须为非负整数（用户指令第四阶段）。"""

    def _submit(self, tmp_db, quantity):
        backend, ctx, token = issue_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": quantity}])
        return backend, result

    def test_positive_int_ok(self, tmp_db):
        _backend, result = self._submit(tmp_db, 3)
        assert result.kind == KIND_A

    def test_zero_ok(self, tmp_db):
        """非负：0 是合法数量。"""
        _backend, result = self._submit(tmp_db, 0)
        assert result.kind == KIND_A

    def test_negative_rejected(self, tmp_db):
        _backend, result = self._submit(tmp_db, -1)
        assert result.kind == KIND_B
        assert "整数" in result.message

    def test_fractional_rejected(self, tmp_db):
        _backend, result = self._submit(tmp_db, 1.5)
        assert result.kind == KIND_B
        assert "整数" in result.message

    def test_integral_float_coerced(self, tmp_db):
        """2.0 是整数值：接受并归一为 int 2（快照不带浮点）。"""
        backend, result = self._submit(tmp_db, 2.0)
        assert result.kind == KIND_A
        name, kwargs = backend.calls[-1]
        assert name == "report_submit"
        assert kwargs["items"][0]["quantity"] == 2
        assert isinstance(kwargs["items"][0]["quantity"], int)

    def test_string_rejected(self, tmp_db):
        _backend, result = self._submit(tmp_db, "3")
        assert result.kind == KIND_B

    def test_bool_rejected(self, tmp_db):
        """bool 是 int 子类：⛔ 不当数量（防 true 变 1）。"""
        _backend, result = self._submit(tmp_db, True)
        assert result.kind == KIND_B

    def test_nan_inf_rejected(self, tmp_db):
        _backend, result = self._submit(tmp_db, float("nan"))
        assert result.kind == KIND_B
        _backend, result = self._submit(tmp_db, float("inf"))
        assert result.kind == KIND_B

    def test_bad_quantity_never_reaches_backend(self, tmp_db):
        backend, _ctx, token = issue_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            _ctx, token, [{"field_id": 1, "quantity": 1.5}])
        assert result.kind == KIND_B
        assert not any(c[0] == "report_submit" for c in backend.calls)


class TestReportWholeDayWindow:
    """整天窗口（⭐ 2026-10-19 用户指令：⛔ 不再使用截止时间）。

    旧的「截止后阻断」三条测试已按新口径反转：当日任意时刻（含原截止
    时刻之后）命令签发 / H5 初始化 / H5 提交都必须放行。
    """

    def test_command_ok_after_old_cutoff(self, tmp_db):
        cutoff_at(tmp_db, "09:00")
        backend = FakeBackend()
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        assert "#t=" in reply
        assert "已截至" not in reply
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 1

    def test_command_ok_before_cutoff(self, tmp_db):
        cutoff_at(tmp_db, "10:30")
        backend = FakeBackend()
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        assert "#t=" in reply

    def test_group_configured_cutoff_ignored(self, tmp_db):
        """后台残留 daily_cutoff=10:30：⛔ 不再影响报数（整天执行）。"""
        cutoff_at(tmp_db, "10:30")
        backend = FakeBackend()
        assert "#t=" in report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, LATE))
        assert "截止" not in reply and "#t=" in reply

    def test_h5_submit_ok_after_old_cutoff(self, tmp_db):
        """令牌在 10:00 签发，10:10 从 H5 提交 -> 照常成功（令牌被消费）。"""
        backend, ctx, token = issue_token(tmp_db, NEAR_CUTOFF, hhmm="10:00")
        late_ctx = ctx_at(tmp_db, backend, PAST_CUTOFF)
        result = report_handler.submit_from_h5(
            late_ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"

    def test_token_info_ok_after_old_cutoff(self, tmp_db):
        backend, _ctx, token = issue_token(tmp_db, NEAR_CUTOFF, hhmm="10:00")
        late_ctx = ctx_at(tmp_db, backend, PAST_CUTOFF)
        info = report_handler.token_info(late_ctx, token)
        assert info["ok"] is True
        assert info["fields"]

    def test_default_cutoff_no_longer_matters(self, tmp_db):
        """群未改配置：原默认 19:00 已废弃，22:00 仍可签发。"""
        enable_all(tmp_db)
        setup_fields(tmp_db)
        backend = FakeBackend()
        night = datetime(2026, 10, 13, 22, 0, tzinfo=TZ)
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, night))
        assert "#t=" in reply


class TestTokenExpiryAnchoredToCutoff:
    def test_token_expires_30min_cap(self, tmp_db):
        """令牌有效期 = 签发后 30 分钟（第七阶段硬上限）。

        ⛔ 即便把截止配到 23:00（+24h 会到次日），也一律封顶到签发后 30 分钟。
        """
        _backend, _ctx, token = issue_token(tmp_db, AM, hhmm="23:00")
        row = tmp_db.get_token(token_id_of(token))
        expires = datetime.fromisoformat(row["expires_at"])
        assert expires == AM + timedelta(minutes=30)


class TestDailyOnce:
    def test_already_done_command_no_new_token(self, tmp_db):
        backend, ctx, token = issue_token(tmp_db, AM)
        assert report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}]).kind == KIND_A
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        assert "今日已提交报数" in reply
        assert "不可重复提交" in reply


class TestReportAdminBackfill:
    def _items(self, field_id=1, quantity=3):
        return [{"field_id": field_id, "quantity": quantity}]

    def test_backfill_after_cutoff_ok(self, tmp_db):
        """补录不检查截止（entry_source=admin 留痕）。"""
        cutoff_at(tmp_db, "09:00")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)  # 已过截止
        result = report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", self._items())
        assert result.kind == KIND_A
        name, kwargs = backend.calls[-1]
        assert name == "report_submit"
        assert kwargs["entry_source"] == "admin"

    def test_backfill_duplicate_rejected(self, tmp_db):
        """报数无覆盖语义：同范围已成功 -> ⛔ 不重复补录。"""
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", self._items()).kind == KIND_A
        result = report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", self._items())
        assert result.kind == KIND_B
        assert "不能重复补录" in result.message

    def test_backfill_blocks_user_path_too(self, tmp_db):
        """补录占额度：同范围用户命令路径也判已完成。"""
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        report_handler.admin_backfill(ctx, "2026-10-13", "开发", self._items())
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        assert "今日已提交报数" in reply
        assert "不可重复提交" in reply

    def test_backfill_invalid_role(self, tmp_db):
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "", self._items()).kind == KIND_B
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "x" * 33, self._items()).kind == KIND_B
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", None, self._items()).kind == KIND_B

    def test_backfill_invalid_items(self, tmp_db):
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        # 未知字段
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "开发",
            [{"field_id": 999, "quantity": 1}]).kind == KIND_B
        # 负整数 / 小数 / 空项
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "开发",
            [{"field_id": 1, "quantity": -1}]).kind == KIND_B
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "开发",
            [{"field_id": 1, "quantity": 1.5}]).kind == KIND_B
        assert report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", []).kind == KIND_B

    def test_backfill_integral_float_coerced(self, tmp_db):
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        result = report_handler.admin_backfill(
            ctx, "2026-10-13", "开发",
            [{"field_id": 1, "quantity": 2.0}])
        assert result.kind == KIND_A
        assert backend.calls[-1][1]["items"][0]["quantity"] == 2

    def test_backfill_backend_down_is_c(self, tmp_db):
        cutoff_at(tmp_db, "23:59")
        backend = FakeBackend()
        backend.raise_on.add("report_submit")
        ctx = ctx_at(tmp_db, backend, AM)
        result = report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", self._items())
        assert result.kind == KIND_C

    def test_backfill_api_endpoint(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        cutoff_at(tmp_db, "23:59")
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/report" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "role": "开发",
                       "items": [{"field_id": 1, "quantity": 3}]})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 重复补录 -> 409
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/report" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "role": "开发",
                       "items": [{"field_id": 1, "quantity": 3}]})
        assert resp.status_code == 409
        # 角色为空 -> 400
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/report" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "role": "", "items": [{"field_id": 1,
                                              "quantity": 3}]})
        assert resp.status_code == 400
        # 非法 items（小数）-> 409（业务校验在处理器）
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/report" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-14",
                       "role": "开发",
                       "items": [{"field_id": 1, "quantity": 1.5}]})
        assert resp.status_code == 409
