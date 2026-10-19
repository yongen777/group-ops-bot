# -*- coding: utf-8 -*-
"""总结业务第三阶段测试（⭐ 2026-10-16：恒两轮；覆盖确认流 10 分钟 TTL、
截止阻断、后台补录、群内 @ 通知）。"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app.handlers import summary as summary_handler
from app.handlers.base import KIND_A, KIND_B, KIND_C, scope_key
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)


def ctx_at(tmp_db, backend, now, text="", phone=PHONE, sender=None):
    from pathlib import Path
    return make_ctx(tmp_db, backend,
                    make_config(Path(tmp_db.db_path).parent), now=now,
                    text=text, phone=phone, sender=sender)


def _save_calls(backend):
    return [c for c in backend.calls if c[0] == "summary_save"]


def submit(tmp_db, backend, now, content, sender=None, phone=PHONE):
    """⭐ 两轮提交（2026-10-16 用户指令三.2：⛔ 移除一轮直接提交）。

    恒走「总结」-> 提示 -> 下一条纯文本=内容；返回第二轮的话术。
    """
    prompt = summary_handler.handle_command(
        commands.parse("总结"),
        ctx_at(tmp_db, backend, now, sender=sender, phone=phone))
    assert "请发送您的工作总结内容" in prompt, prompt
    return summary_handler.handle_pending_text(
        ctx_at(tmp_db, backend, now + timedelta(minutes=1), text=content,
               sender=sender, phone=phone))


class TestTwoRoundOnly:
    """⭐ 恒两轮：一轮直接提交已移除。"""

    def test_command_always_prompts(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, AM))
        assert "请发送您的工作总结内容" in reply
        # ⛔ 本轮不保存
        assert backend.calls == []

    def test_inline_content_is_not_a_command(self, tmp_db):
        """「总结 内容」在命令层就是无关文本（⛔ 一轮提交路径不存在）。"""
        parsed = commands.parse("总结 今天完成了X")
        assert parsed.result == commands.RESULT_UNKNOWN
        assert parsed.command is None
        assert commands.reply_text(parsed) == commands.HELP_TEXT

    def test_two_round_submits_content(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = submit(tmp_db, backend, AM, "今天完成了X")
        assert reply == "✅ 今日工作总结已成功完成。"
        assert len(_save_calls(backend)) == 1


class TestOverwriteConfirmation:
    def test_first_submit_saves_directly(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = submit(tmp_db, backend, AM, "第一版")
        assert reply == "✅ 今日工作总结已成功完成。"
        assert len(_save_calls(backend)) == 1

    def test_duplicate_prompts_confirmation(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        reply = submit(tmp_db, backend, AM, "第二版")
        assert "已提交过总结" in reply and "确认覆盖" in reply
        # ⛔ 未直接覆盖：仍是 1 次保存
        assert len(_save_calls(backend)) == 1

    def test_confirm_overwrites_with_new_request_id(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        first_id = _save_calls(backend)[-1][1]["request_id"]
        submit(tmp_db, backend, AM, "第二版")
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2), text="确认覆盖"))
        assert "覆盖更新" in reply
        second_id = _save_calls(backend)[-1][1]["request_id"]
        assert first_id != second_id  # §2.5.3：覆盖必须换新键

    def test_cancel_keeps_original(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        submit(tmp_db, backend, AM, "第二版")
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2), text="取消"))
        assert "保留" in reply
        assert len(_save_calls(backend)) == 1  # ⛔ 没有第二次保存
        # 会话已消费
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary", AM) is None

    def test_other_text_reprompts_not_submits(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        submit(tmp_db, backend, AM, "第二版")
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2),
                   text="随便说点别的"))
        assert "确认覆盖" in reply  # ⛔ 不当新总结内容
        assert len(_save_calls(backend)) == 1

    def test_confirmation_ttl_10_minutes(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        submit(tmp_db, backend, AM, "第二版")
        # 10 分钟 TTL：第 11 分钟确认 -> 失效
        late = AM + timedelta(minutes=12)
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, late, text="确认覆盖"))
        assert "超时失效" in reply
        assert len(_save_calls(backend)) == 1  # ⛔ 未覆盖
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary",
                                          late) is None

    def test_two_round_ttl_still_30_minutes(self, tmp_db):
        """两轮内容会话仍是 30 分钟（§2.2），与覆盖确认的 10 分钟互不干扰。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = summary_handler.handle_command(
            commands.parse("总结"), ctx_at(tmp_db, backend, AM))
        assert "请发送您的工作总结内容" in reply
        row = tmp_db.get_pending_session(GROUP, PHONE, "summary", AM)
        assert row["ttl_minutes"] == 30

    def test_two_round_then_overwrite_flow(self, tmp_db):
        """两轮提交触发覆盖确认：内容会话被确认会话顶替（同一 scope_key）。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        summary_handler.handle_command(commands.parse("总结"),
                                       ctx_at(tmp_db, backend, AM))
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=1),
                   text="第二版内容"))
        assert "确认覆盖" in reply
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2),
                   text="确认覆盖"))
        assert "覆盖更新" in reply
        assert len(_save_calls(backend)) == 2


class TestSummaryWholeDayWindow:
    """整天窗口（⭐ 2026-10-19 用户指令：⛔ 不再使用截止时间）。

    旧的「超过截止 -> 已截至」三条测试已按新口径反转。
    """

    def test_past_old_cutoff_still_saves(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, daily_cutoff="10:00")
        # 10:00 >= 旧截止 10:00：现在应照常保存（整天执行）
        reply = submit(tmp_db, backend, AM, "今天完成了X")
        assert "已截至" not in reply
        assert "已成功完成" in reply
        assert len(_save_calls(backend)) == 1

    def test_before_daily_cutoff_ok(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, daily_cutoff="10:30")
        reply = submit(tmp_db, backend, AM, "今天完成了X")  # 10:00 < 10:30
        assert reply == "✅ 今日工作总结已成功完成。"

    def test_confirm_crossing_old_cutoff_still_works(self, tmp_db):
        """确认期间跨过旧截止：照常覆盖（⛔ 不再有截止判定）。

        截止 10:05，10:00 建会话（10:10 到期），10:08 确认。
        """
        backend = FakeBackend()
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, daily_cutoff="10:05")
        submit(tmp_db, backend, AM, "第一版")
        submit(tmp_db, backend, AM, "第二版")
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, datetime(2026, 10, 13, 10, 8,
                                             tzinfo=TZ), text="确认覆盖"))
        assert "已截至" not in reply
        assert "覆盖更新" in reply
        assert len(_save_calls(backend)) == 2

    def test_late_night_still_saves(self, tmp_db):
        """22:30 提交（原 19:00 默认截止之后）-> 照常保存。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        night = datetime(2026, 10, 13, 22, 30, tzinfo=TZ)
        reply = submit(tmp_db, backend, night, "晚上补写的总结")
        assert "已截至" not in reply
        assert "已成功完成" in reply

    def test_weekly_period_sunday_ok(self, tmp_db):
        """周报：周日提交正常（周期键在周一 00:00 翻页，⛔ 不是截止拦截）。"""
        backend = FakeBackend()
        enable_all(tmp_db, period_type="weekly")
        sunday = datetime(2026, 10, 18, 23, 0, tzinfo=TZ)  # 2026-W42 周日
        reply = submit(tmp_db, backend, sunday, "本周总结")
        assert reply == "✅ 本周工作总结已成功完成。"


