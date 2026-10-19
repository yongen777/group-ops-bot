# -*- coding: utf-8 -*-
"""D18（2026-10-19）群聊侧新口径测试。

覆盖三条用户指令：
1. 未绑定群任意 @机器人 消息 -> 统一引导管理员绑定（⛔ 不再静默）
2. 「实名」两轮登记：会话 10 分钟 / 格式校验 / 取消 / 超时 / 入库 + 脱敏回执
3. 暂停考核 = 群内**不能执行业务操作**（固定回执），而不只是不再提醒
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import commands
from app.handlers import base as base_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
UNBOUND = "G-NOBIND"
NAME = "郑佳"
REAL_PHONE = "13800000001"


def callback_payload(text, group_id=GROUP, phone=PHONE):
    return {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}


def make_client(tmp_db, backend=None, sender=None, clock=None):
    clock = clock or {"now": NOW}
    sender = sender or RecordingSender()
    app = create_app(state=tmp_db, backend=backend or FakeBackend(),
                     sender=sender,
                     config=make_config(Path(tmp_db.db_path).parent),
                     now_fn=lambda: clock["now"])
    return TestClient(app), sender, clock


def pending_realname(tmp_db, group_id=GROUP, phone=PHONE, now=NOW):
    return tmp_db.get_pending_session(group_id, phone, "realname", now)


class TestUnboundGuide:
    """未绑定群：任何非绑定流程的 @机器人 消息都回同一句引导。"""

    def test_any_text_gets_bind_guide(self, tmp_db):
        client, sender, _ = make_client(tmp_db)
        for text in ("打卡", "你好", "总结", "随便说点什么"):
            sender.sent.clear()
            resp = client.post("/api/callback",
                               json=callback_payload(text, UNBOUND))
            assert resp.json()["replied"] is True
            assert len(sender.sent) == 1
            body = sender.sent[0]["text"]
            assert "本群尚未接入机器人" in body
            assert "「@general-bot 绑定」" in body
            # 真实 @ 发送者，而不是正文冒充
            assert sender.sent[0]["mention_phones"] == [PHONE]

    def test_bind_command_from_admin_enters_round_one(self, tmp_db):
        """白名单管理员发「绑定」-> 正常进第一轮（⛔ 不被未绑定提示拦）。"""
        sender = RecordingSender()
        config = make_config(Path(tmp_db.db_path).parent)
        config = dataclasses.replace(config, bind_admin_phones=(PHONE,))
        app = create_app(state=tmp_db, backend=FakeBackend(), sender=sender,
                         config=config, now_fn=lambda: NOW)
        client = TestClient(app)
        resp = client.post("/api/callback",
                           json=callback_payload("绑定", UNBOUND))
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(UNBOUND) is None  # 第一轮不建群

    def test_bind_command_from_non_admin_denied(self, tmp_db):
        """不在白名单 -> 明确无权限（⛔ 也不落成未绑定提示）。"""
        client, sender, _ = make_client(tmp_db)
        client.post("/api/callback", json=callback_payload("绑定", UNBOUND))
        text = sender.sent[-1]["text"]
        assert "无权执行绑定" in text
        assert "本群尚未接入机器人" not in text
        assert tmp_db.get_group_config(UNBOUND) is None


class TestRealName:
    def test_round_one_creates_ten_minute_session(self, tmp_db):
        enable_all(tmp_db)
        client, sender, _ = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=callback_payload("实名"))
        assert resp.json()["replied"] is True
        text = sender.sent[-1]["text"]
        assert "请发送您的「姓名 手机号」" in text
        assert "10 分钟内有效" in text
        row = pending_realname(tmp_db)
        assert row is not None and row["biz"] == "realname"
        assert row["ttl_minutes"] == 10

    def test_round_two_saves_user_and_roster_name(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="旧名字",
                                  role="开发", enabled=True)
        backend = FakeBackend()
        client, sender, _ = make_client(tmp_db, backend=backend)
        client.post("/api/callback", json=callback_payload("实名"))
        resp = client.post("/api/callback",
                           json=callback_payload("%s %s" % (NAME, REAL_PHONE)))
        assert resp.json()["replied"] is True
        assert "实名信息已成功录入" in sender.sent[-1]["text"]
        # 手机号脱敏：前 3 + **** + 后 4，⛔ 不回显完整号码
        assert "138****0001" in sender.sent[-1]["text"]
        assert REAL_PHONE not in sender.sent[-1]["text"]
        # 写 sys_user（带 group_id，供 operation_log 留痕）
        calls = [c for c in backend.calls if c[0] == "user_upsert"]
        assert len(calls) == 1
        assert calls[0][1]["phone"] == REAL_PHONE
        assert calls[0][1]["name"] == NAME
        assert calls[0][1]["group_id"] == GROUP
        # 名单姓名同步（角色/启用状态保持）
        member = tmp_db.get_member_task(GROUP, PHONE)
        assert member["nickname"] == NAME
        assert member["role"] == "开发"
        assert bool(member["enabled"]) is True
        # 会话已销毁
        assert pending_realname(tmp_db) is None

    def test_multiple_spaces_between_name_and_phone(self, tmp_db):
        enable_all(tmp_db)
        backend = FakeBackend()
        client, _, _ = make_client(tmp_db, backend=backend)
        client.post("/api/callback", json=callback_payload("实名"))
        client.post("/api/callback",
                    json=callback_payload("%s      %s" % (NAME, REAL_PHONE)))
        assert [c for c in backend.calls if c[0] == "user_upsert"]

    def test_invalid_phone_keeps_session(self, tmp_db):
        enable_all(tmp_db)
        backend = FakeBackend()
        client, sender, _ = make_client(tmp_db, backend=backend)
        client.post("/api/callback", json=callback_payload("实名"))
        resp = client.post("/api/callback",
                           json=callback_payload("%s 12345" % NAME))
        assert resp.json()["replied"] is True
        assert "输入格式有误" in sender.sent[-1]["text"]
        assert pending_realname(tmp_db) is not None  # ⛔ 不销毁，让用户重发
        assert not [c for c in backend.calls if c[0] == "user_upsert"]

    def test_cancel_clears_session(self, tmp_db):
        enable_all(tmp_db)
        client, sender, _ = make_client(tmp_db)
        client.post("/api/callback", json=callback_payload("实名"))
        resp = client.post("/api/callback", json=callback_payload("取消"))
        assert resp.json()["replied"] is True
        assert "已取消实名登记" in sender.sent[-1]["text"]
        assert pending_realname(tmp_db) is None

    def test_expired_session_replies_timeout(self, tmp_db):
        enable_all(tmp_db)
        backend = FakeBackend()
        client, sender, clock = make_client(tmp_db, backend=backend)
        client.post("/api/callback", json=callback_payload("实名"))
        clock["now"] = NOW + timedelta(minutes=11)
        resp = client.post("/api/callback",
                           json=callback_payload("%s %s" % (NAME, REAL_PHONE)))
        assert resp.json()["replied"] is True
        assert "超时" in sender.sent[-1]["text"]
        assert not [c for c in backend.calls if c[0] == "user_upsert"]

    def test_realname_is_help_listed(self):
        assert commands.CMD_REALNAME == "实名"
        assert "「实名」" in commands.HELP_TEXT


class TestPausedMember:
    def test_paused_member_business_commands_are_blocked(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三",
                                  role="开发", enabled=False)
        backend = FakeBackend()
        client, sender, _ = make_client(tmp_db, backend=backend)
        for text in ("打卡", "总结", "报数"):
            sender.sent.clear()
            resp = client.post("/api/callback", json=callback_payload(text))
            assert resp.json()["replied"] is True
            assert sender.sent[-1]["text"] == base_handler.PAUSED_MEMBER_TEXT
        # ⛔ 一次后端调用都不该发生
        assert backend.calls == []

    def test_paused_member_can_register_realname(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三",
                                  role="开发", enabled=False)
        client, sender, _ = make_client(tmp_db)
        resp = client.post("/api/callback", json=callback_payload("实名"))
        assert resp.json()["replied"] is True
        assert "请发送您的「姓名 手机号」" in sender.sent[-1]["text"]

    def test_resume_restores_business(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三",
                                  role="开发", enabled=False)
        client, sender, _ = make_client(tmp_db)
        client.post("/api/callback", json=callback_payload("打卡"))
        assert sender.sent[-1]["text"] == base_handler.PAUSED_MEMBER_TEXT
        # 恢复考核后同一句命令正常发链接
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三",
                                  role="开发", enabled=True)
        resp = client.post("/api/callback", json=callback_payload("打卡"))
        assert resp.json()["replied"] is True
        assert "/h5/checkin#t=" in sender.sent[-1]["text"]

    def test_unknown_text_still_gets_help(self, tmp_db):
        """暂停考核只拦业务操作，帮助文案照常（否则用户完全没出口）。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三",
                                  role="开发", enabled=False)
        client, sender, _ = make_client(tmp_db)
        client.post("/api/callback", json=callback_payload("你好"))
        assert sender.sent[-1]["text"] == commands.HELP_TEXT
