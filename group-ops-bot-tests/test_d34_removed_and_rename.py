# -*- coding: utf-8 -*-
"""D34（2026-10-22 用户指令）：①B 移除留痕（防自愈还魂）+ ②B 改号同步全局档案。

口径：
- ①B 管理员「移出群」-> 写 `group_member_removed`；自愈⛔ 不许复活；
  管理员重新添加 -> 清痕迹（解封）。
- ②B 后台改号必须**先**同步 MySQL `sys_user.phone`（MCP user.update_phone），
  失败则整笔取消，⛔ 不留半改状态；否则一人会分裂成两个身份。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.handlers import realname as realname_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config
from test_realname_phase import NOW, callback_payload


def _bot(tmp_db, roster=(), user=None):
    enable_all(tmp_db)
    for r in roster:
        tmp_db.upsert_member_task(GROUP, r, nickname="在册成员")
    backend = FakeBackend()
    backend.results["user_get"] = {"user": user}
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


def _last(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


# ---------- ①B 移除留痕：不许自愈还魂 ----------

class TestRemovedBlocksHeal:
    def test_removed_member_is_blocked_not_healed(self, tmp_db):
        """被移出的人即便全局有姓名，也⛔ 不许补回名单。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"],
                                  user={"id": 7, "phone": PHONE, "name": "郑佳"})
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "打卡")
        assert _last(sender) == realname_handler.REMOVED_FROM_ROSTER_TEXT
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_removed_text_is_not_realname_text(self, tmp_db):
        """⛔ 不能回「请先实名」——那会让人白跑一趟。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"],
                                  user={"id": 7, "name": "郑佳"})
        tmp_db.mark_member_removed(GROUP, PHONE)
        _say(client, "打卡")
        assert _last(sender) != realname_handler.REQUIRE_REALNAME_TEXT

    def test_readd_clears_mark_and_passes(self, tmp_db):
        """管理员重新添加 = 解封：痕迹清掉，业务恢复。"""
        client, _b, sender = _bot(tmp_db, roster=["13800138099"],
                                  user={"id": 7, "name": "郑佳"})
        tmp_db.mark_member_removed(GROUP, PHONE)
        assert tmp_db.is_member_removed(GROUP, PHONE) is True
        tmp_db.clear_member_removed(GROUP, PHONE)          # 后台重新添加
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        assert tmp_db.is_member_removed(GROUP, PHONE) is False
        _say(client, "打卡")
        assert _last(sender) != realname_handler.REMOVED_FROM_ROSTER_TEXT

    def test_mark_is_idempotent(self, tmp_db):
        tmp_db.mark_member_removed(GROUP, PHONE)
        tmp_db.mark_member_removed(GROUP, PHONE)
        rows = tmp_db._conn.execute(
            "SELECT COUNT(*) FROM group_member_removed WHERE group_id=?",
            (GROUP,)).fetchone()[0]
        assert rows == 1


# ---------- ②B 改号同步全局档案 ----------

def _admin(tmp_db):
    from test_d32_member_phone_guard import _ready
    return _ready(tmp_db)


class TestRenameSyncsGlobal:
    def test_rename_calls_backend_and_moves_row(self, tmp_db, monkeypatch):
        """改号：调 MCP user.update_phone，且本地名单换号（旧号不再存在）。"""
        from test_admin import mutate
        admin = _admin(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        called = {}
        original = FakeBackend.user_update_phone

        def spy(self, **kw):
            called.update(kw)
            return original(self, **kw)

        monkeypatch.setattr(FakeBackend, "user_update_phone", spy)
        resp = mutate(admin, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": "13800138099"})
        assert resp.status_code == 200, resp.text
        assert called.get("old_phone") == PHONE
        assert called.get("new_phone") == "13800138099"
        assert tmp_db.get_member_task(GROUP, "13800138099") is not None
        assert tmp_db.get_member_task(GROUP, PHONE) is None

    def test_backend_failure_aborts_rename(self, tmp_db, monkeypatch):
        """MCP 改号失败 -> 502，本地名单⛔ 不动（不留半改状态）。"""
        from test_admin import mutate
        admin = _admin(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")

        def boom(self, **kw):
            raise RuntimeError("mcp down")

        monkeypatch.setattr(FakeBackend, "user_update_phone", boom)
        resp = mutate(admin, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": "13800138099"})
        assert resp.status_code == 502
        assert tmp_db.get_member_task(GROUP, PHONE) is not None
        assert tmp_db.get_member_task(GROUP, "13800138099") is None


# ---------- 后台「移出」写留痕 / 「添加」清留痕 ----------

class TestAdminRemoveWritesMark:
    def test_remove_marks_then_add_clears(self, tmp_db):
        from test_admin import mutate
        admin = _admin(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        assert mutate(admin, "DELETE",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE)
                      ).status_code == 200
        assert tmp_db.is_member_removed(GROUP, PHONE) is True
        assert mutate(admin, "POST",
                      "/api/admin/groups/%s/members" % GROUP,
                      {"user_id": PHONE, "nickname": "郑佳"}).status_code == 200
        assert tmp_db.is_member_removed(GROUP, PHONE) is False


# ---------- ②B 跨群联动（2026-10-22 用户指令：提示"全群联动"必须是真的） ----------

GROUP2 = "G200"


class TestRenameAcrossGroups:
    def test_renames_in_all_groups(self, tmp_db):
        tmp_db.create_group(GROUP2, "第二群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task(GROUP2, PHONE, nickname="郑佳")
        tmp_db.upsert_whitelist(GROUP2, PHONE, "ADMIN_ALL")

        result = tmp_db.rename_member_all_groups(PHONE, "13800138099")
        assert sorted(result["renamed"]) == sorted([GROUP, GROUP2])
        assert result["conflicts"] == []
        assert tmp_db.get_member_task(GROUP, "13800138099") is not None
        assert tmp_db.get_member_task(GROUP2, "13800138099") is not None
        assert tmp_db.get_member_task(GROUP, PHONE) is None
        assert tmp_db.get_member_task(GROUP2, PHONE) is None
        # 授权跟着人跨群走
        assert [r["user_id"] for r in tmp_db.list_whitelist(GROUP2)] == \
            ["13800138099"]

    def test_conflict_group_skipped_not_merged(self, tmp_db):
        """某群已存在新号（同号两人）-> 跳过该群并报告，⛔ 不静默合并。"""
        tmp_db.create_group(GROUP2, "第二群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task(GROUP2, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task(GROUP2, "13800138099", nickname="另一个人")

        result = tmp_db.rename_member_all_groups(PHONE, "13800138099")
        assert result["renamed"] == [GROUP]
        assert result["conflicts"] == [GROUP2]
        # 冲突群保持原样：两个人都在，谁也没被覆盖
        assert tmp_db.get_member_task(GROUP2, PHONE)["nickname"] == "郑佳"
        assert tmp_db.get_member_task(GROUP2, "13800138099")["nickname"] == "另一个人"

    def test_api_returns_group_counts(self, tmp_db):
        from test_admin import mutate
        admin = _admin(tmp_db)
        tmp_db.create_group(GROUP2, "第二群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task(GROUP2, PHONE, nickname="郑佳")
        resp = mutate(admin, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": "13800138099"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert sorted(body["renamed_groups"]) == sorted([GROUP, GROUP2])
        assert body["conflict_groups"] == []

    def test_clears_pending_sessions_everywhere(self, tmp_db):
        from helpers import TZ
        from datetime import datetime
        tmp_db.create_group(GROUP2, "第二群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task(GROUP2, PHONE, nickname="郑佳")
        now = datetime(2026, 10, 22, 10, 0, tzinfo=TZ)
        tmp_db.put_pending_session(GROUP2, PHONE, "summary", "日报", now)
        tmp_db.rename_member_all_groups(PHONE, "13800138099")
        assert tmp_db.get_pending_session(GROUP2, PHONE, "summary", now) is None