class TestSummaryAdminBackfill:
    def test_backfill_after_cutoff(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, daily_cutoff="10:00")
        ctx = ctx_at(tmp_db, backend, AM)  # 已过截止
        result = summary_handler.admin_backfill(
            ctx, "2026-10-13", "daily", "补录的总结内容")
        assert result.kind == KIND_A
        assert _save_calls(backend)[-1][1]["entry_source"] == "admin"

    def test_backfill_overwrites_existing(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        summary_handler.admin_backfill(ctx, "2026-10-13", "daily", "第一版")
        result = summary_handler.admin_backfill(
            ctx, "2026-10-13", "daily", "第二版")
        assert result.kind == KIND_A  # 覆盖语义，不拒绝

    def test_backfill_invalid_inputs(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        ctx = ctx_at(tmp_db, backend, AM)
        assert summary_handler.admin_backfill(
            ctx, "2026-10-13", "yearly", "x").kind == KIND_B
        assert summary_handler.admin_backfill(
            ctx, "2026-10-13", "daily", "  ").kind == KIND_B

    def test_backfill_api_endpoint(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        enable_all(tmp_db)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/summary" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "period_type": "daily", "content": "后台补录的总结"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 非法周期类型 -> 400
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/backfill/summary" % GROUP,
                      {"user_id": PHONE, "period_key": "2026-10-13",
                       "period_type": "yearly", "content": "x"})
        assert resp.status_code == 400


class _ExplodingSender(RecordingSender):
    """push 端口抛异常的替身（⛔ 不当已发送，也不得炸断提交链路）。"""

    def push_text(self, group_id, text, mention_phones=None):
        raise RuntimeError("push port exploded")


class TestSummaryMentionNotify:
    """上报 / 覆盖成功的群内 @ 通知（用户指令 2026-10-16 四）。

    口径与打卡成功通知一致（checkin._notify_group_checkin）：
    push_text + mention_phones=[提交人手机号]（真实 @）；
    推送未接通 -> 回落普通回复；已推送 -> 返空串防双消息。
    """

    def test_save_pushes_group_mention(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        sender = RecordingSender()
        sender.push_ok = True
        reply = submit(tmp_db, backend, AM, "今天修复了 3 个 bug",
                       sender=sender)
        # 已 @ 推送 -> 空串（router 约定：不回复），⛔ 不再走普通回推
        assert reply == ""
        assert sender.sent == []
        assert len(sender.pushed) == 1
        pushed = sender.pushed[-1]
        assert pushed["group_id"] == GROUP
        assert pushed["text"] == "✅ 今日工作总结已成功完成。"
        assert pushed["mention_phones"] == [PHONE]

    def test_save_falls_back_to_plain_reply_when_push_unwired(self, tmp_db):
        """推送未接通（默认替身口径）：⛔ 不让用户干等 -> 回落普通回复。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        sender = RecordingSender()   # push_ok=False：未接通
        reply = submit(tmp_db, backend, AM, "今天修复了 3 个 bug",
                       sender=sender)
        assert reply == "✅ 今日工作总结已成功完成。"
        assert sender.pushed == []   # ⛔ 未伪造已推送

    def test_overwrite_pushes_group_mention(self, tmp_db):
        backend = FakeBackend()
        enable_all(tmp_db)
        sender = RecordingSender()
        sender.push_ok = True
        submit(tmp_db, backend, AM, "第一版", sender=sender)
        assert sender.pushed[-1]["mention_phones"] == [PHONE]
        submit(tmp_db, backend, AM, "第二版", sender=sender)
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, AM + timedelta(minutes=2),
                   text="确认覆盖", sender=sender))
        assert reply == ""
        pushed = sender.pushed[-1]
        assert pushed["text"] == "✅ 今日工作总结已成功覆盖更新。"
        assert pushed["mention_phones"] == [PHONE]
        assert len(_save_calls(backend)) == 2

    def test_push_exception_still_delivers_plain_reply(self, tmp_db):
        """push 端口抛异常：提交链路⛔ 不被炸断，回落普通回复。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = submit(tmp_db, backend, AM, "今天修复了 3 个 bug",
                       sender=_ExplodingSender())
        assert reply == "✅ 今日工作总结已成功完成。"
        assert tmp_db.has_successful_scope(
            scope_key(GROUP, PHONE, "summary", "2026-10-13"))

    def test_overwrite_prompt_mentions_bot_name(self, tmp_db):
        """拦截提示带机器人平台名（用户实测名 general-bot，2026-10-16）。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        reply = submit(tmp_db, backend, AM, "第二版")
        assert reply == ("⚠️ 您在今天内已提交过总结。如需覆盖更新，"
                         "请回复：@general-bot 确认覆盖")

    def test_cancel_after_ttl_reports_expired(self, tmp_db):
        """取消遇超时（覆盖确认流边界）：会话已失效 -> 超时话术，⛔ 不静默。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        submit(tmp_db, backend, AM, "第一版")
        submit(tmp_db, backend, AM, "第二版")
        late = AM + timedelta(minutes=12)   # 超过 10 分钟确认 TTL
        reply = summary_handler.handle_pending_text(
            ctx_at(tmp_db, backend, late, text="取消"))
        assert "超时失效" in reply
        assert len(_save_calls(backend)) == 1   # ⛔ 未覆盖