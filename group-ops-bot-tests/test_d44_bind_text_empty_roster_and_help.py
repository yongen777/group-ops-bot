# -*- coding: utf-8 -*-
"""D44（2026-10-26 用户反馈，三件事）。

① 帮助文案里「绑定」还写着「仅后台授权的管理员可用」——D28 已经把绑定对**全员**
   开放（`bind.py` 里连 `NOT_ADMIN_TEXT` 都删了），只有文案没跟上。
② 空名单的新群：旧口径「名单为空 → 直接放行且不入册」，于是群里的人发命令被
   放行，却**永远进不了名单、后台一条记录都没有**（用户原话：「未执行实名录入
   到群成员，且后台也没有任何记录」）。改：空名单也走一次自愈，自愈不了才拦。
③ 未实名的人发**任何**文本，回「未识别该指令」太误导（他以为机器人坏了）。
   改成「实名引导 + 命令清单」；已实名的人不受影响。
   另外把「导出」也纳入实名闸门 —— 未实名本来就没导出授权，回实名提示更有用。
"""

from __future__ import annotations

from app import commands
from app.handlers import realname as realname_handler
from helpers import GROUP, PHONE
from test_d34_removed_and_rename import _bot, _last, _say


class TestBindHelpText:
    def test_help_text_does_not_call_bind_admin_only(self, tmp_db):
        """帮助文案里不能再出现「绑定…仅后台授权」。"""
        assert "「绑定」将本群接入机器人（本群任一成员均可操作）" in commands.HELP_TEXT
        assert "仅绑定管理员可用" not in commands.HELP_TEXT
        # 导出行保留「仅后台授权」——导出确实要白名单
        assert "「导出」一键导出全部业务的统计表（仅后台授权的管理员可用）" in commands.HELP_TEXT
        assert "任一成员" in commands.USAGE_TEXT[commands.CMD_BIND]


class TestEmptyRosterHealsInsteadOfSilentPass:
    def test_unknown_user_in_empty_group_is_blocked(self, tmp_db):
        """空名单 + 全局也没这个人 -> 发「打卡」必须回实名引导，⛔ 不放行。"""
        client, _b, sender = _bot(tmp_db, roster=[], user=None)
        assert tmp_db.list_member_tasks(GROUP) == []

        _say(client, "打卡")

        assert _last(sender) == realname_handler.REQUIRE_REALNAME_TEXT
        assert tmp_db.list_member_tasks(GROUP) == [], "⛔ 不该凭空入册"

    def test_known_user_in_empty_group_is_healed(self, tmp_db):
        """空名单 + 全局有姓名 -> 无感入册并放行（后台立刻能看到他）。"""
        client, _b, sender = _bot(tmp_db, roster=[],
                                  user={"id": 7, "phone": PHONE, "name": "王红霞"})
        _say(client, "打卡")

        rows = list(tmp_db.list_member_tasks(GROUP))
        assert len(rows) == 1, "必须入册，否则后台永远没记录"
        assert rows[0]["nickname"] == "王红霞"
        assert rows[0]["source"] == "auto_heal"
        assert realname_handler.REQUIRE_REALNAME_TEXT not in _last(sender)


class TestExportGatedToo:
    def test_unregistered_export_asks_for_realname(self, tmp_db):
        """未实名发「导出」-> 实名引导（而不是「无导出权限」）。"""
        client, _b, sender = _bot(tmp_db, roster=[], user=None)
        # 名单里只有别人，发送者既不在名单、全局也没姓名
        tmp_db.upsert_member_task(GROUP, "13800138999", nickname="别人")
        _say(client, "导出")
        assert _last(sender) == realname_handler.REQUIRE_REALNAME_TEXT

    def test_registered_export_not_gated(self, tmp_db):
        """已实名发「导出」-> ⛔ 不再被实名闸门拦（走导出自己的权限判定）。"""
        client, _b, sender = _bot(tmp_db, roster=[PHONE],
                                  user={"id": 7, "phone": PHONE, "name": "王红霞"})
        _say(client, "导出")
        assert _last(sender) != realname_handler.REQUIRE_REALNAME_TEXT


class TestUnknownTextForUnregistered:
    def test_unregistered_random_text_shows_realname_plus_help(self, tmp_db):
        """未实名发乱文本 -> 先告诉他去实名，再给命令清单。"""
        client, _b, sender = _bot(tmp_db, roster=[], user=None)
        tmp_db.upsert_member_task(GROUP, "13800138999", nickname="别人")

        _say(client, "我要怎么打卡啊")

        text = _last(sender)
        assert text.startswith(realname_handler.REQUIRE_REALNAME_TEXT)
        assert "未识别该指令" in text, "⛔ 命令清单不能丢，否则他不知道有哪些命令"

    def test_registered_random_text_shows_help_only(self, tmp_db):
        """已实名发乱文本 -> 只有帮助文案（⛔ 不重复劝实名）。"""
        client, _b, sender = _bot(tmp_db, roster=[PHONE],
                                  user={"id": 7, "phone": PHONE, "name": "王红霞"})
        _say(client, "我要怎么打卡啊")

        text = _last(sender)
        assert text == commands.HELP_TEXT
        assert realname_handler.REQUIRE_REALNAME_TEXT not in text

    def test_summary_pending_wins_over_help(self, tmp_db):
        """两轮总结进行中发文本 -> 仍按总结内容处理（⛔ 不被实名引导打断）。"""
        client, _b, sender = _bot(tmp_db, roster=[PHONE],
                                  user={"id": 7, "phone": PHONE, "name": "王红霞"})
        _say(client, "总结")
        _say(client, "今天做完了三件事")
        text = _last(sender)
        assert realname_handler.REQUIRE_REALNAME_TEXT not in text
