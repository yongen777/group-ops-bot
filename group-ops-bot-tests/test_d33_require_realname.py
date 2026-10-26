# -*- coding: utf-8 -*-
"""D33（2026-10-22 用户指令）：群内业务命令的「实名校验」闸门。

用户口径（原话）：
- 「群运维机器人应该添加一个效验就是后台是否录入该用户，未录入就是没有
  实名提醒用户 @机器人 实名这个操作执行。」
- 「这个群成员就是用户实名」
- 「为什么还需要加一个开关，默认这个固定回复效验功能一直打开啊」

因此本文件钉死：
1. 判定真源 = **本群成员名单**（`group_member_task_config`），⛔ 不查 MySQL 姓名；
2. **常开**，⛔ 无群级开关；
3. 拦的是一组**业务命令**（打卡 / 总结 / 报数 / 导出）；实名/绑定/帮助⛔ 不拦
   （⭐ D44 起「导出」也纳入 —— 未实名本来就没导出授权，回实名提示更有用）
4. **名单为空 = 该群还没治理**：⭐ D44（2026-10-26 用户反馈）改口径 —— 空名单群
   也走一次全局档案自愈：有姓名则入册放行，没有则拦下引导实名。
   旧口径「空名单一律放行且不入册」会让新群里的人发命令被放行、却永远进不了
   名单（用户原话：「未执行实名录入到群成员，且后台也没有任何记录」）。
5. 实名成功 -> **自动进名单**（否则「不在名单→提示实名→实名又不进名单」死循环）。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.handlers import realname as realname_handler
from app.web.server import PAUSED_MEMBER_TEXT, create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config
from test_realname_phase import NOW, callback_payload


def _client(tmp_db, roster=(), paused_phones=()):
    enable_all(tmp_db)
    for row in roster:
        tmp_db.upsert_member_task(GROUP, row, nickname="测试成员")
    for phone in paused_phones:
        tmp_db.upsert_member_task(GROUP, phone, nickname="暂停成员",
                                  enabled=False)
    backend = FakeBackend()
    # 默认模拟「人员目录里还没有姓名」：否则实名处理器会判「已实名」直接拦，
    # 走不到「姓名 手机号」登记分支（本文件要多测实名流程本身）。
    backend.results["user_get"] = {"user": None}
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


def _last_text(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


# ---------- 1. 名单为空：不拦（新群还没治理） ----------

class TestEmptyRoster:
    def test_empty_roster_without_global_name_is_blocked(self, tmp_db):
        """⭐ D44：空名单 + 全局档案也没有他 -> 拦下并引导实名（⛔ 不再静默放行）。"""
        client, _b, sender = _client(tmp_db)
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_empty_roster_with_global_name_heals(self, tmp_db):
        """⭐ D44：空名单 + 全局档案有姓名 -> 无感入册并放行（后台立刻可见）。"""
        client, backend, sender = _client(tmp_db)
        backend.results["user_get"] = {"user": {"id": 7, "name": "郑佳"}}
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT
        row = tmp_db.get_member_task(GROUP, PHONE)
        assert row is not None and row["source"] == "auto_heal"


# ---------- 2. 名单非空：在册放行、不在册拦 ----------

class TestRosterGate:
    def test_in_roster_passes(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=[PHONE])
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT

    def test_not_in_roster_blocked_checkin(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_not_in_roster_blocked_summary(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "总结")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_not_in_roster_blocked_report(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "报数")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_paused_takes_precedence(self, tmp_db):
        """在名单但 enabled=0（暂停考核）-> 回暂停话术，⛔ 不回实名话术。"""
        client, _b, sender = _client(tmp_db, paused_phones=[PHONE])
        _say(client, "打卡")
        assert _last_text(sender) == PAUSED_MEMBER_TEXT

    def test_no_switch_anywhere(self):
        from app.state import StateStore
        assert not hasattr(StateStore, "EXTRA_SWITCH_KEYS")
        assert not hasattr(realname_handler, "GATE_SWITCH")
        assert not hasattr(realname_handler, "gate_enabled")


# ---------- 3. 豁免：别把人堵死 ----------

class TestExemptions:
    def test_realname_command_not_gated(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "实名")
        assert _last_text(sender) == realname_handler.ASK_TEXT

    def test_unknown_text_not_gated(self, tmp_db):
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "今天天气不错")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT

    def test_export_is_gated_by_realname(self, tmp_db):
        """⭐ D44：导出也纳入实名闸门（未实名 -> 先实名，而不是「无导出权限」）。"""
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "导出")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT


# ---------- 4. 实名成功后自动进名单（闭环，⛔ 不能死循环） ----------

class TestRealnameJoinsRoster:
    def test_realname_adds_member(self, tmp_db):
        client, _b, _s = _client(tmp_db, roster=["13800138099"])
        assert tmp_db.get_member_task(GROUP, PHONE) is None
        tmp_db.put_pending_session(GROUP, PHONE, "realname", None, NOW)
        _say(client, "张三 13800138009")    # ⚠️ 平台身份仍是 PHONE
        row = tmp_db.get_member_task(GROUP, PHONE)
        assert row is not None, "实名成功必须把该平台身份加入本群名单"
        assert row["nickname"] == "张三"
        assert bool(row["enabled"]) is True

    def test_after_realname_business_works(self, tmp_db):
        """实名 -> 进名单 -> 再打卡不再被拦。"""
        client, _b, sender = _client(tmp_db, roster=["13800138099"])
        tmp_db.put_pending_session(GROUP, PHONE, "realname", None, NOW)
        _say(client, "张三 13800138009")
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT


# ---------- 5. 文案与副作用 ----------

class TestTextAndSideEffects:
    def test_text_mentions_realname_instruction(self):
        assert "@机器人 实名" in realname_handler.REQUIRE_REALNAME_TEXT

    def test_gated_command_writes_nothing(self, tmp_db):
        client, backend, _s = _client(tmp_db, roster=["13800138099"])
        _say(client, "总结")
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW) is None
        assert not [c for c in backend.calls if c[0] == "summary_save"]


# ---------- 6. 死循环回归（2026-10-22 实测：不在名单但已实名过 -> 卡死） ----------

class TestDeadLoop:
    def test_not_in_roster_can_start_realname(self, tmp_db):
        """不在本群名单时，「实名」必须放行 —— 否则「打卡要实名 / 实名说已完成」死循环。"""
        client, backend, sender = _client(tmp_db, roster=["13800138099"])
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "郑佳"}}
        _say(client, "实名")
        assert _last_text(sender) == realname_handler.ASK_TEXT

    def test_after_realname_already_registered(self, tmp_db):
        """入册 + 有姓名之后，再发「实名」才回「已完成登记」。"""
        client, backend, sender = _client(tmp_db, roster=["13800138099"])
        tmp_db.put_pending_session(GROUP, PHONE, "realname", None, NOW)
        _say(client, "张三 13800138009")
        assert tmp_db.get_member_task(GROUP, PHONE) is not None
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "张三"}}
        _say(client, "实名")
        assert _last_text(sender).startswith("您已完成实名登记")

    def test_full_loop_unblocks_business(self, tmp_db):
        """完整闭环：被拦 -> 实名 -> 入册 -> 打卡放行（不再提示实名）。"""
        client, backend, sender = _client(tmp_db, roster=["13800138099"])
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT
        _say(client, "实名")
        assert _last_text(sender) == realname_handler.ASK_TEXT
        _say(client, "张三 13800138009")
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT


# ---------- 7. D34 全局档案静默自愈入册（2026-10-22 用户指令） ----------

class TestAutoHeal:
    def _heal_client(self, tmp_db):
        """名单里有别人（非空）、群里的人全局已实名 -> 触发静默入册。"""
        client, backend, sender = _client(tmp_db, roster=["13800138099"])
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "郑佳"}}
        return client, backend, sender

    def test_heals_and_passes_silently(self, tmp_db):
        """分支 A：全局有姓名 -> 静默入册 + 无感放行，⛔ 不出现实名话术。"""
        client, _b, sender = self._heal_client(tmp_db)
        assert tmp_db.get_member_task(GROUP, PHONE) is None
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT
        row = tmp_db.get_member_task(GROUP, PHONE)
        assert row is not None, "应静默补录进本群名单"
        assert row["nickname"] == "郑佳"
        assert bool(row["enabled"]) is True
        assert row["source"] == "auto_heal"

    def test_heals_only_once(self, tmp_db):
        """第二次命令走命中分支，⛔ 不重复写库。"""
        client, _b, _s = self._heal_client(tmp_db)
        _say(client, "打卡")
        first = tmp_db.get_member_task(GROUP, PHONE)["updated_at"]
        _say(client, "打卡")
        assert tmp_db.get_member_task(GROUP, PHONE)["updated_at"] == first

    def test_pure_new_user_still_blocked(self, tmp_db):
        """分支 B：两处都没有姓名 -> 照旧拦截并引导实名。"""
        client, backend, sender = _client(tmp_db, roster=["13800138099"])
        backend.results["user_get"] = {"user": None}
        _say(client, "打卡")
        assert _last_text(sender) == realname_handler.REQUIRE_REALNAME_TEXT
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_empty_roster_heals_into_roster(self, tmp_db):
        """⭐ D44 改口径：空名单群也自愈入册（旧口径是「放行但不入册」）。

        用户线上反馈：新群里的人发命令被放行，但**后台一条记录都没有**，
        因为他永远进不了名单。
        """
        client, backend, sender = _client(tmp_db)   # 空名单
        backend.results["user_get"] = {"user": {"id": 7, "name": "郑佳"}}
        _say(client, "打卡")
        assert _last_text(sender) != realname_handler.REQUIRE_REALNAME_TEXT
        row = tmp_db.get_member_task(GROUP, PHONE)
        assert row is not None, "必须入册，否则后台永远看不到这个人"
        assert row["source"] == "auto_heal"

    def test_visible_in_admin_members_api(self, tmp_db):
        """自愈后，后台 `GET /api/admin/groups/{gid}/members` 立即可见。"""
        from test_d32_member_phone_guard import _ready
        admin = _ready(tmp_db)                      # 建号 + 登录 + 建群
        client, backend, _s = _client(tmp_db, roster=["13800138099"])
        backend.results["user_get"] = {
            "user": {"id": 7, "phone": PHONE, "name": "郑佳"}}
        _say(client, "打卡")
        body = admin.get("/api/admin/groups/%s/members" % GROUP).json()
        ids = [m["user_id"] for m in body["members"]]
        assert PHONE in ids, "后台群成员接口应能立刻看到自愈补录的人"
        me = [m for m in body["members"] if m["user_id"] == PHONE][0]
        assert me["nickname"] == "郑佳" and me["enabled"] is True
