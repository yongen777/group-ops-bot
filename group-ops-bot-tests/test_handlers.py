# -*- coding: utf-8 -*-
"""业务处理器测试（§4.1-§4.6 机器人侧逻辑，脱库替身）。

覆盖：总结单轮/两轮/覆盖/周期不符；报数字段池校验与令牌消费；
打卡时段与令牌；A/B/C 归类与 intent/receipt/recovery 落账。
⛔ 全部为本地逻辑验证，不构成真实 MySQL / 平台证据（§7.2）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app import commands, period as period_mod
from app.handlers import checkin as checkin_handler
from app.handlers import report as report_handler
from app.handlers import summary as summary_handler
from app.handlers.base import (KIND_A, KIND_B, KIND_C, scope_key,
                               token_record_from_row)
from app.state import StateStore
from app.tokens import token_id_of
from doubles.fakes import FakeBackend
from helpers import (GROUP, OTHER_PHONE, PHONE, TZ, enable_all,
                     extract_token, make_config, make_ctx, make_upload,
                     setup_fields)

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)   # 上午 -> am
PM = datetime(2026, 10, 13, 15, 0, tzinfo=TZ)   # 下午 -> pm


def ctx_at(tmp_db, backend, now, text="", phone=PHONE, group_id=GROUP):
    from pathlib import Path
    return make_ctx(tmp_db, backend, make_config(Path(tmp_db.db_path).parent),
                    now=now, text=text, phone=phone, group_id=group_id)


# ---------- 总结 ----------

class TestSummary:
    def test_single_round_submit(self, tmp_db):
        """⭐ 两轮提交（2026-10-16：⛔ 移除一轮直接提交）。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM, text="今天修复了 3 个 bug")
        prompt = summary_handler.handle_command(commands.parse("总结"), ctx)
        assert "请发送您的工作总结内容" in prompt
        reply = summary_handler.handle_pending_text(ctx)
        assert reply == "✅ 今日工作总结已成功完成。"
        name, kwargs = backend.calls[-1]
        assert name == "summary_save"
        assert kwargs["content"] == "今天修复了 3 个 bug"
        assert kwargs["period_key"] == "2026-10-13"
        assert kwargs["period_type"] == "daily"
        row = tmp_db.get_intent(kwargs["request_id"])
        assert row["status"] == "confirmed"
        assert tmp_db.has_successful_scope(
            scope_key(GROUP, PHONE, "summary", "2026-10-13"))

    def test_overwrite_requires_confirmation(self, tmp_db):
        """用户指令第三阶段：⛔ 禁止直接覆盖 —— 先提示确认，确认后才覆盖。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        summary_handler.handle_command(commands.parse("总结"), ctx)
        summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM, text="第一版"))
        first_id = backend.calls[-1][1]["request_id"]
        # 第二次提交（⛔ 换一分钟：request_id 按时刻派生，同刻会撞键）：
        summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, AM))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=1), text="第二版"))
        assert "确认覆盖" in reply
        assert len([c for c in backend.calls if c[0] == "summary_save"]) == 1
        # 取消 -> 保留原记录，⛔ 不产生新保存
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM, text="取消"))
        assert "保留" in reply
        assert len([c for c in backend.calls if c[0] == "summary_save"]) == 1
        # 重新提交并确认覆盖 -> §2.5.3：换新 request_id 且内容生效
        summary_handler.handle_command(commands.parse("总结"), ctx)
        summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2), text="第三版"))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=3), text="确认覆盖"))
        assert "覆盖" in reply
        second_id = backend.calls[-1][1]["request_id"]
        assert first_id != second_id
        assert backend.calls[-1][1]["content"] == "第三版"
        assert tmp_db.has_successful_scope(
            scope_key(GROUP, PHONE, "summary", "2026-10-13"))

    def test_two_round_flow(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        reply = summary_handler.handle_command(commands.parse("总结"), ctx)
        assert "请发送您的工作总结内容" in reply
        row = tmp_db.get_pending_session(GROUP, PHONE, "summary", AM)
        assert row is not None
        ctx2 = ctx_at(tmp_db, backend, AM + timedelta(minutes=5),
                      text="明天的发布计划已确认")
        reply2 = summary_handler.handle_pending_text(ctx2)
        assert reply2 == "✅ 今日工作总结已成功完成。"
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary", AM) is None

    def test_period_mismatch_rejected(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)  # daily 群
        ctx = ctx_at(tmp_db, backend, AM)
        reply = summary_handler.handle_command(
            commands.parse("总结 周报"), ctx)
        assert "日报" in reply
        assert not any(c[0] == "summary_save" for c in backend.calls)

    def test_not_enabled(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, biz_switches={"summary": False})
        ctx = ctx_at(tmp_db, backend, AM)
        reply = summary_handler.handle_command(commands.parse("总结  hi"), ctx)
        assert "未开启" in reply

    def test_backend_unreachable_is_c(self, tmp_db):
        backend = FakeBackend()
        backend.raise_on.add("summary_save")
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        summary_handler.handle_command(commands.parse("总结"), ctx)
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM, text=" hi"))
        assert "未知" in reply
        tasks = tmp_db.pending_recovery_tasks()
        assert len(tasks) == 1
        assert tasks[0]["biz"] == "summary"


# ---------- 报数 ----------

def _issue_report_token(tmp_db, now):
    backend = FakeBackend()
    enable_all(tmp_db)
    setup_fields(tmp_db)
    ctx = ctx_at(tmp_db, backend, now)
    reply = report_handler.handle_command(commands.parse("报数"), ctx)
    token = reply.split("#t=")[1].split("\n")[0].strip()
    return backend, ctx, token


class TestReport:
    def test_command_issues_token(self, tmp_db):
        _backend, _ctx, token = _issue_report_token(tmp_db, AM)
        row = tmp_db.get_token(token_id_of(token))
        assert row is not None and row["biz"] == "report"

    def test_token_info_returns_field_pool(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        info = report_handler.token_info(ctx, token)
        assert info["ok"] is True
        assert [f["field_name"] for f in info["fields"]] == ["bug修复数", "代码行数"]
        assert info["fields"][1]["unit"] == "行"

    def test_submit_happy_path_consumes_token(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3},
                         {"field_id": 2, "quantity": 500}])
        assert result.kind == KIND_A
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"
        name, kwargs = backend.calls[-1]
        assert name == "report_submit"
        assert kwargs["role"] == "成员"  # 2026-10-16 去角色化：服务端派生，无应交名单配置时回落 DEFAULT_ROLE
        assert kwargs["items"][0]["field_name"] == "bug修复数"  # 快照
        assert kwargs["items"][1]["unit"] == "行"

    def test_submit_unknown_field_rejected_before_backend(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 999, "quantity": 1}])
        assert result.kind == KIND_B
        assert not any(c[0] == "report_submit" for c in backend.calls)

    def test_submit_empty_items_rejected(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        assert report_handler.submit_from_h5(ctx, token, []).kind == KIND_B
        assert report_handler.submit_from_h5(ctx, token, None).kind == KIND_B

    def test_submit_unit_mismatch_rejected(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 2, "quantity": 1, "unit": "个"}])
        assert result.kind == KIND_B
        assert "单位" in result.message

    def test_submit_bad_quantity_rejected(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": "三"}])
        assert result.kind == KIND_B

    def test_replay_after_consumed_returns_a(self, tmp_db):
        """§2.4 安全重试：令牌已消费 + 范围键已成功 -> 归 A（重复识别）。"""
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        first = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert first.kind == KIND_A
        second = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert second.kind == KIND_A and second.duplicate is True

    def test_backend_unreachable_is_c_with_recovery(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        backend.raise_on.add("report_submit")
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_C
        tasks = tmp_db.pending_recovery_tasks()
        assert len(tasks) == 1 and tasks[0]["biz"] == "report"

    def test_already_done_command_no_new_token(self, tmp_db):
        backend, ctx, token = _issue_report_token(tmp_db, AM)
        report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        reply = report_handler.handle_command(commands.parse("报数"), ctx)
        assert "今日已提交报数" in reply
        assert "不可重复提交" in reply

    def test_bad_token_rejected(self, tmp_db):
        backend, ctx, _token = _issue_report_token(tmp_db, AM)
        result = report_handler.submit_from_h5(
            ctx, "tampered-token-value", [{"field_id": 1, "quantity": 1}])
        assert result.kind == KIND_B


# ---------- 打卡 ----------

class TestCheckin:
    def _issue(self, tmp_db, now):
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, now)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        # ⛔ 不 split：链接话术带「30 分钟有效」尾注（用户指令 2026-10-16 二）
        token = extract_token(reply)
        return backend, ctx, token

    def _upload(self, tmp_db, token):
        """造一条与令牌绑定的上传凭证（image_ref 真源，§4.11.4 D4 定夺）。"""
        return make_upload(tmp_db, token)

    def test_am_session(self, tmp_db):
        _b, _c, token = self._issue(tmp_db, AM)
        row = tmp_db.get_token(token_id_of(token))
        assert row["session"] == "am" and row["period_key"] == "2026-10-13"

    def test_pm_session(self, tmp_db):
        _b, _c, token = self._issue(tmp_db, PM)
        row = tmp_db.get_token(token_id_of(token))
        assert row["session"] == "pm"

    def test_link_text_carries_ttl_hint(self, tmp_db):
        """用户指令 2026-10-16 二：链接话术必须带有效期提醒。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        assert "30 分钟" in reply
        assert "逾期" in reply

    def test_submit_happy_consumes_token(self, tmp_db):
        backend, ctx, token = self._issue(tmp_db, AM)
        result = checkin_handler.submit_from_h5(
            ctx, token, self._upload(tmp_db, token), None)
        assert result.kind == KIND_A
        # 2026-10-16 四：三业务成功话术统一为总结格式风格
        assert result.message == "✅ 上午打卡已成功完成。"
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"

    def test_submit_uses_server_side_image_meta(self, tmp_db):
        """⛔ 不信客户端自报 image_meta：name/size 以凭证行为准。"""
        backend, ctx, token = self._issue(tmp_db, AM)
        ref = make_upload(tmp_db, token, file_name="real.jpg", size=4096)
        checkin_handler.submit_from_h5(
            ctx, token, ref, {"name": "fake.jpg", "size": 1})
        call = [c for c in backend.calls if c[0] == "checkin_submit"][-1]
        assert call[1]["image_meta"] == {"name": "real.jpg", "size": 4096}
        assert call[1]["image_ref"] == ref

    def test_submit_foreign_image_ref_rejected(self, tmp_db):
        """⛔ 不认他人令牌签发的 image_ref（防冒领上传通道）。"""
        backend, ctx, token = self._issue(tmp_db, AM)
        other_ref = make_upload(tmp_db, "not-this-token-value")
        result = checkin_handler.submit_from_h5(ctx, token, other_ref, None)
        assert result.kind == KIND_B
        assert "图片凭证" in result.message

    def test_submit_without_image_rejected(self, tmp_db):
        backend, ctx, token = self._issue(tmp_db, AM)
        result = checkin_handler.submit_from_h5(ctx, token, "", None)
        assert result.kind == KIND_B
        assert "上传" in result.message

    def test_already_done(self, tmp_db):
        backend, ctx, token = self._issue(tmp_db, AM)
        checkin_handler.submit_from_h5(
            ctx, token, self._upload(tmp_db, token), None)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        assert "已完成" in reply

    def test_token_info_done_flag(self, tmp_db):
        backend, ctx, token = self._issue(tmp_db, AM)
        checkin_handler.submit_from_h5(
            ctx, token, self._upload(tmp_db, token), None)
        info = checkin_handler.token_info(ctx, token)
        assert info["done"] is True

    def test_token_info_carries_expires_at(self, tmp_db):
        """H5 倒计时数据源：info 必须带回链接过期时刻（用户指令二）。"""
        backend, ctx, token = self._issue(tmp_db, AM)
        info = checkin_handler.token_info(ctx, token)
        assert info["ok"] is True
        assert info["expires_at"] == tmp_db.get_token(
            token_id_of(token))["expires_at"]
        assert info["ttl_minutes"] == 30

    def test_wrong_group_token_rejected(self, tmp_db):
        """令牌绑定群 G100，换群上下文提交 -> 拒绝（最小授权，§2.4）。"""
        backend, ctx, token = self._issue(tmp_db, AM)
        other = ctx_at(tmp_db, backend, AM, group_id="G200", phone=PHONE)
        result = checkin_handler.submit_from_h5(
            other, token, self._upload(tmp_db, token), None)
        assert result.kind == KIND_B
        assert "不适用" in result.message

    def test_backend_unreachable_is_c(self, tmp_db):
        backend, ctx, token = self._issue(tmp_db, AM)
        backend.raise_on.add("checkin_submit")
        result = checkin_handler.submit_from_h5(
            ctx, token, self._upload(tmp_db, token), None)
        assert result.kind == KIND_C
        assert len(tmp_db.pending_recovery_tasks()) == 1
