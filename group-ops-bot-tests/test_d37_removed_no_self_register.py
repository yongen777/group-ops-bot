# -*- coding: utf-8 -*-
"""D37（2026-10-23 用户指令）：被移出者⛔ 不许在群内自助实名回册。

背景：D34 的移除留痕只挡了「业务命令触发的自愈」，**实名入口没挡** ——
被移出的人发一次「@机器人 实名」就能重新入册，等于「移出」形同虚设。
后台移出弹窗的提示已改为「不再支持群成员 @机器人 实名录入」，本文件锁住实现。
"""

from __future__ import annotations

from app.handlers import realname as realname_handler
from helpers import GROUP, PHONE
from test_d34_removed_and_rename import _bot, _last, _say


class TestRemovedCannotSelfRegister:
    def test_realname_command_blocked(self, tmp_db):
        """被移出者发「实名」-> 回「已移出」，且⛔ 不挂会话。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"])
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "实名")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT
        assert tmp_db.get_pending_session(GROUP, PHONE,
                                          realname_handler.BIZ,
                                          tmp_db_now()) is None

    def test_removed_wins_over_already_registered(self, tmp_db):
        """已实名但被移出 -> 回「已移出」，⛔ 不用「已完成登记」误导他。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"],
                                  user={"id": 7, "phone": PHONE, "name": "郑佳"})
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "实名")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT

    def test_pending_submit_blocked(self, tmp_db):
        """会话挂起期间被移出 -> 提交姓名手机号也⛔ 不许入册。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"])
        _say(client, "实名")
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "张三 13800138009")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_admin_readd_restores_self_service(self, tmp_db):
        """管理员重新添加（清留痕）-> 实名流程恢复正常。"""
        client, backend, sender = _bot(tmp_db, roster=["13800138099"])
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "实名")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT

        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.clear_member_removed(GROUP, PHONE)
        backend.results["user_get"] = {"user": None}
        _say(client, "实名")
        assert _last(sender) == realname_handler.ASK_TEXT

    def test_normal_member_unaffected(self, tmp_db):
        """没被移出的人发「实名」照旧走登记（回归）。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"])
        _say(client, "实名")
        assert _last(sender) == realname_handler.ASK_TEXT


def tmp_db_now():
    from test_realname_phase import NOW
    return NOW
