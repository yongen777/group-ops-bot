# -*- coding: utf-8 -*-
"""⭐ 绑定两轮对话测试（2026-10-16 用户指令三.4：⛔ 移除一轮直绑；
2026-10-17 用户指令一：第一轮只识别精确「绑定」二字，「绑定: 群名」已移除）。

覆盖第 1 轮（提示输群名）/ 第 2 轮（纯文本建群）及全部边界：
- 第 1 轮 ⛔ 不建群；第 2 轮建群且群名 = 用户输入
- 等待态超时 -> 明确超时提示（2026-10-17 用户指令六，⛔ 不静默失效）
- 「取消」-> 清等待态 + 告知已取消
- 命令类消息 ⛔ 不作群名（明确提示，避免静默吞用户命令）
- 空文本 / 超长群名 -> 保留等待态让用户重发
- 二次鉴权已移除（2026-10-20 用户指令：全体群成员可绑定，⛔ 再看手机号名单）
- 等待期被后台一键绑定 -> 不重复建，告知已绑定
- 回调链路：两轮都回话且都 @ 发送者
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from app import commands
from app.handlers import bind as bind_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, OTHER_PHONE, PHONE, TZ

from test_bind_phase_p0 import (NEW_GROUP, bind_config, callback_payload,
                                ctx_at, switches_of)

ADMIN_PHONE = PHONE
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)


def first_round(tmp_db, backend, **kw):
    return bind_handler.handle_command(commands.parse("绑定"),
                                       ctx_at(tmp_db, backend, **kw))


def bind_handler_bound_guide() -> str:
    """绑定成功话术附带的命令清单（供一致性断言取真源）。"""
    return bind_handler.BOUND_COMMANDS_GUIDE


def second_round(tmp_db, backend, text, **kw):
    kw.setdefault("text", text)
    return bind_handler.handle_pending_text_if_any(ctx_at(tmp_db, backend, **kw))


class TestRoundOne:
    def test_round_one_prompts_and_does_not_create_group(self, tmp_db):
        backend = FakeBackend()
        reply = first_round(tmp_db, backend)
        assert "请输入要绑定的群名称" in reply
        # ⭐ 关键：第 1 轮 ⛔ 不建群
        assert tmp_db.get_group_config(NEW_GROUP) is None
        # 等待态已挂
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                          "bind", NOW) is not None
        # ⛔ 尚未记 group_bound 事件（还没接入）
        rows = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='group_bound'"
        ).fetchall()
        assert len(rows) == 0

    def test_round_one_any_member_gets_prompt(self, tmp_db):
        """⭐ 2026-10-20 用户指令：全体群成员都能触发绑定（不再看手机号名单）。

        用户实测「还没授权任何群成员为管理员，郑佳也能绑定」，确认这是期望语义。
        """
        backend = FakeBackend()
        reply = first_round(tmp_db, backend, phone=OTHER_PHONE)
        assert "请输入要绑定的群名称" in reply
        assert "无权执行绑定" not in reply
        # 任何成员都进两轮（挂等待态）
        assert tmp_db.get_pending_session(NEW_GROUP, OTHER_PHONE,
                                         "bind", NOW) is not None

    def test_round_one_disabled_group_not_prompted(self, tmp_db):
        backend = FakeBackend()
        tmp_db.create_group(NEW_GROUP, "停用群")
        tmp_db.upsert_group_config(NEW_GROUP, status="disabled")
        reply = first_round(tmp_db, backend)
        assert "已被停用" in reply
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is None


class TestRoundTwo:
    def test_round_two_creates_group_with_input_name(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "量子密信群机器人开发沟通群")
        assert "绑定成功" in reply and NEW_GROUP in reply
        row = tmp_db.get_group_config(NEW_GROUP)
        assert row["group_name"] == "量子密信群机器人开发沟通群"
        assert switches_of(tmp_db, NEW_GROUP) == {
            "checkin": False, "summary": False, "report": False,
            "reminder": False, "statistics": False}
        assert json.loads(row["bind_admins"]) == [ADMIN_PHONE]
        # 等待态已清
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is None

    def test_bound_text_appends_commands_guide(self, tmp_db):
        """⭐ 绑定成功话术后面附「可用命令如下」（2026-10-16 用户指令）。

        ⛔ 清单**不含「绑定」**（刚绑定完无需再列），其余四行与帮助文案字面一致。
        """
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "测试群")
        expected_guide = (
            "可用命令如下：\n"
            "「打卡」每日按群配置的时间段打卡\n"
            "「总结」提交当日总结\n"
            "「报数」提交当日工作报数\n"
            "「实名」录入个人姓名与手机号至本群\n"
            "「导出」一键导出全部业务的统计表（仅后台授权的管理员可用）")
        assert reply.endswith(expected_guide)
        # ⛔ 不重复列「绑定」
        assert "「绑定」将本群接入机器人" not in reply
        # 与 commands.HELP_TEXT 的四条业务行保持一致（⛔ 两处不各改各的）
        for line in expected_guide.split("\n")[1:]:
            assert line in bind_handler_bound_guide()

    def test_round_two_consumes_session_once(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        assert second_round(tmp_db, backend, "群A") is not None
        # ⛔ 同一条文本再来一次：没有等待态 -> None（交回主流程）
        assert second_round(tmp_db, backend, "群A") is None

    def test_no_pending_returns_none(self, tmp_db):
        backend = FakeBackend()
        assert bind_handler.handle_pending_text_if_any(
            ctx_at(tmp_db, backend, text="随便一句话")) is None

    def test_pending_expired_returns_timeout_hint(self, tmp_db):
        """⭐ 2026-10-17 用户指令六：等待态过期 -> **明确超时提示**。

        旧行为是过期即删、返 None（未绑定群落入静默分支，用户发群名
        却零反馈）；现在必须告诉用户「已超时，请重新发送绑定」。
        """
        backend = FakeBackend()
        first_round(tmp_db, backend)
        # 31 分钟后（> 30 分钟 TTL）-> 过期
        late = datetime(2026, 10, 13, 10, 31, tzinfo=TZ)
        reply = bind_handler.handle_pending_text_if_any(
            ctx_at(tmp_db, backend, now=late, text="群B"))
        assert "超时" in reply and "重新发送「绑定」" in reply
        # 过期会话已消费（不会二次提示）
        assert bind_handler.handle_pending_text_if_any(
            ctx_at(tmp_db, backend, now=late, text="群B")) is None

    def test_cancel_ends_pending(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "取消")
        assert "已取消" in reply
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is None
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_command_text_is_not_group_name(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "打卡")
        assert "正在等待" in reply and "群名称" in reply
        # ⛔ 等待态保留（用户下一条还能输群名）
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is not None
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_empty_text_keeps_pending(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "   ")
        assert "不能为空" in reply
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is not None

    def test_too_long_name_keeps_pending(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "长" * 65)
        assert "过长" in reply
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is not None

    def test_second_round_no_admin_recheck(self, tmp_db):
        """⭐ 2026-10-20：第二轮不再复核手机号名单（名单已不参与判定）。

        原口径是「等待期名单被改 -> 拒绝 + 留痕」，现改为全员可绑定，
        故第二轮只保留「已被后台绑定 / 群名非法」两类护栏。
        """
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "群C")
        assert "绑定成功" in reply
        assert tmp_db.get_group_config(NEW_GROUP) is not None
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE,
                                         "bind", NOW) is None
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='bind_unauthorized'"
        ).fetchall()
        assert events == []

    def test_already_bound_during_wait(self, tmp_db):
        """等待期被后台一键绑定 -> 不重复建，告知已绑定。"""
        backend = FakeBackend()
        first_round(tmp_db, backend)
        tmp_db.create_group(NEW_GROUP, "后台绑的")
        reply = second_round(tmp_db, backend, "群D")
        assert "已绑定" in reply
        assert tmp_db.get_group_config(NEW_GROUP)["group_name"] == "后台绑的"


class TestBindColonFormIsPlainText:
    """⭐ 2026-10-17 用户指令一：「绑定: 群名」不再是命令。

    命令层已判为无关文本（test_commands.py 锁定）；处理器层的后果：
    两轮等待期发「绑定: 新名字」= 普通文本，按「纯文本即群名」口径
    成为群名（⛔ 不会被当成旧命令静默吞掉）。
    """

    def test_bind_colon_text_during_wait_is_group_name(self, tmp_db):
        backend = FakeBackend()
        first_round(tmp_db, backend)
        reply = second_round(tmp_db, backend, "绑定: 新名字")
        assert "绑定成功" in reply
        assert tmp_db.get_group_config(NEW_GROUP)["group_name"] == "绑定: 新名字"

    def test_bind_colon_in_bound_group_is_help_text(self, tmp_db):
        """已绑定群发「绑定: 新名字」-> 无关文本 -> 帮助文案（命令层口径）。"""
        parsed = commands.parse("绑定: 新名字")
        assert parsed.result == commands.RESULT_UNKNOWN
        assert commands.reply_text(parsed) == commands.HELP_TEXT


class TestTwoRoundViaCallback:
    def _callback_app(self, tmp_db):
        """回调两轮用 app（D29：配置里已无任何权限名单）。"""
        backend = FakeBackend()
        sender = RecordingSender()
        app = create_app(
            state=tmp_db, backend=backend, sender=sender,
            config=bind_config(Path(tmp_db.db_path).parent),
            now_fn=lambda: NOW)
        return TestClient(app), sender

    def test_callback_two_rounds_with_mention(self, tmp_db):
        """回调两轮：都回话，且 ⭐ 都 @ 发送者（用户指令一）。"""
        client, sender = self._callback_app(tmp_db)
        resp = client.post("/api/callback",
                           json=callback_payload("绑定", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        assert sender.sent[-1]["mention_phones"] == [ADMIN_PHONE]

        resp = client.post("/api/callback",
                           json=callback_payload("我的测试群", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "绑定成功" in sender.sent[-1]["text"]
        assert sender.sent[-1]["mention_phones"] == [ADMIN_PHONE]
        assert tmp_db.get_group_config(NEW_GROUP)["group_name"] == "我的测试群"

    def test_callback_round_two_ignores_other_user(self, tmp_db):
        """⛔ 别人在等待期发文本不当群名（等待态按手机号绑定）。

        ⭐ 2026-10-19：该群仍未绑定 -> 回统一引导话术（⛔ 不再静默）。
        """
        client, sender = self._callback_app(tmp_db)
        client.post("/api/callback",
                    json=callback_payload("绑定", NEW_GROUP,
                                          phone=ADMIN_PHONE))
        resp = client.post("/api/callback",
                           json=callback_payload("冒名群", NEW_GROUP,
                                                 phone=OTHER_PHONE))
        assert resp.json()["replied"] is True
        assert "尚未接入机器人" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_callback_round_two_outside_slot_command_not_swallowed(self, tmp_db):
        """⛔ 等待期插入命令 -> 明确提示，不静默吞（且不建群）。"""
        client, sender = self._callback_app(tmp_db)
        client.post("/api/callback",
                    json=callback_payload("绑定", NEW_GROUP,
                                          phone=ADMIN_PHONE))
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "正在等待" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(NEW_GROUP) is None