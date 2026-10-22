# -*- coding: utf-8 -*-
"""D36（2026-10-22 用户指令）：单人单号 —— 撞号防护与后台待办。

两个入口都要防：
1. **群内实名**：填了别人的号 -> ⛔ 不允许 upsert（否则会把别人的姓名覆盖掉），
   回「该手机号已被其他人员登记」，并落一条待办；
2. **后台改号**：跨群撞号 -> 跳过该群 + 落待办（上一版只弹 toast，关掉就丢）。

待办在后台「群成员」页顶部持久展示，管理员核对后点「已处理」才消失。
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.handlers import realname as realname_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, enable_all, make_config
from test_realname_phase import NOW, callback_payload

OTHER_PHONE = "13800138099"


class ScriptedBackend(FakeBackend):
    """按手机号返回不同 user（模拟「这个号已经是别人的」）。"""

    def __init__(self, by_phone):
        super().__init__()
        self.by_phone = by_phone

    def user_get(self, *, phone):
        self._record("user_get", {"phone": phone})
        self._maybe_raise("user_get")
        return self.by_phone.get(phone, {"user": None})


def _bot(tmp_db, backend=None, roster=()):
    enable_all(tmp_db)
    for r in roster:
        tmp_db.upsert_member_task(GROUP, r, nickname="在册成员")
    backend = backend or ScriptedBackend({})
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


def _start_realname(tmp_db):
    tmp_db.put_pending_session(GROUP, PHONE, "realname", None, NOW)


# ---------- 1. 实名撞号 ----------

class TestRealnameConflict:
    def test_rejects_phone_owned_by_someone_else(self, tmp_db):
        """填别人的号：⛔ 拒绝 + ⛔ 不写库（不能覆盖别人的姓名）。"""
        backend = ScriptedBackend({
            OTHER_PHONE: {"user": {"id": 9, "phone": OTHER_PHONE,
                                   "name": "李四"}}})
        client, _b, sender = _bot(tmp_db, backend)
        _start_realname(tmp_db)
        _say(client, "张三 " + OTHER_PHONE)
        assert _last(sender) == realname_handler.PHONE_TAKEN_TEXT
        assert not [c for c in backend.calls if c[0] == "user_upsert"]

    def test_conflict_recorded_as_todo(self, tmp_db):
        backend = ScriptedBackend({
            OTHER_PHONE: {"user": {"id": 9, "name": "李四"}}})
        client, _b, _s = _bot(tmp_db, backend)
        _start_realname(tmp_db)
        _say(client, "张三 " + OTHER_PHONE)
        conflicts = tmp_db.list_phone_conflicts(GROUP)
        assert len(conflicts) == 1
        assert conflicts[0]["phone"] == OTHER_PHONE
        assert conflicts[0]["kind"] == "realname"
        assert "李四" in (conflicts[0]["detail"] or "")

    def test_session_kept_so_user_can_retry(self, tmp_db):
        """撞号后⛔ 不踢出会话：用户可直接重发自己的号。"""
        backend = ScriptedBackend({
            OTHER_PHONE: {"user": {"id": 9, "name": "李四"}}})
        client, _b, sender = _bot(tmp_db, backend)
        _start_realname(tmp_db)
        _say(client, "张三 " + OTHER_PHONE)
        assert tmp_db.get_pending_session(GROUP, PHONE, "realname", NOW) is not None
        # 换成自己的平台号 -> 成功
        _say(client, "张三 " + PHONE)
        assert _last(sender) != realname_handler.PHONE_TAKEN_TEXT
        assert [c for c in backend.calls if c[0] == "user_upsert"]

    def test_own_phone_with_empty_name_is_allowed(self, tmp_db):
        """目录里有该号但没姓名（历史遗留）-> 允许补录。"""
        backend = ScriptedBackend({
            PHONE: {"user": {"id": 7, "phone": PHONE, "name": None}}})
        client, _b, sender = _bot(tmp_db, backend)
        _start_realname(tmp_db)
        _say(client, "张三 " + PHONE)
        assert _last(sender) != realname_handler.PHONE_TAKEN_TEXT
        assert [c for c in backend.calls if c[0] == "user_upsert"]


# ---------- 2. 待办接口 ----------

def _admin(tmp_db):
    from test_d32_member_phone_guard import _ready
    return _ready(tmp_db)


class TestConflictTodoApi:
    def test_list_and_resolve(self, tmp_db):
        from test_admin import mutate
        admin = _admin(tmp_db)
        cid = tmp_db.add_phone_conflict(GROUP, OTHER_PHONE, "rename",
                                        "以下群已存在该号码：G200")
        body = admin.get("/api/admin/groups/%s/member-conflicts" % GROUP).json()
        assert len(body["conflicts"]) == 1
        assert body["conflicts"][0]["phone"] == OTHER_PHONE
        assert body["conflicts"][0]["detail"].endswith("G200")

        resp = mutate(admin, "POST",
                      "/api/admin/groups/%s/member-conflicts/%d/resolve"
                      % (GROUP, cid), {})
        assert resp.status_code == 200, resp.text
        assert admin.get(
            "/api/admin/groups/%s/member-conflicts" % GROUP).json()["conflicts"] == []

    def test_resolve_twice_returns_404(self, tmp_db):
        from test_admin import mutate
        admin = _admin(tmp_db)
        cid = tmp_db.add_phone_conflict(GROUP, OTHER_PHONE, "realname", "x")
        path = "/api/admin/groups/%s/member-conflicts/%d/resolve" % (GROUP, cid)
        assert mutate(admin, "POST", path, {}).status_code == 200
        assert mutate(admin, "POST", path, {}).status_code == 404

    def test_add_is_idempotent_while_open(self, tmp_db):
        a = tmp_db.add_phone_conflict(GROUP, OTHER_PHONE, "realname", "x")
        b = tmp_db.add_phone_conflict(GROUP, OTHER_PHONE, "realname", "y")
        assert a == b
        assert len(tmp_db.list_phone_conflicts(GROUP)) == 1


# ---------- 3. 改号撞号也落待办 ----------

class TestRenameConflictTodo:
    def test_cross_group_conflict_creates_todo(self, tmp_db):
        from test_admin import mutate
        admin = _admin(tmp_db)
        tmp_db.create_group("G200", "第二群")
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="郑佳")
        tmp_db.upsert_member_task("G200", PHONE, nickname="郑佳")
        tmp_db.upsert_member_task("G200", OTHER_PHONE, nickname="另一个人")
        resp = mutate(admin, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": OTHER_PHONE})
        assert resp.status_code == 200, resp.text
        assert resp.json()["conflict_groups"] == ["G200"]
        todos = tmp_db.list_phone_conflicts(GROUP)
        assert len(todos) == 1 and todos[0]["kind"] == "rename"
        assert "G200" in todos[0]["detail"]
