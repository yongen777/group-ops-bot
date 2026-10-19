# -*- coding: utf-8 -*-
"""H5 令牌 PENDING_VERIFY 重复提交防护测试（用户指令 P1 验收项 5）。

修的隐患：ACTIVE 令牌提交后进入 PENDING_VERIFY（C 类结果未知）期间，
原实现允许同一链接再次独立提交 —— 未知结论的窗口内同一操作可能落两条
记录。现状态机：
    active --提交发起--> pending_verify --A--> consumed
                                      --B--> active（明确失败，可修正重试）
                                      --C--> pending_verify（保持，等恢复结论）

本文件验证：pending_verify 期间同链接再提交被拒（SUBMITTING）、
同范围新令牌也被挡（has_pending_scope）、恢复结论落地后令牌按结论流转。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import commands
from app.handlers import checkin as checkin_handler
from app.handlers import report as report_handler
from app.handlers.base import KIND_A, KIND_B, KIND_C
from app.tokens import TokenState
from doubles.fakes import FakeBackend
from helpers import (GROUP, PHONE, TZ, enable_all, extract_token, make_config,
                     make_ctx, make_upload, setup_fields)

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
SUBMITTING_TEXT = ("该链接的提交正在处理中，请稍候再试"
                   "（请勿重复提交）")


def ctx_at(tmp_db, backend, now=AM, phone=PHONE, group_id=GROUP):
    from pathlib import Path
    return make_ctx(tmp_db, backend, make_config(Path(tmp_db.db_path).parent),
                    now=now, phone=phone, group_id=group_id)


def setup(tmp_db):
    """打卡 + 报数全开；打卡区间覆盖 10:00（多区间格式）。"""
    enable_all(tmp_db)
    setup_fields(tmp_db)
    tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
        {"id": "am", "name": "上午", "start": "00:00", "end": "12:00"},
        {"id": "pm", "name": "下午", "start": "12:00", "end": "23:59"},
    ]})


def issue_checkin_token(tmp_db, now=AM):
    backend = FakeBackend()
    ctx = ctx_at(tmp_db, backend, now)
    reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
    token = extract_token(reply)
    # image_ref 真源：与令牌绑定的上传凭证（§4.11.4 D4 定夺后的硬校验）
    ref = make_upload(tmp_db, token)
    return backend, ctx, token, ref


def issue_report_token(tmp_db, now=AM):
    backend = FakeBackend()
    ctx = ctx_at(tmp_db, backend, now)
    reply = report_handler.handle_command(commands.parse("报数"), ctx)
    return backend, ctx, extract_token(reply)


# ---------- 打卡：C 类窗口内禁重复提交 ----------

class TestCheckinPendingVerify:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_db):
        setup(tmp_db)

    def test_second_submit_rejected_during_window(self, tmp_db):
        """后端不可用（C）-> 令牌 pending_verify -> 再提交被拒。"""
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        backend.raise_on.add("checkin_submit")
        first = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert first.kind == KIND_C
        row = tmp_db.get_token(checkin_handler.tokens_mod.token_id_of(token))
        assert row["state"] == TokenState.PENDING_VERIFY.value

        second = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert second.kind == KIND_B
        assert second.message == SUBMITTING_TEXT
        # 只有第一次（失败的）尝试到达过后端；第二次在本地就被挡下
        submits = [c for c in backend.calls if c[0] == "checkin_submit"]
        assert len(submits) == 1

    def test_token_info_reports_submitting(self, tmp_db):
        """H5 初始化也看得到 pending_verify：告知处理中，⛔ 不显示可提交。"""
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        backend.raise_on.add("checkin_submit")
        checkin_handler.submit_from_h5(ctx, token, ref, None)
        info = checkin_handler.token_info(ctx, token)
        assert info["ok"] is False
        assert info["reason"] == "submitting"
        assert "请勿重复提交" in info["message"]

    def test_new_token_same_scope_blocked(self, tmp_db):
        """C 挂起期间换新链接再打一次：同范围被 has_pending_scope 挡下。"""
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        backend.raise_on.add("checkin_submit")
        checkin_handler.submit_from_h5(ctx, token, ref, None)
        # 用户回群重新发「打卡」拿新令牌再提交
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        new_token = extract_token(reply)
        new_ref = make_upload(tmp_db, new_token, file_name="b.jpg")
        result = checkin_handler.submit_from_h5(ctx, new_token, new_ref, None)
        assert result.kind == KIND_C  # 结果未知，⛔ 不落第二条

    def test_recovery_verdict_b_reactivates(self, tmp_db):
        """恢复结论 = 明确拒绝 -> 令牌回 active，用户可修正重试。"""
        from app.recovery import process_task
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        backend.raise_on.add("checkin_submit")
        checkin_handler.submit_from_h5(ctx, token, ref, None)
        task = tmp_db.pending_recovery_tasks()[0]
        # 查证结论：服务端实际拒绝了这次提交
        backend.raise_on.discard("checkin_submit")
        backend.results["op_query_by_request"] = {"status": "rejected"}
        summary = process_task(tmp_db, backend, task, AM + timedelta(minutes=1))
        assert summary["outcome"] == "B"
        row = tmp_db.get_token(checkin_handler.tokens_mod.token_id_of(token))
        assert row["state"] == TokenState.ACTIVE.value
        # 令牌活了：可以再次提交（这次后端放行；同 ref 安全重试放行）
        result = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert result.kind == KIND_A

    def test_recovery_verdict_a_consumes(self, tmp_db):
        """恢复结论 = 服务端其实已收下 -> 令牌补消费，额度成立。"""
        from app.recovery import process_task
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        backend.raise_on.add("checkin_submit")
        checkin_handler.submit_from_h5(ctx, token, ref, None)
        task = tmp_db.pending_recovery_tasks()[0]
        backend.raise_on.discard("checkin_submit")
        backend.results["op_query_by_request"] = {"status": "success",
                                                  "record_id": 77}
        summary = process_task(tmp_db, backend, task, AM + timedelta(minutes=1))
        assert summary["outcome"] == "A"
        row = tmp_db.get_token(checkin_handler.tokens_mod.token_id_of(token))
        assert row["state"] == TokenState.CONSUMED.value
        # 再提交：已消费 -> 查范围键已成功 -> A 语义（duplicate）
        result = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert result.kind == KIND_A and result.duplicate is True

    def test_explicit_failure_reactivates(self, tmp_db):
        """明确失败（B）-> 令牌回 active：⛔ 不吞掉用户修正重试的机会。"""
        backend, ctx, token, ref = issue_checkin_token(tmp_db)
        result = checkin_handler.submit_from_h5(ctx, token, "", None)
        assert result.kind == KIND_B  # 缺图，明确失败
        row = tmp_db.get_token(checkin_handler.tokens_mod.token_id_of(token))
        assert row["state"] == TokenState.ACTIVE.value
        retry = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert retry.kind == KIND_A


# ---------- 报数：同机制 ----------

class TestReportPendingVerify:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_db):
        setup(tmp_db)

    def test_second_submit_rejected_during_window(self, tmp_db):
        backend, ctx, token = issue_report_token(tmp_db)
        backend.raise_on.add("report_submit")
        first = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert first.kind == KIND_C
        row = tmp_db.get_token(report_handler.tokens_mod.token_id_of(token))
        assert row["state"] == TokenState.PENDING_VERIFY.value

        second = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert second.kind == KIND_B
        assert second.message == SUBMITTING_TEXT

    def test_new_token_same_scope_blocked(self, tmp_db):
        backend, ctx, token = issue_report_token(tmp_db)
        backend.raise_on.add("report_submit")
        report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        reply = report_handler.handle_command(commands.parse("报数"), ctx)
        new_token = extract_token(reply)
        result = report_handler.submit_from_h5(
            ctx, new_token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_C
