# -*- coding: utf-8 -*-
"""D43（2026-10-26 用户反馈，两件事）。

① 新群实名：郑佳在**别的群**实名过（全局档案 `sys_user.name` 有值），新建的群
   名单还是空的。他发「@机器人 实名」，旧实现只回
   「您已完成实名登记……无需重复提交」，于是**他永远进不了本群名单**，
   后台「群成员」里看不到人。
   口径：本群名单里没有他 -> 先把全局档案补进本群名单（source=auto_heal），
   再回「已为您加入本群名单」。真重复（已在名单里）仍回原来那句。

② 未绑定群删不掉：机器人被拉进群、还没绑定就被移出，`qmx_robot_instance`
   留着 WAIT_BIND 一行、`group_seen` 也留着一条；后台「移除群聊」按钮是亮的，
   但旧接口对没有 group_config 的群一律 404，于是这行永远挂在后台。
"""

from __future__ import annotations

from app.handlers import realname as realname_handler
from app.state import StateStore
from helpers import GROUP, PHONE
from test_d34_removed_and_rename import _bot, _last, _say
from test_admin import ADMIN_PASS, NEW_PASS, bootstrap, login, mutate


class TestNewGroupRealnameHealsIntoRoster:
    def test_empty_roster_registers_and_replies_healed(self, tmp_db):
        """空名单的新群：发「实名」-> 入册 + 回「已加入本群名单」。"""
        client, _b, sender = _bot(tmp_db, roster=[],
                                  user={"id": 7, "phone": PHONE, "name": "郑佳"})
        assert tmp_db.list_member_tasks(GROUP) == []

        _say(client, "实名")

        assert _last(sender) == realname_handler.HEALED_INTO_ROSTER_TEXT.format(
            name="郑佳", masked=realname_handler._mask(PHONE))
        row = tmp_db.get_member_task(GROUP, PHONE)
        assert row is not None, "⛔ 回执说加入名单，就必须真的入册"
        assert row["nickname"] == "郑佳"
        assert row["source"] == "auto_heal"

    def test_true_duplicate_still_blocked(self, tmp_db):
        """已在名单里的人再发「实名」-> 仍是原来那句，⛔ 不重复写。"""
        client, _b, sender = _bot(tmp_db, roster=[PHONE],
                                  user={"id": 7, "phone": PHONE, "name": "郑佳"})
        _say(client, "实名")
        assert _last(sender) == realname_handler.ALREADY_REGISTERED_TEXT.format(
            name="郑佳", masked=realname_handler._mask(PHONE))
        assert len(tmp_db.list_member_tasks(GROUP)) == 1

    def test_removed_user_still_blocked(self, tmp_db):
        """被移出的人：即便名单为空也⛔ 不许自愈还魂（D37 不被本改动击穿）。"""
        client, _b, sender = _bot(tmp_db, roster=[],
                                  user={"id": 7, "phone": PHONE, "name": "郑佳"})
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "实名")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_empty_roster_without_global_name_asks(self, tmp_db):
        """全局档案也没有姓名 -> 正常走登记（⛔ 不误报已实名）。"""
        client, _b, sender = _bot(tmp_db, roster=[], user=None)
        _say(client, "实名")
        assert _last(sender) == realname_handler.ASK_TEXT
        assert tmp_db.list_member_tasks(GROUP) == []


class TestRemoveUnboundGroup:
    def _admin(self, tmp_db, tmp_path):
        """建号 -> 首登 -> 改密 -> 用新密码重新登录（改密会吊销旧会话）。"""
        bootstrap(tmp_db, None, password=ADMIN_PASS)
        from test_admin import admin_client

        client = admin_client(tmp_db, tmp_path)
        assert login(client).status_code == 200
        csrf = client.get("/api/admin/me").json()["csrf_token"]
        resp = client.post("/api/admin/change-password",
                           json={"old_password": ADMIN_PASS,
                                 "new_password": NEW_PASS},
                           headers={"X-CSRF-Token": csrf})
        assert resp.status_code == 200, resp.text
        assert login(client, password=NEW_PASS).status_code == 200
        return client

    def test_forget_discovered_group(self, tmp_db, tmp_path):
        """只被发现过、从未绑定的群 -> 能删掉那一行。"""
        client = self._admin(tmp_db, tmp_path)
        tmp_db.note_group_seen(GROUP, PHONE, "郑佳")
        tmp_db.ensure_robot_instance(GROUP)
        assert tmp_db.get_group_config(GROUP) is None
        assert tmp_db.get_robot_instance(GROUP) is not None
        assert len(list(tmp_db.list_unbound_groups())) == 1

        resp = mutate(client, "delete", "/api/admin/robot-instances/%s" % GROUP)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "未绑定群记录已移除"
        assert tmp_db.get_robot_instance(GROUP) is None
        assert list(tmp_db.list_unbound_groups()) == []

    def test_bound_group_still_cascades(self, tmp_db, tmp_path):
        """已绑定的群 -> 仍走级联删除（回归，⛔ 不能被新分支截胡）。"""
        client = self._admin(tmp_db, tmp_path)
        tmp_db.create_group(GROUP, "测试群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")

        resp = mutate(client, "delete", "/api/admin/robot-instances/%s" % GROUP)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "群聊已移除"
        assert tmp_db.get_group_config(GROUP) is None
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_unknown_group_404(self, tmp_db, tmp_path):
        """两处都没有的群 -> 仍 404（⛔ 不伪装成功）。"""
        client = self._admin(tmp_db, tmp_path)
        resp = mutate(client, "delete", "/api/admin/robot-instances/G_NOT_EXIST")
        assert resp.status_code == 404

    def test_state_layer_refuses_bound_group(self, tmp_db):
        """state 层自保护：已绑定的群调 forget_unbound_group 返回 {} 且⛔ 不删。"""
        tmp_db.create_group(GROUP, "测试群")
        assert tmp_db.forget_unbound_group(GROUP) == {}
        assert tmp_db.get_group_config(GROUP) is not None
