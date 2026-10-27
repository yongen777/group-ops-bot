# -*- coding: utf-8 -*-
"""D47（2026-10-26 用户反馈两件事）

一、停用了群聊为什么还能执行指令
--------------------------------
后台有两处「停用」：
- `group_config.status = 'disabled'`（群配置页）—— 一直有效
- `qmx_robot_instance.status = 'DISABLED'`（群实例绑定页）—— 旧实现**只停
  推送出站**，⛔ 不影响业务授权

用户点的是后者，于是"停用了"但机器人照常回总结/打卡。线上实测：
三个群的 `group_config.status` 全是 active，其中两个实例是 DISABLED。
现在 `state.group_active` 一处真源：两者任一停用即不可用，群内任何指令
（含帮助文案）都回 `bind.DISABLED_GROUP_TEXT`。

二、总结文本录入错
------------------
群里有人发 `asdfghjkl`，机器人回「✅ 今日工作总结已成功完成」，后台显示
「正常完成」—— 统计里多出一条假记录。旧实现只判 `if not text`。
现在按「是否像总结」拦（不按长度，避免误伤「第一版」这种短总结）：
含中文放行；无中文则要求 ≥3 个词或 ≥20 字符。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import commands
from app.handlers import bind as bind_handler
from app.handlers import summary as summary_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=TZ)


def _payload(text, group_id=GROUP, phone=PHONE):
    return {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}


def _bot(tmp_db, backend=None):
    enable_all(tmp_db)
    backend = backend or FakeBackend()
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=make_config(Path(str(tmp_db.db_path)).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), backend, sender


def _say(client, text, group_id=GROUP, phone=PHONE):
    resp = client.post("/api/callback", json=_payload(text, group_id, phone))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _last(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


def _saves(backend):
    return [c for c in backend.calls if c[0] == "summary_save"]


def _disable_instance(tmp_db, group_id=GROUP):
    tmp_db.ensure_robot_instance(group_id)
    tmp_db.set_robot_instance_status(group_id, "DISABLED")


class TestInstanceDisabledBlocksBusiness:
    def test_business_command_gets_disabled_text(self, tmp_db):
        client, _b, sender = _bot(tmp_db)
        _disable_instance(tmp_db)
        _say(client, "总结")
        assert _last(sender) == bind_handler.DISABLED_GROUP_TEXT

    def test_help_text_also_blocked(self, tmp_db):
        """未知指令也⛔ 不能漏出帮助文案（否则用户以为群还能用）。"""
        client, _b, sender = _bot(tmp_db)
        _disable_instance(tmp_db)
        _say(client, "随便说点什么")
        assert _last(sender) == bind_handler.DISABLED_GROUP_TEXT
        assert commands.HELP_TEXT not in _last(sender)

    def test_no_business_side_effect(self, tmp_db):
        """停用群里发「总结」⛔ 不能挂会话（否则重新启用后下条文本被吞）。"""
        client, _b, sender = _bot(tmp_db)
        _disable_instance(tmp_db)
        _say(client, "总结")
        assert tmp_db.peek_pending_session(GROUP, PHONE, "summary") is None

    def test_all_business_commands_blocked(self, tmp_db):
        client, _b, sender = _bot(tmp_db)
        _disable_instance(tmp_db)
        for cmd in ("打卡", "总结", "报数", "导出"):
            _say(client, cmd)
            assert _last(sender) == bind_handler.DISABLED_GROUP_TEXT, cmd

    def test_group_config_disabled_still_works(self, tmp_db):
        """群配置页停用（老路径）必须仍然生效 —— 回归。"""
        client, _b, sender = _bot(tmp_db)
        tmp_db.upsert_group_config(GROUP, status="disabled")
        _say(client, "打卡")
        assert _last(sender) == bind_handler.DISABLED_GROUP_TEXT

    def test_active_instance_passes(self, tmp_db):
        client, _b, sender = _bot(tmp_db)
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db.set_robot_instance_status(GROUP, "ACTIVE")
        _say(client, "随便说点什么")
        assert _last(sender) == commands.HELP_TEXT

    def test_no_instance_row_does_not_block(self, tmp_db):
        """没有实例行的历史群⛔ 不能因此被判停用（fail-open）。"""
        client, _b, sender = _bot(tmp_db)
        _say(client, "随便说点什么")
        assert _last(sender) == commands.HELP_TEXT

    def test_group_active_reflects_both(self, tmp_db):
        """两处「停用」任一生效即视为不可用（一处真源）。"""
        enable_all(tmp_db)
        assert tmp_db.group_active(GROUP) is True
        # 实例停用（群实例绑定页）
        _disable_instance(tmp_db)
        assert tmp_db.group_active(GROUP) is False
        # 群配置停用（群配置页）—— 另一条路径同样生效
        # ⚠️ 这里不测「恢复 ACTIVE」：set_robot_instance_status(ACTIVE) 要求
        # 已绑 webhook（后台 enable 的守卫），可逆性由 test_robot_instance
        # 里的改写用例覆盖。
        tmp_db.upsert_group_config(GROUP, status="disabled")
        assert tmp_db.group_active(GROUP) is False


class TestSummaryContentGuard:
    def test_gibberish_blocked(self, tmp_db):
        """线上实际输入 asdfghjkl -> ⛔ 不落库、⛔ 不回「已完成」。"""
        client, backend, sender = _bot(tmp_db)
        _say(client, "总结")
        _say(client, "asdfghjkl")
        assert _last(sender) == summary_handler.MEANINGLESS_CONTENT_TEXT
        assert not _saves(backend)
        # 会话保留：用户可以直接重发
        assert tmp_db.peek_pending_session(GROUP, PHONE, "summary") is not None

    def test_blocked_then_valid_content_succeeds(self, tmp_db):
        client, backend, sender = _bot(tmp_db)
        _say(client, "总结")
        _say(client, "qwer")
        _say(client, "今天完成了登录模块联调")
        assert "成功完成" in _last(sender)
        assert len(_saves(backend)) == 1

    def test_short_chinese_content_allowed(self, tmp_db):
        """⚠️ 判据不按长度：短中文总结必须放行（否则会误伤真实汇报）。"""
        client, backend, sender = _bot(tmp_db)
        _say(client, "总结")
        _say(client, "第一版")
        assert "成功完成" in _last(sender)
        assert len(_saves(backend)) == 1

    def test_english_sentence_allowed(self, tmp_db):
        client, backend, sender = _bot(tmp_db)
        _say(client, "总结")
        _say(client, "Fixed login bug and added tests")
        assert "成功完成" in _last(sender)
        assert len(_saves(backend)) == 1

    def test_cancel_still_works(self, tmp_db):
        client, _b, sender = _bot(tmp_db)
        _say(client, "总结")
        _say(client, "取消")
        assert "已取消" in _last(sender)
        assert tmp_db.peek_pending_session(GROUP, PHONE, "summary") is None

    def test_predicate(self):
        """判据本身的边界（改动这条要同时改线上口径）。"""
        f = summary_handler.is_meaningless_content
        for bad in ("asdfghjkl", "qwer", "12345", "aaaaaaa", "!!!", "OK"):
            assert f(bad) is True, bad
        for good in ("第一版", "今天完成了X", "写代码",
                     "Fixed login bug", "deployed the service to staging today"):
            assert f(good) is False, good
        assert f("") is False          # 空内容交给 EMPTY_CONTENT_TEXT
