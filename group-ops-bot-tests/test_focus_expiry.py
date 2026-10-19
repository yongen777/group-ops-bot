# -*- coding: utf-8 -*-
"""⭐ 两轮对话焦点过期测试（2026-10-17 用户指令六）。

设计口径（回答「焦点是一直保持还是有过期」）：
- 焦点 = pending_session，**有 TTL**（默认 30 分钟 =
  ``config.pending_ttl_minutes``；本地演示可经
  GOPS_PENDING_SESSION_TTL_MINUTES 调小），⛔ 不是永久保持
- 过期 = **懒过期**（该用户下一条消息到达时判定并清除），
  ⛔ 不起后台线程到点推送（避免「您的会话已过期」骚扰全群）
- 过期后下一条**非命令**文本 -> 明确超时话术（绑定 / 总结各一套），
  ⛔ 不静默失效（旧行为：绑定落入未绑定群静默分支零反馈、
  总结落入帮助文案）
- 命令类消息永不被超时话术吞掉（照常走主流程：已绑定群照常执行业务）
- 重开 = 重新发送命令即挂新焦点；等待期可发「取消」主动放弃
- 覆盖确认（10 分钟 TTL）过期 -> OVERWRITE_EXPIRED_TEXT，
  与「输入超时」是两套话术（同一 scope，按 payload.mode 区分）
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import commands
from app.config import BotConfig
from app.handlers import bind as bind_handler
from app.handlers import summary as summary_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_ctx

from test_bind_phase_p0 import ADMIN_PHONE, NEW_GROUP, callback_payload

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
AM = NOW


def _config(data_dir, ttl=30, bind_admins=(ADMIN_PHONE,)) -> BotConfig:
    """带可调焦点 TTL 的配置（⛔ 不改 helpers.make_config 的签名）。"""
    return BotConfig(
        callback_port=18002, admin_port=18003,
        external_base_url="http://127.0.0.1:18002",
        tz_name="Asia/Shanghai", data_dir=str(data_dir),
        daily_cutoff=(19, 0), mcp=None,
        pending_ttl_minutes=ttl,
        bind_admin_phones=bind_admins)


def _ctx(tmp_db, backend, now, text="", phone=ADMIN_PHONE,
         group_id=NEW_GROUP, ttl=30, sender=None):
    return make_ctx(tmp_db, backend,
                    _config(Path(tmp_db.db_path).parent, ttl),
                    now=now, group_id=group_id, phone=phone, text=text,
                    sender=sender)


def _summary_ctx(tmp_db, backend, now, text=""):
    return make_ctx(tmp_db, backend,
                    _config(Path(tmp_db.db_path).parent, 30),
                    now=now, group_id=GROUP, phone=PHONE, text=text)


# ---------- 1. state 层：过期行的「取到即删」语义 ----------

class TestStateExpiredSession:
    def test_expired_row_returned_and_deleted(self, tmp_db):
        tmp_db.put_pending_session(GROUP, PHONE, "bind", None, NOW,
                                   ttl_minutes=30)
        late = NOW + timedelta(minutes=31)
        row = tmp_db.get_expired_pending_session(GROUP, PHONE, "bind", late)
        assert row is not None and row["biz"] == "bind"
        # 命中即删：第二次取不到，表里也没了
        assert tmp_db.get_expired_pending_session(
            GROUP, PHONE, "bind", late) is None
        assert tmp_db._conn.execute(
            "SELECT * FROM pending_session").fetchall() == []

    def test_not_expired_returns_none(self, tmp_db):
        """未过期 -> None（调用方该用 get_pending_session），且⛔ 不删。"""
        tmp_db.put_pending_session(GROUP, PHONE, "bind", None, NOW,
                                   ttl_minutes=30)
        assert tmp_db.get_expired_pending_session(
            GROUP, PHONE, "bind", NOW) is None
        assert tmp_db.get_pending_session(GROUP, PHONE, "bind",
                                          NOW) is not None

    def test_no_session_returns_none(self, tmp_db):
        assert tmp_db.get_expired_pending_session(
            GROUP, PHONE, "bind", NOW) is None


# ---------- 2. 绑定焦点过期 ----------

class TestBindFocusExpiry:
    def test_expired_wait_gets_timeout_hint(self, tmp_db):
        backend = FakeBackend()
        first = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend, NOW))
        assert "请输入要绑定的群名称" in first
        # 31 分钟后发群名 -> 明确超时提示（⛔ 不静默、不建群）
        late = NOW + timedelta(minutes=31)
        reply = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, late, text="我的群名"))
        assert "超时" in reply and "重新发送「绑定」" in reply
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_expired_wait_command_not_swallowed(self, tmp_db):
        """过期期插入命令 -> 超时话术⛔ 不吞命令（返 None 交回主流程）。"""
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend, NOW))
        late = NOW + timedelta(minutes=31)
        assert bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, late, text="打卡")) is None

    def test_expired_after_admin_bound_no_nag(self, tmp_db):
        """等待期被后台绑定 + 焦点过期 -> ⛔ 不再催「重新发送绑定」。"""
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend, NOW))
        tmp_db.create_group(NEW_GROUP, "后台绑的")
        late = NOW + timedelta(minutes=31)
        assert bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, late, text="我的群名")) is None

    def test_retrigger_after_expiry(self, tmp_db):
        """过期后重新发「绑定」= 挂新焦点，第二轮照常建群。"""
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend, NOW))
        late = NOW + timedelta(minutes=31)
        bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, late, text="我的群名"))
        again = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend, late))
        assert "请输入要绑定的群名称" in again
        reply = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, late + timedelta(minutes=1),
                 text="重试群名"))
        assert "绑定成功" in reply
        assert tmp_db.get_group_config(NEW_GROUP)["group_name"] == "重试群名"

    def test_custom_ttl_respected(self, tmp_db):
        """TTL 来自配置（本地演示可调小）：2 分钟 TTL，3 分钟即过期。"""
        backend = FakeBackend()
        first = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend, NOW, ttl=2))
        assert "2 分钟内有效" in first  # 话术里的有效期随配置走
        # 1 分钟：仍有效（发命令 -> 提示正在等待，⛔ 不建群不消费焦点）
        busy = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, NOW + timedelta(minutes=1), text="打卡"))
        assert "正在等待" in busy
        # 3 分钟：已过期 -> 超时话术
        reply = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, NOW + timedelta(minutes=3),
                 text="我的群名"))
        assert "超时" in reply

    def test_cancel_still_works_within_ttl(self, tmp_db):
        """TTL 内「取消」主动放弃（不占满 30 分钟）。"""
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend, NOW))
        reply = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, NOW + timedelta(minutes=5), text="取消"))
        assert "已取消" in reply
        assert tmp_db.get_group_config(NEW_GROUP) is None


# ---------- 3. 总结焦点过期 ----------

class TestSummaryFocusExpiry:
    def _first_round(self, tmp_db, backend, now=NOW, ttl=30):
        enable_all(tmp_db)
        return summary_handler.handle_command(
            commands.parse("总结"),
            make_ctx(tmp_db, backend,
                     _config(Path(tmp_db.db_path).parent, ttl),
                     now=now, group_id=GROUP, phone=PHONE))

    def test_round_two_expired_gets_prompt(self, tmp_db):
        """输入焦点过期 -> 明确提示重发「总结」（⛔ 不落入帮助文案）。"""
        backend = FakeBackend()
        self._first_round(tmp_db, backend)
        late = NOW + timedelta(minutes=31)
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, late, text="迟到的总结内容"))
        assert "已过期" in reply and "重新发送「总结」" in reply
        # ⛔ 过期内容不被保存
        assert [c for c in backend.calls if c[0] == "summary_save"] == []

    def test_overwrite_expired_gets_overwrite_text(self, tmp_db):
        """覆盖确认过期（10 分钟）-> 覆盖专用超时话术。"""
        backend = FakeBackend()
        self._first_round(tmp_db, backend)
        summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, NOW + timedelta(minutes=1),
                         text="第一版"))
        # 第二次提交：再发「总结」挂新焦点 -> 内容触发覆盖确认会话
        self._first_round(tmp_db, backend, now=NOW + timedelta(minutes=2))
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, NOW + timedelta(minutes=3),
                         text="第二版"))
        assert "确认覆盖" in reply
        # 覆盖确认 TTL 10 分钟（+3 建，+13 到期）：第 14 分钟发普通文本
        # -> 覆盖确认超时话术（⛔ 不是输入超时话术），且未覆盖
        late = NOW + timedelta(minutes=14)
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, late, text="随便说说"))
        assert "超时失效" in reply
        saves = [c for c in backend.calls if c[0] == "summary_save"]
        assert len(saves) == 1

    def test_cancel_during_input_wait(self, tmp_db):
        """⭐ 等待输入期间可发「取消」主动放弃（2026-10-17 用户指令六）。"""
        backend = FakeBackend()
        self._first_round(tmp_db, backend)
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, NOW + timedelta(minutes=5),
                         text="取消"))
        assert "已取消" in reply and "总结" in reply
        assert [c for c in backend.calls if c[0] == "summary_save"] == []
        # 会话已消费：再发内容 -> 无待处理 -> None（主流程走帮助文案）
        assert summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, NOW + timedelta(minutes=6),
                         text="其实我想提交")) is None

    def test_no_session_confirm_overwrite_legacy(self, tmp_db):
        """从未有会话却发「确认覆盖」-> 保留旧口径的超时话术。"""
        backend = FakeBackend()
        enable_all(tmp_db)
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, NOW, text="确认覆盖"))
        assert "超时失效" in reply

    def test_custom_ttl_respected(self, tmp_db):
        backend = FakeBackend()
        first = self._first_round(tmp_db, backend, ttl=2)
        assert "2 分钟内有效" in first and "取消" in first
        late = NOW + timedelta(minutes=3)
        reply = summary_handler.handle_pending_text(
            _summary_ctx(tmp_db, backend, late, text="迟到的内容"))
        assert "已过期" in reply


# ---------- 4. 回调链路端到端（未绑定群过期 -> 回话 + @） ----------

class TestCallbackBindExpiryEndToEnd:
    def test_expired_bind_replies_with_mention(self, tmp_db):
        """未绑定群：绑定 -> 等超时 -> 发群名 -> 明确超时话术且 @ 发送者。"""
        sender = RecordingSender()
        clock = {"now": NOW}
        app = create_app(
            state=tmp_db, backend=FakeBackend(), sender=sender,
            config=_config(Path(tmp_db.db_path).parent, 30),
            now_fn=lambda: clock["now"])
        client = TestClient(app)

        resp = client.post("/api/callback",
                           json=callback_payload("绑定", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]

        clock["now"] = NOW + timedelta(minutes=31)
        resp = client.post("/api/callback",
                           json=callback_payload("我的群名", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "超时" in sender.sent[-1]["text"]
        assert sender.sent[-1]["mention_phones"] == [ADMIN_PHONE]
        # ⛔ 超时走完不建群
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_expired_bind_command_falls_through_silent(self, tmp_db):
        """过期期发业务命令 -> 超时话术不吞命令；未绑定群照旧静默。"""
        sender = RecordingSender()
        clock = {"now": NOW}
        app = create_app(
            state=tmp_db, backend=FakeBackend(), sender=sender,
            config=_config(Path(tmp_db.db_path).parent, 30),
            now_fn=lambda: clock["now"])
        client = TestClient(app)

        client.post("/api/callback",
                    json=callback_payload("绑定", NEW_GROUP,
                                          phone=ADMIN_PHONE))
        clock["now"] = NOW + timedelta(minutes=31)
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is False
        assert tmp_db.get_group_config(NEW_GROUP) is None
