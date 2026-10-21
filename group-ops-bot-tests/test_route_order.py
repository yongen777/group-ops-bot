# -*- coding: utf-8 -*-
"""顶层消息路由顺序回归（⭐ 2026-10-19 用户指定顺序，⛔ 不得调换）。

用户点出的缺陷形态：
    第一轮与第二轮是**两次独立回调**。若先判「群是否已绑定」，
    第二轮发的群名会被「未绑定群 -> 静默」吞掉，用户输完群名零反馈。

锁定顺序（`app/web/server.py` /api/callback）：
    0) 记账（发现群 + 实例登记，不回复）
    1) 是否 @机器人？否 -> 静默；载荷无 @ 字段 -> 放行
    2) 精确「绑定」 -> 第 1 轮
    3) 是否存在绑定等待会话 -> 第 2 轮
    4) 群是否已绑定且启用 -> 否 -> 静默
    5) 业务分支

⚠️ 本文件是**本地自测**，不构成真实平台回调验证（§7.2 D10 / D11）。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from app.web.server import create_app, mention_state
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all

from test_bind_phase_p0 import bind_config

NOW = datetime(2026, 10, 19, 10, 0, tzinfo=TZ)
UNBOUND = "G-ROUTE-UNBOUND"
ADMIN_PHONE = PHONE


def make_client(tmp_db):
    backend = FakeBackend()
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=bind_config(Path(tmp_db.db_path).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), sender


def payload(text, group_id=UNBOUND, phone=ADMIN_PHONE, extra=None):
    body = {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}
    if extra:
        body.update(extra)
    return body


def real_platform_payload(text, group_id=UNBOUND, phone=ADMIN_PHONE):
    """2026-10-19 实测真实形态（探针日志 payload_shape）。

    ⚠️ 关键事实：**没有 isMentioned / mentionedMobileList 之类的 @ 字段**。
    """
    return {
        "callBackMethod": "POST",
        "callBackUrl": "http://127.0.0.1:12334/api/callback",
        "groupId": group_id,
        "groupName": "路由顺序测试群",
        "phone": phone,
        "robotId": "2109573070744977410",
        "tenantId": 1,
        "textMsg": {"content": text, "tenantId": 1},
        "type": "text",
        "userName": "郑佳",
    }


# ---------- 第 1 步：@ 判定（容错放行） ----------

class TestMentionGate:
    def test_mention_state_returns_none_for_real_payload(self):
        assert mention_state(real_platform_payload("绑定")) is None

    def test_mention_state_reads_explicit_flags(self):
        assert mention_state({"isMentioned": False}) is False
        assert mention_state({"isMentioned": True}) is True
        assert mention_state({"textMsg": {"isMentioned": False}}) is False
        assert mention_state({"mentionedMobileList": []}) is False
        assert mention_state({"mentionedMobileList": [PHONE]}) is True
        assert mention_state({"unrelated": 1}) is None

    def test_not_mentioned_is_silent(self, tmp_db):
        """显式「未 @」-> 静默：不回复、不当业务处理。"""
        enable_all(tmp_db)
        client, sender = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=payload("打卡", GROUP, extra={"isMentioned": False}))
        assert resp.status_code == 200
        assert resp.json()["replied"] is False
        assert resp.json().get("ignored") == "not_mentioned"
        assert sender.sent == []

    def test_not_mentioned_does_not_create_group(self, tmp_db):
        """未 @ 的「绑定」也不进第 1 轮（顺序：@ 判定在第 2 步之前）。"""
        client, sender = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=payload("绑定", UNBOUND,
                                        extra={"isMentioned": False}))
        assert resp.json()["replied"] is False
        assert tmp_db.get_group_config(UNBOUND) is None
        assert tmp_db.get_pending_session(UNBOUND, ADMIN_PHONE, "bind") is None

    def test_mention_absent_proceeds(self, tmp_db):
        """真实载荷无 @ 字段 -> 放行（⛔ 不能拿不存在的字段拦截）。"""
        enable_all(tmp_db)
        client, sender = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=real_platform_payload("你好", GROUP))
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert sender.sent and "可用命令" in sender.sent[-1]["text"]

    def test_mentioned_true_proceeds(self, tmp_db):
        enable_all(tmp_db)
        client, sender = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=payload("你好", GROUP, extra={"isMentioned": True}))
        assert resp.json()["replied"] is True
        assert sender.sent


# ---------- 第 2/3/4 步：绑定两轮不被「未绑定静默」吞掉 ----------

class TestBindTwoRoundOrder:
    def test_round2_group_name_in_unbound_group_creates_group(self, tmp_db):
        """核心回归：未绑定群里第二轮群名 -> 建群成功（⛔ 不被静默拦截）。"""
        client, sender = make_client(tmp_db)

        # 第 1 轮：精确「绑定」
        first = client.post("/api/callback", json=payload("绑定", UNBOUND))
        assert first.json()["replied"] is True
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(UNBOUND) is None

        # 第 2 轮：纯文本群名 —— 此刻群仍**未绑定**，第 3 步必须早于第 4 步
        second = client.post("/api/callback",
                             json=payload("量子密信路由测试群", UNBOUND))
        assert second.status_code == 200
        assert second.json()["replied"] is True
        assert "绑定成功" in sender.sent[-1]["text"]
        row = tmp_db.get_group_config(UNBOUND)
        assert row is not None and row["status"] == "active"
        assert row["group_name"] == "量子密信路由测试群"

    def test_round2_with_real_payload_shape(self, tmp_db):
        """真实平台载荷形态走完两轮（无 @ 字段也不影响第二轮）。"""
        client, sender = make_client(tmp_db)
        client.post("/api/callback", json=real_platform_payload("绑定", UNBOUND))
        resp = client.post("/api/callback",
                           json=real_platform_payload("真实载荷测试群", UNBOUND))
        assert resp.json()["replied"] is True
        assert tmp_db.get_group_config(UNBOUND)["group_name"] == "真实载荷测试群"

    def test_command_during_pending_is_not_taken_as_group_name(self, tmp_db):
        """等待期内发命令：⛔ 不当群名（明确提示），也⛔ 不建群。"""
        client, sender = make_client(tmp_db)
        client.post("/api/callback", json=payload("绑定", UNBOUND))
        resp = client.post("/api/callback", json=payload("打卡", UNBOUND))
        assert resp.json()["replied"] is True
        assert "命令类消息不作群名" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(UNBOUND) is None

    def test_pending_does_not_leak_to_other_sender(self, tmp_db):
        """等待会话按「群 + 发送者」隔离：别人发文本不建群，只得到未绑定引导。"""
        client, sender = make_client(tmp_db)
        client.post("/api/callback", json=payload("绑定", UNBOUND))
        resp = client.post("/api/callback",
                           json=payload("别人的群名", UNBOUND,
                                        phone="13800138010"))
        # 别人：未绑定群 + 无等待会话 -> 统一引导话术（⛔ 不当群名）
        assert resp.json()["replied"] is True
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(UNBOUND) is None

    def test_bound_group_business_still_works_after_rounds(self, tmp_db):
        """绑定完成 + 开业务后：第 4/5 步正常（打卡返回链接）。"""
        client, sender = make_client(tmp_db)
        client.post("/api/callback", json=payload("绑定", UNBOUND))
        client.post("/api/callback", json=payload("绑定后业务群", UNBOUND))
        enable_all(tmp_db, group_id=UNBOUND)
        resp = client.post("/api/callback", json=payload("打卡", UNBOUND))
        assert resp.json()["replied"] is True
        assert "/h5/checkin#t=" in sender.sent[-1]["text"]


# ---------- 第 4 步：未绑定群回统一引导（⭐ 2026-10-19 起不再静默）----------

class TestUnboundGuide:
    def test_business_command_in_unbound_group_gets_guide(self, tmp_db):
        client, sender = make_client(tmp_db)
        resp = client.post("/api/callback",
                           json=payload("打卡", "G-ROUTE-OTHER"))
        assert resp.json()["replied"] is True
        assert len(sender.sent) == 1
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type="
            "'unauthorized_group'").fetchall()
        assert len(events) == 1
