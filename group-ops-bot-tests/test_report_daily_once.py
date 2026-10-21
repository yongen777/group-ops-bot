# -*- coding: utf-8 -*-
"""报数 / 总结「每日一次 + 截止话术」验收测试（用户指令验收项 4）。

用户原话口径：
- 报数每日一次，暂不开放重复提交；当日已提交 -> 「今日已提交，不可重复提交」
- 报数和总结超过截止提交 -> 「已截至，请找管理员补交/修复」
- ⛔ 阻断不误伤管理员后台补录（补录的本质就是补截止后的账）

与 test_report_phase4.py / test_summary_phase3.py 的分工：那两文件按业务
细节铺开；本文件按**用户验收清单**逐条对口径（命令 / H5 / 后台三路径）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app.handlers import report as report_handler
from app.handlers import summary as summary_handler
from app.handlers.base import KIND_A, KIND_B
from app.tokens import token_id_of
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx, \
    setup_fields

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
NEXT_DAY = datetime(2026, 10, 14, 10, 0, tzinfo=TZ)
NEAR_CUTOFF = datetime(2026, 10, 13, 9, 50, tzinfo=TZ)    # 截止前 10 分钟
PAST_CUTOFF = datetime(2026, 10, 13, 10, 10, tzinfo=TZ)   # 截止后 10 分钟


def ctx_at(tmp_db, backend, now, phone=PHONE, group_id=GROUP, text=""):
    from pathlib import Path
    return make_ctx(tmp_db, backend,
                    make_config(Path(tmp_db.db_path).parent), now=now,
                    phone=phone, group_id=group_id, text=text)


def setup(tmp_db, cutoff="09:00"):
    enable_all(tmp_db)
    setup_fields(tmp_db)
    tmp_db.upsert_group_config(GROUP, daily_cutoff=cutoff)


def issue_report_token(tmp_db, now, cutoff="23:59"):
    setup(tmp_db, cutoff)
    backend = FakeBackend()
    ctx = ctx_at(tmp_db, backend, now)
    reply = report_handler.handle_command(commands.parse("报数"), ctx)
    token = reply.split("#t=")[1].split("\n")[0].strip()
    return backend, ctx, token


# ---------- 报数：每日一次 ----------

class TestReportDailyOnce:
    def test_command_after_submit_no_new_token(self, tmp_db):
        """当日已提交 -> 命令直接提醒，⛔ 不再签发新令牌。"""
        backend, ctx, token = issue_report_token(tmp_db, AM)
        assert report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}]).kind == KIND_A
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, AM))
        assert reply == ("今日已提交报数，不可重复提交；"
                         "如需调整请联系后台管理员修改。")
        # ⛔ 没有第二个令牌
        rows = tmp_db._conn.execute(
            "SELECT token_id FROM tokens").fetchall()
        assert len(rows) == 1 and rows[0]["token_id"] == token_id_of(token)

    def test_next_day_is_new_quota(self, tmp_db):
        """跨到次日 -> 新额度（每日一次按 period_key 隔离）。"""
        backend, ctx, token = issue_report_token(tmp_db, AM)
        report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, NEXT_DAY))
        assert "#t=" in reply

    def test_h5_resubmit_same_link_is_duplicate_a(self, tmp_db):
        """同链接重复提交（安全重试）：归 A 语义 + duplicate 标记，
        ⛔ 不落第二条记录。"""
        backend, ctx, token = issue_report_token(tmp_db, AM)
        first = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert first.kind == KIND_A and first.duplicate is False
        second = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 5}])
        assert second.kind == KIND_A and second.duplicate is True
        submits = [c for c in backend.calls if c[0] == "report_submit"]
        assert len(submits) == 1

    def test_different_user_not_blocked(self, tmp_db):
        """每日一次按人隔离：⛔ 不拿同一个人的完成态拦另一个人。"""
        backend, ctx, token = issue_report_token(tmp_db, AM)
        report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        other = ctx_at(tmp_db, backend, AM, phone="13800138010")
        reply = report_handler.handle_command(commands.parse("报数"), other)
        assert "#t=" in reply


# ---------- 报数：整天窗口（⭐ 2026-10-19 取消截止时间） ----------

class TestReportWholeDayWindow:
    def test_command_any_time_issues_token(self, tmp_db):
        """命令侧：当日任意时刻（含原「截止」之后）都能签发令牌。

        ⭐ 2026-10-19 用户指令：报数整天执行，⛔ 不再有截止阻断。
        """
        setup(tmp_db, "09:00")
        backend = FakeBackend()
        reply = report_handler.handle_command(
            commands.parse("报数"), ctx_at(tmp_db, backend, PAST_CUTOFF))
        assert "/h5/report#t=" in reply
        assert "已截至" not in reply
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"] == 1

    def test_h5_submit_any_time_ok(self, tmp_db):
        """H5 侧：原「截止」之后提交照常成功（令牌被消费）。"""
        backend, ctx, token = issue_report_token(tmp_db, NEAR_CUTOFF,
                                                 cutoff="10:00")
        late = ctx_at(tmp_db, backend, PAST_CUTOFF)
        result = report_handler.submit_from_h5(
            late, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"

    def test_token_info_any_time_ok(self, tmp_db):
        """H5 初始化：原「截止」之后打开表单正常返回字段池。"""
        _b, _c, token = issue_report_token(tmp_db, NEAR_CUTOFF, cutoff="10:00")
        backend = FakeBackend()
        info = report_handler.token_info(
            ctx_at(tmp_db, backend, PAST_CUTOFF), token)
        assert info["ok"] is True
        assert info["fields"]

    def test_admin_backfill_still_ok(self, tmp_db):
        """后台补录路径不受影响（admin 留痕照旧）。"""
        setup(tmp_db, "09:00")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        result = report_handler.admin_backfill(
            ctx, "2026-10-13", "开发", [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        name, kwargs = backend.calls[-1]
        assert name == "report_submit" and kwargs["entry_source"] == "admin"


# ---------- 总结：整天 / 整周窗口（⛔ 不再有截止阻断） ----------

class TestSummaryWholeDayWindow:
    def test_pending_text_after_old_cutoff_saves(self, tmp_db):
        """两轮第二轮在原「截止」之后提交 -> 照常保存。

        ⭐ 2026-10-19：总结整天执行，周期键（自然日）本身就是边界。
        """
        setup(tmp_db, "09:00")
        backend = FakeBackend()
        summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, AM))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, PAST_CUTOFF, text="今天完成了X"))
        saves = [c for c in backend.calls if c[0] == "summary_save"]
        assert len(saves) == 1
        # 成功走 @ 投递时返回空串（避免同话术出现两次）；未接通则回话术
        assert reply == "" or "已成功完成" in reply
        assert "已截至" not in reply

    def test_confirm_overwrite_after_old_cutoff_works(self, tmp_db):
        """确认覆盖在原「截止」之后确认 -> 覆盖成功（⛔ 不再拦）。"""
        setup(tmp_db, "10:05")
        backend = FakeBackend()
        for content in ("第一版", "第二版"):
            summary_handler.handle_command(
                commands.parse("总结"), ctx_at(tmp_db, backend, AM))
            summary_handler.handle_pending_text(
                ctx_at(tmp_db, backend, AM, text=content))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=8), text="确认覆盖"))
        assert "已截至" not in reply
        saves = [c for c in backend.calls if c[0] == "summary_save"]
        assert len(saves) == 2  # 首存 + 覆盖

    def test_cross_day_goes_to_new_period(self, tmp_db):
        """跨过自然日 -> 进入新周期（周期键变化，⛔ 不是「截止拦截」）。"""
        setup(tmp_db, "09:00")
        backend = FakeBackend()
        summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, AM))
        summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM, text="第一天"))
        summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, NEXT_DAY))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, NEXT_DAY, text="第二天"))
        assert "已截至" not in reply
        periods = [c[1]["period_key"] for c in backend.calls
                   if c[0] == "summary_save"]
        assert periods == ["2026-10-13", "2026-10-14"]

    def test_admin_backfill_still_ok(self, tmp_db):
        setup(tmp_db, "09:00")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        result = summary_handler.admin_backfill(
            ctx, "2026-10-13", "daily", "补录的总结内容")
        assert result.kind == KIND_A
        assert backend.calls[-1][1]["entry_source"] == "admin"
