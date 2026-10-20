# -*- coding: utf-8 -*-
"""P0 群绑定测试（用户指令验收项 1：进群 -> 管理员「绑定」-> 真实 group_id 建配置）。

覆盖：
- 群内「绑定」命令：全体成员可触发（D29：已无任何 env 权限名单）/ 幂等 /
  停用群不借绑定暗中启用 / 空 group_id / 格式错误
- fail-closed：绑定后五业务开关**全关**（⛔ 不替管理员决定开哪些业务）
- 回调发现记账（group_seen）+ 后台「发现但未绑定」列表 + 一键绑定（双路径）
- 隔离：绑定后本群业务按 group_id 走；未绑定群回调静默

⛔ 这是本地自测证据，不构成真实平台回调验证（§7.2 / D10 / D11）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app import commands
from app.config import BotConfig
from app.handlers import bind as bind_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, OTHER_PHONE, PHONE, TZ, make_ctx

from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
NEW_GROUP = "G700"          # 本轮要绑定的群
OTHER_GROUP = "G800"        # 始终未绑定的群（隔离对照）
ADMIN_PHONE = PHONE         # 绑定管理员 = 测试用户手机号


def bind_config(data_dir) -> BotConfig:
    """绑定场景配置（⛔ 不含任何权限名单：D29 起 BotConfig 已无此字段）。"""
    return BotConfig(
        callback_port=18002, admin_port=18003,
        external_base_url="http://127.0.0.1:18002",
        tz_name="Asia/Shanghai", data_dir=str(data_dir),
        daily_cutoff=(19, 0), mcp=None)


def ctx_at(tmp_db, backend, now=NOW, group_id=NEW_GROUP, phone=ADMIN_PHONE,
           text="绑定"):
    return make_ctx(tmp_db, backend,
                    bind_config(Path(tmp_db.db_path).parent),
                    now=now, group_id=group_id, phone=phone, text=text)


def bind_via_chat(tmp_db, backend, group_id=NEW_GROUP, phone=ADMIN_PHONE,
                  group_name="新群"):
    """⭐ 两轮绑定（2026-10-16 用户指令三.4）：第 1 轮提示 -> 第 2 轮输群名。

    返回 (第一轮话术, 第二轮话术)。
    """
    first = bind_handler.handle_command(
        commands.parse("绑定"),
        ctx_at(tmp_db, backend, group_id=group_id, phone=phone))
    second = bind_handler.handle_pending_text_if_any(
        ctx_at(tmp_db, backend, group_id=group_id, phone=phone,
               text=group_name))
    return first, second


def security_events(tmp_db, event_type):
    return tmp_db._conn.execute(
        "SELECT * FROM security_event WHERE event_type=?",
        (event_type,)).fetchall()


def switches_of(tmp_db, group_id):
    row = tmp_db.get_group_config(group_id)
    return tmp_db.normalized_switches(row)


def make_callback_app(tmp_db):
    backend = FakeBackend()
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=bind_config(Path(tmp_db.db_path).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), sender


def callback_payload(text, group_id, phone=PHONE):
    return {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}


def admin_ready(tmp_db, monkeypatch):
    """引导 + 登录 + 过强制改密门（⛔ 不过门其他端点一律 403）。"""
    admin = admin_client(tmp_db, Path(tmp_db.db_path).parent)
    bootstrap(tmp_db, monkeypatch)
    assert login(admin).status_code == 200
    resp = mutate(admin, "POST", "/api/admin/change-password",
                  {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert resp.status_code == 200
    assert login(admin, password=NEW_PASS).status_code == 200
    return admin


# ---------- 群内「绑定」命令 ----------

class TestBindCommand:
    def test_bind_creates_group_config(self, tmp_db):
        """授权管理员发「绑定」-> 两轮：用回调真实 group_id 建配置（active + 全关）。"""
        backend = FakeBackend()
        first, second = bind_via_chat(tmp_db, backend)
        # ⭐ 第 1 轮只提示，第 2 轮才建群
        assert "请输入要绑定的群名称" in first
        assert "绑定成功" in second and NEW_GROUP in second
        row = tmp_db.get_group_config(NEW_GROUP)
        assert row is not None and row["status"] == "active"
        # ⭐ 群名 = 用户第二轮输入的文本
        assert row["group_name"] == "新群"
        # fail-closed：五业务全关（缺键即 False 的显式化）
        assert switches_of(tmp_db, NEW_GROUP) == {
            "checkin": False, "summary": False, "report": False,
            "reminder": False, "statistics": False}
        # 绑定管理员留痕（⛔ 不是授权本身，授权仍看 env 名单）
        assert json.loads(row["bind_admins"]) == [ADMIN_PHONE]
        events = security_events(tmp_db, "group_bound")
        assert len(events) == 1 and events[0]["group_id"] == NEW_GROUP

    def test_bind_any_member_can_start(self, tmp_db):
        """⭐ 2026-10-20 用户指令：**全体群成员都能触发绑定**（不校验手机号名单）。

        用户实测「还没授权任何群成员为管理员，郑佳也能绑定」，确认这就是期望语义。
        """
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"),
            ctx_at(tmp_db, backend, phone=OTHER_PHONE))
        assert "请输入要绑定的群名称" in reply
        assert tmp_db.get_group_config(NEW_GROUP) is None   # 第一轮不建群
        # ⛔ 不再产生越权留痕（该分支已随名单判定一起移除）
        assert security_events(tmp_db, "bind_unauthorized") == []

    def test_bind_any_member_completes_two_rounds(self, tmp_db):
        """非名单成员走完两轮 -> 正常建群，且 bind_admins 记录**实际绑定人**。"""
        backend = FakeBackend()
        _, second = bind_via_chat(tmp_db, backend, phone=OTHER_PHONE,
                                  group_name="非管理员绑的群")
        assert "绑定成功" in second
        row = tmp_db.get_group_config(NEW_GROUP)
        assert row is not None and row["group_name"] == "非管理员绑的群"
        assert json.loads(row["bind_admins"]) == [OTHER_PHONE]

    def test_bind_needs_no_env_whitelist(self, tmp_db):
        """D29：配置里已无任何绑定名单，绑定照常进两轮（权限不从 env 来）。"""
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"), ctx_at(tmp_db, backend))
        assert "请输入要绑定的群名称" in reply

    def test_rebind_is_idempotent(self, tmp_db):
        """重复绑定：明确告知已绑定，⛔ 不重置任何配置，⛔ 不进两轮。"""
        backend = FakeBackend()
        first, second = bind_via_chat(tmp_db, backend)
        assert "请输入要绑定的群名称" in first
        assert "绑定成功" in second
        # 管理员在后台开了两个业务，再「绑定」不得被改回全关
        tmp_db.upsert_group_config(
            NEW_GROUP, biz_switches={"checkin": True, "summary": True,
                                     "report": False, "reminder": False,
                                     "statistics": False})
        reply = bind_handler.handle_command(
            commands.parse("绑定"), ctx_at(tmp_db, backend))
        assert "已绑定" in reply and "无需重复绑定" in reply
        # ⛔ 已绑定不进两轮：没有等待会话
        assert tmp_db.get_pending_session(NEW_GROUP, ADMIN_PHONE, "bind") is None
        switches = switches_of(tmp_db, NEW_GROUP)
        assert switches["checkin"] is True and switches["summary"] is True

    def test_disabled_group_not_reenabled_by_bind(self, tmp_db):
        """已停用群：指引去后台启用，⛔ 不借绑定命令暗中重新启用。"""
        backend = FakeBackend()
        tmp_db.create_group(NEW_GROUP, "停用群")
        tmp_db.upsert_group_config(NEW_GROUP, status="disabled")
        reply = bind_handler.handle_command(
            commands.parse("绑定"), ctx_at(tmp_db, backend))
        assert "已被停用" in reply and "后台" in reply
        assert tmp_db.group_active(NEW_GROUP) is False

    def test_empty_group_id(self, tmp_db):
        """回调里没有群标识 -> 明确失败，⛔ 不猜。"""
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"), ctx_at(tmp_db, backend, group_id=""))
        assert "没有群标识" in reply
        assert tmp_db.get_group_config("") is None

    def test_bad_format(self, tmp_db):
        """"绑定abc" -> 精确匹配口径下是**无关文本**（⛔ 不进绑定处理器）。"""
        backend = FakeBackend()
        parsed = commands.parse("绑定abc")
        assert parsed.result == commands.RESULT_UNKNOWN
        assert parsed.command is None
        # 帮助文案兜底
        assert commands.reply_text(parsed) == commands.HELP_TEXT

    def test_at_bot_mention_parsed(self, tmp_db):
        """"@机器人 绑定" 形态也能解析成绑定命令。"""
        parsed = commands.parse("@机器人 绑定")
        assert parsed.command == commands.CMD_BIND


# ---------- 回调发现 + 后台一键绑定（双路径） ----------

class TestDiscoveredAndBackendBind:
    def test_unbound_group_recorded_then_bindable(self, tmp_db, monkeypatch):
        """未绑定群回调 -> group_seen 记账 -> 后台列表可见 -> 一键绑定。"""
        client, _sender = make_callback_app(tmp_db)
        # 未绑定群发业务命令：回统一引导（⭐ 2026-10-19 起不再静默），且已记账
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", NEW_GROUP))
        assert resp.json()["replied"] is True
        seen = tmp_db._conn.execute(
            "SELECT * FROM group_seen WHERE group_id=?", (NEW_GROUP,)).fetchall()
        assert len(seen) == 1 and seen[0]["msg_count"] == 1

        admin = admin_ready(tmp_db, monkeypatch)
        discovered = admin.get("/api/admin/groups/discovered").json()
        assert [g["group_id"] for g in discovered["discovered"]] == [NEW_GROUP]

        resp = mutate(admin, "POST", "/api/admin/groups/bind",
                      {"group_id": NEW_GROUP, "group_name": "新群"})
        assert resp.status_code == 200
        row = tmp_db.get_group_config(NEW_GROUP)
        assert row is not None and row["status"] == "active"
        assert switches_of(tmp_db, NEW_GROUP) == {
            "checkin": False, "summary": False, "report": False,
            "reminder": False, "statistics": False}
        # 绑定后从发现列表消失
        discovered = admin.get("/api/admin/groups/discovered").json()
        assert discovered["discovered"] == []

    def test_backend_bind_conflict_409(self, tmp_db, monkeypatch):
        """已绑定群再一键绑定 -> 409，⛔ 不重复建。"""
        tmp_db.create_group(NEW_GROUP, "已存在")
        admin = admin_ready(tmp_db, monkeypatch)
        resp = mutate(admin, "POST", "/api/admin/groups/bind",
                      {"group_id": NEW_GROUP})
        assert resp.status_code == 409

    def test_backend_bind_requires_auth(self, tmp_db, monkeypatch):
        """未登录调一键绑定 -> 拒绝（后台鉴权不是摆设）。"""
        admin = admin_client(tmp_db, Path(tmp_db.db_path).parent)
        bootstrap(tmp_db, monkeypatch)
        resp = admin.post("/api/admin/groups/bind",
                          json={"group_id": NEW_GROUP})
        assert resp.status_code in (401, 403)

    def test_bind_command_via_callback_gets_reply(self, tmp_db):
        """回调收到「绑定」-> 优先于群授权处理：未授权也回话 + 进两轮（全员可绑）。"""
        client, sender = make_callback_app(tmp_db)
        resp = client.post("/api/callback",
                           json=callback_payload("绑定", NEW_GROUP))
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        assert "本群尚未接入机器人" not in sender.sent[-1]["text"]
        # 第一轮不建配置（群名还没给）
        assert tmp_db.get_group_config(NEW_GROUP) is None

    def test_bind_command_via_callback_authorized(self, tmp_db):
        """回调收到授权管理员的「绑定」-> 两轮：先提示，再输群名建配置。"""
        client, sender = make_callback_app(tmp_db)
        resp = client.post("/api/callback",
                           json=callback_payload("绑定", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        # ⭐ 第 1 轮尚未建群
        assert tmp_db.get_group_config(NEW_GROUP) is None

        # 第 2 轮：纯文本 = 群名称
        resp = client.post("/api/callback",
                           json=callback_payload("回调测试群", NEW_GROUP,
                                                 phone=ADMIN_PHONE))
        assert resp.json()["replied"] is True
        assert "绑定成功" in sender.sent[-1]["text"]
        row = tmp_db.get_group_config(NEW_GROUP)
        assert row is not None and row["status"] == "active"
        assert row["group_name"] == "回调测试群"
        assert switches_of(tmp_db, NEW_GROUP) == {
            "checkin": False, "summary": False, "report": False,
            "reminder": False, "statistics": False}


# ---------- 绑定后的业务隔离 ----------

class TestIsolationAfterBind:
    def test_bound_group_runs_business_other_stays_silent(self, tmp_db):
        """绑定 + 后台开业务 -> 本群打卡正常；未绑定群只回引导不跑业务。"""
        backend = FakeBackend()
        bind_via_chat(tmp_db, backend)
        # 绑定只接入不开业务：管理员在后台开五业务 + 配覆盖 10:00 的打卡区间
        tmp_db.upsert_group_config(
            NEW_GROUP,
            biz_switches={"checkin": True, "summary": True, "report": True,
                          "reminder": True, "statistics": True},
            checkin_deadlines={"slots": [
                {"id": "am", "name": "上午", "start": "00:00", "end": "12:00"},
                {"id": "pm", "name": "下午", "start": "12:00", "end": "23:59"},
            ]})

        client, sender = make_callback_app(tmp_db)
        # 已绑定群：打卡回链接
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", NEW_GROUP))
        assert resp.json()["replied"] is True
        assert "/h5/checkin#t=" in sender.sent[-1]["text"]
        # 未绑定群：统一引导 + unauthorized_group 留痕（⛔ 不跑业务）
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", OTHER_GROUP))
        assert resp.json()["replied"] is True
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        events = security_events(tmp_db, "unauthorized_group")
        assert any(e["group_id"] == OTHER_GROUP for e in events)
        # ⛔ 未给 OTHER_GROUP 建任何配置
        assert tmp_db.get_group_config(OTHER_GROUP) is None
