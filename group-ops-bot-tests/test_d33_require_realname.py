# -*- coding: utf-8 -*-
"""D33（2026-10-22 用户指令）：群内业务命令的「实名校验」闸门。

用户原话：「群运维机器人应该添加一个效验就是后台是否录入该用户，
未录入就是没有实名提醒用户 @机器人 实名这个操作执行。」
二次指令：「为什么还需要加一个开关，默认一直打开就好了，固定回复校验一直生效」

口径（本文件钉死）：
1. **常开**，⛔ 无群级开关（用户明确要求）；
2. 只拦三个业务命令（打卡 / 总结 / 报数）；实名/绑定/导出/帮助/闲聊⛔ 不拦；
3. 未录入 -> 回固定话术引导实名，且**不产生任何业务副作用**；
4. 后端不可用 -> **放行**（fail-open），⛔ 不因后端抖动挡住业务。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.handlers import realname as realname_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config
from test_realname_phase import NOW, callback_payload


def _client(tmp_db, backend=None):
    enable_all(tmp_db)
    backend = backend or FakeBackend()
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=make_config(Path(str(tmp_db.db_path)).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), backend, sender


def _say(client, text):
    resp = client.post("/api/callback", json=callback_payload(text))
    assert resp.status_code == 200
    return resp.json()


def _last_text(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


def _unregistered(backend):
    backend.results["user_get"] = {"user": None}


# ---------- 1. 常开：不配任何开关也生效 ----------

class TestAlwaysOn:
    def test_blocks_checkin_without_any_config(self, tmp_db):
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_blocks_summary(self, tmp_db):
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "总结")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_blocks_report(self, tmp_db):
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "报数")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_no_switch_key_exists(self):
        """⛔ 不允许再出现群级开关（用户明确否掉）。"""
        from app.state import StateStore
        assert not hasattr(StateStore, "EXTRA_SWITCH_KEYS")
        assert not hasattr(realname_handler, "GATE_SWITCH")


# ---------- 2. 已录入 / 未录入判定 ----------

class TestRegisteredState:
    def test_registered_passes_through(self, tmp_db):
        client, backend, sender = _client(tmp_db)
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "郑佳"}}
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT

    def test_user_row_without_name_is_unregistered(self, tmp_db):
        """有行但姓名为空 = 还没实名（与实名登记判定一致）。"""
        client, backend, sender = _client(tmp_db)
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "   "}}
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_backend_error_fails_open(self, tmp_db):
        """后端查不到 -> 放行（fail-open），⛔ 不因 MCP 抖动挡住打卡。"""
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        backend.raise_on = {"user_get"}
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT


# ---------- 3. 豁免：别把人堵死 ----------

class TestExemptions:
    def test_realname_command_not_gated(self, tmp_db):
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "实名")
        assert _last_text(sender) == realname_handler.ASK_TEXT

    def test_unknown_text_not_gated(self, tmp_db):
        """随手一句话⛔ 不弹实名提示（否则人人被骚扰）。"""
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "今天天气不错")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT

    def test_export_not_gated_by_realname(self, tmp_db):
        """导出按白名单权限走，⛔ 不该被实名话术顶掉。"""
        client, backend, sender = _client(tmp_db)
        _unregistered(backend)
        _say(client, "导出")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT


# ---------- 4. 文案与副作用 ----------

class TestTextAndSideEffects:
    def test_text_mentions_realname_instruction(self):
        text = realname_handler.REQUIRE_REALNAME_TEXT
        assert "@机器人 实名" in text

    def test_gated_command_writes_nothing(self, tmp_db):
        """被拦时⛔ 不能建待输入会话（否则解除后会突然执行旧命令）。"""
        client, backend, _s = _client(tmp_db)
        _unregistered(backend)
        _say(client, "总结")
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW) is None
        assert not [c for c in backend.calls if c[0] == "summary_save"]
