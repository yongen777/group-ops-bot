# -*- coding: utf-8 -*-
"""D45（2026-10-26 用户指令）：实名只问姓名，手机号取平台推送值。

背景
----
平台回调 payload 顶层自带 `phone`（实测 11 位）与 `userName`，所以
「让用户手输手机号」是纯冗余，而且带来两类风险：

1. 输错 —— 会把错误的号写进 MySQL `sys_user`（业务真源）；
2. 输成别人的号 —— 旧实现只在 `phone != ctx.phone` 时记一条安全事件、
   **照样录入**，等于允许把别人的号配到自己名下。

新口径
------
- 第 1 轮话术：「请发送您的真实姓名」，明确告知手机号自动取；
- 第 2 轮：只发姓名即可 -> 手机号 = `ctx.phone`；
- 仍兼容老用户的「姓名 手机号」，但号码必须与平台身份**一致**，
  不一致直接拒（不写库、不改展示名、会话保留）；
- 撞号（平台号在目录里已是别人的姓名）仍拒并落待办。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.handlers import realname as realname_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config
from test_realname_phase import NOW, callback_payload

OTHER_PHONE = "13900000000"


class RosterBackend(FakeBackend):
    """`user_get` 按手机号返回不同 user（模拟目录里已有/没有这个人）。"""

    def __init__(self, by_phone=None):
        super().__init__()
        self.by_phone = by_phone or {}

    def user_get(self, *, phone):
        self._record("user_get", {"phone": phone})
        self._maybe_raise("user_get")
        return self.by_phone.get(phone, {"user": None})


def _bot(tmp_db, backend=None):
    enable_all(tmp_db)
    backend = backend or RosterBackend()
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=make_config(Path(str(tmp_db.db_path)).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), backend, sender


def _say(client, text, phone=PHONE):
    resp = client.post("/api/callback",
                       json=callback_payload(text, phone=phone))
    assert resp.status_code == 200
    return resp.json()


def _last(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


def _start(tmp_db, phone=PHONE):
    tmp_db.put_pending_session(GROUP, phone, "realname", None, NOW)


def _upserts(backend):
    return [c for c in backend.calls if c[0] == "user_upsert"]


class TestNameOnlyFlow:
    def test_ask_text_no_longer_asks_for_phone(self, tmp_db):
        client, _b, sender = _bot(tmp_db)
        _say(client, "实名")
        text = _last(sender)
        assert "请发送您的真实姓名" in text
        assert "手机号会自动取" in text
        # ⛔ 不能再让用户填号（旧话术是「姓名 手机号」）
        assert "姓名 手机号" not in text
        assert "例如：张三" in text

    def test_name_only_writes_platform_phone(self, tmp_db):
        """只发姓名 -> sys_user 用的就是平台推来的号。"""
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "郑佳")
        text = _last(sender)
        assert "实名信息已成功录入" in text
        assert "138****8009" in text          # PHONE 脱敏
        assert PHONE not in text              # ⛔ 不回显完整号码
        calls = _upserts(backend)
        assert len(calls) == 1
        assert calls[0][1]["phone"] == PHONE
        assert calls[0][1]["name"] == "郑佳"
        assert calls[0][1]["group_id"] == GROUP

    def test_name_only_enters_roster_and_display_name(self, tmp_db):
        client, _b, _s = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "郑佳")
        member = tmp_db.get_member_task(GROUP, PHONE)
        assert member is not None
        assert member["nickname"] == "郑佳"
        # learned_user 同步（提醒/看板里的显示名首选源）
        assert tmp_db.user_display_name(PHONE) == "郑佳"
        # 会话已销毁
        assert tmp_db.get_pending_session(GROUP, PHONE, "realname", NOW) is None

    def test_trim_fullwidth_and_extra_spaces(self, tmp_db):
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "　　郑佳　　")
        assert "实名信息已成功录入" in _last(sender)
        assert _upserts(backend)


class TestTypedPhoneStillAcceptedButChecked:
    def test_same_phone_still_works(self, tmp_db):
        """老用户顺手带上自己的号 -> 仍成功（兼容）。"""
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "%s %s" % ("郑佳", PHONE))
        assert "实名信息已成功录入" in _last(sender)
        assert _upserts(backend)

    def test_other_phone_is_rejected(self, tmp_db):
        """填别人的号 -> 拒，且⛔ 不写库。"""
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "%s %s" % ("郑佳", OTHER_PHONE))
        assert _last(sender) == realname_handler.PHONE_MISMATCH_TEXT
        assert not _upserts(backend)
        # 入站学习本来就会写展示名（不是本用例的目标），关键是⛔ 没被改成「郑佳」
        assert tmp_db.user_display_name(PHONE) == "测试用户"
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_other_phone_recorded_as_security_event(self, tmp_db):
        client, _b, _s = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "%s %s" % ("郑佳", OTHER_PHONE))
        rows = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type=?",
            ("realname_phone_mismatch",)).fetchall()
        assert len(rows) == 1
        assert rows[0]["phone"] == PHONE
        # ⛔ 日志/留痕里不出现用户填的那个完整号码（避免二次扩散）
        assert OTHER_PHONE not in (rows[0]["detail"] or "")

    def test_other_phone_keeps_session_for_retry(self, tmp_db):
        """被拒后会话保留：用户可以直接只发姓名重来。"""
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "%s %s" % ("郑佳", OTHER_PHONE))
        assert tmp_db.get_pending_session(GROUP, PHONE, "realname", NOW) is not None
        _say(client, "郑佳")
        assert "实名信息已成功录入" in _last(sender)
        assert _upserts(backend)


class TestFormatStillStrict:
    def test_one_char_name_rejected(self, tmp_db):
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "郑")
        assert _last(sender) == realname_handler.FORMAT_ERROR_TEXT
        assert not _upserts(backend)
        assert tmp_db.get_pending_session(GROUP, PHONE, "realname", NOW) is not None

    def test_three_fields_rejected(self, tmp_db):
        """多段（姓名 手机号 部门）-> 格式错，⛔ 不猜哪个是姓名。"""
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "%s %s 开发部" % ("郑佳", PHONE))
        assert _last(sender) == realname_handler.FORMAT_ERROR_TEXT
        assert not _upserts(backend)

    def test_bad_phone_format_rejected(self, tmp_db):
        client, backend, sender = _bot(tmp_db)
        _start(tmp_db)
        _say(client, "郑佳 12345")
        assert _last(sender) == realname_handler.FORMAT_ERROR_TEXT
        assert not _upserts(backend)


class TestConflictStillGuarded:
    def test_platform_phone_owned_by_other_name(self, tmp_db):
        """平台号在目录里已是别人的姓名 -> 拒 + 落待办（⛔ 不覆盖）。"""
        backend = RosterBackend({
            PHONE: {"user": {"id": 9, "phone": PHONE, "name": "李四"}}})
        client, _b, sender = _bot(tmp_db, backend)
        _start(tmp_db)
        _say(client, "郑佳")
        assert _last(sender) == realname_handler.PHONE_TAKEN_TEXT
        assert not _upserts(backend)
        conflicts = tmp_db.list_phone_conflicts(GROUP)
        assert len(conflicts) == 1
        assert conflicts[0]["phone"] == PHONE

    def test_own_phone_without_name_is_allowed(self, tmp_db):
        """目录里该号存在但姓名为空（历史遗留）-> 允许补录。"""
        backend = RosterBackend({
            PHONE: {"user": {"id": 7, "phone": PHONE, "name": None}}})
        client, _b, sender = _bot(tmp_db, backend)
        _start(tmp_db)
        _say(client, "郑佳")
        assert "实名信息已成功录入" in _last(sender)
