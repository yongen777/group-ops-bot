# -*- coding: utf-8 -*-
"""量子机器人实例测试（v6，D3-B1：迁移 + CRUD + 后台绑定 API）。

覆盖 D3-B0 设计文档的验收判据：
- 迁移：新库直建 v6（叠加 v7 h5_upload）/ v5 旧库非破坏性升级
  （行内容逐字节不变）/ 重复打开幂等
- 状态机：T0 发现（幂等）-> T2 绑定（409/400 守卫）
  -> T3 禁用 -> T4 启用（url 守卫）；三门纪律（实例状态 ≠ 群授权 ≠ 业务开关）
- ⛔ 2026-10-17 用户指令一：群内「绑定: 备注」命名命令已移除
  （改名仅存管理后台接口）；命令解析边界见 test_commands.py
- 回调端到端：T0 自动发现挂钩（ensure 失败不影响主流程）
- 后台 API：六端点 + 掩码载荷（webhook_key_masked 固定 ****）+
  审计脱敏（webhook_key ⛔ 不进 detail）+ V1 fail-closed 拒收 key

⛔ 这是本地自测证据，不构成真实平台回调验证（§7.2 / D10 / D11）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import commands
from app.config import BotConfig
from app.state import StateStore
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, OTHER_PHONE, PHONE, TZ

from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

NOW = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)
INST_GROUP = "G900"          # 实例验收群（⛔ 非真实群）
ADMIN_PHONE = PHONE         # 绑定管理员 = 测试用户手机号

# v5 旧库最小骨架（仅保留迁移断言需要的表 + 数据；v5->v6 不 ALTER 任何表）
V5_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_config (
    group_id TEXT PRIMARY KEY, group_name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    biz_switches TEXT NOT NULL DEFAULT '{}',
    period_type TEXT NOT NULL DEFAULT 'daily',
    daily_cutoff TEXT NOT NULL DEFAULT '19:00',
    checkin_deadlines TEXT NOT NULL DEFAULT '{"am":"09:00","pm":"14:50"}',
    checkin_require_image INTEGER NOT NULL DEFAULT 1,
    reminder_rules TEXT NOT NULL DEFAULT '{}',
    bind_admins TEXT, created_at TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS group_seen (
    group_id TEXT PRIMARY KEY, last_phone TEXT, last_name TEXT,
    msg_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS security_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT NOT NULL,
    group_id TEXT, phone TEXT, detail TEXT NOT NULL, created_at TEXT NOT NULL);
"""


def _dump_rows(conn, table):
    return [tuple(r) for r in conn.execute(
        "SELECT * FROM %s ORDER BY rowid" % table).fetchall()]


def _make_v5_db(path):
    conn = sqlite3.connect(str(path))
    conn.executescript(V5_SCHEMA)
    conn.execute("INSERT INTO meta(key, value) VALUES('schema_version', '5')")
    conn.execute(
        "INSERT INTO group_config(group_id, group_name, biz_switches,"
        " updated_at) VALUES(?, ?, ?, ?)",
        (GROUP, "旧库群", '{"checkin": true}', "2026-10-14 10:00:00+08:00"))
    conn.execute(
        "INSERT INTO group_seen(group_id, last_phone, msg_count,"
        " first_seen, last_seen) VALUES(?, ?, 3, ?, ?)",
        (INST_GROUP, PHONE, "2026-10-14 09:00:00+08:00",
         "2026-10-14 09:30:00+08:00"))
    conn.execute(
        "INSERT INTO security_event(event_type, group_id, phone, detail,"
        " created_at) VALUES('unauthorized_group', ?, ?, 'x', ?)",
        (OTHER_PHONE and "G950", OTHER_PHONE, "2026-10-14 09:31:00+08:00"))
    conn.commit()
    conn.close()


def _bind_config(data_dir) -> BotConfig:
    return BotConfig(
        callback_port=18002, admin_port=18003,
        external_base_url="http://127.0.0.1:18002",
        tz_name="Asia/Shanghai", data_dir=str(data_dir),
        daily_cutoff=(19, 0), mcp=None)


def _callback_app(tmp_db):
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=FakeBackend(), sender=sender,
                     config=_bind_config(Path(tmp_db.db_path).parent),
                     now_fn=lambda: NOW)
    return TestClient(app), sender


def _post_callback(client, group_id, text, phone=PHONE, payload_extra=None):
    body = {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/x"}
    # payload_extra：补平台真实字段（如 groupName，2026-10-15 实测载荷形态）
    if payload_extra:
        body.update(payload_extra)
    return client.post("/api/callback", json=body)


def _ready_admin_client(tmp_db, tmp_path, monkeypatch):
    """后台客户端（已过强制改密门；实例端点有 _require_password_changed）。"""
    bootstrap(tmp_db, monkeypatch)
    client = admin_client(tmp_db, tmp_path)
    assert login(client).status_code == 200
    mutate(client, "POST", "/api/admin/change-password",
           {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert login(client, password=NEW_PASS).status_code == 200
    return client


# ---------- 1. 迁移（v5 -> v6 非破坏性） ----------

class TestMigrationV5ToV6:
    def test_new_db_builds_v10(self, tmp_db):
        conn = tmp_db._conn
        version = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        # v6 实例表 + v7 h5_upload（§4.11.4 D4 定夺）
        # + v8 白名单单权化（2026-10-19）
        # + v9 reminder_plan.source（2026-10-20）
        # + v10 report_field_seq（2026-10-20 字段可真删 -> id 不复用）
        assert version == "10"
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(qmx_robot_instance)")}
        assert cols == {"group_id", "display_name", "webhook_base_url",
                        "webhook_key_encrypted", "status", "created_at",
                        "updated_at"}
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM qmx_robot_instance").fetchone()["n"] == 0
        ucols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(h5_upload)")}
        assert "ref" in ucols and "token_id" in ucols and "rel_path" in ucols
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM h5_upload").fetchone()["n"] == 0

    def test_v5_db_upgrades_non_destructive(self, tmp_path):
        db = tmp_path / "v5.db"
        _make_v5_db(db)
        before = sqlite3.connect(str(db))
        snap = {t: _dump_rows(before, t)
                for t in ("group_config", "group_seen", "security_event")}
        before.close()

        store = StateStore(db)
        try:
            conn = store._conn
            assert conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"] == "10"
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "qmx_robot_instance" in tables
            # v7：h5_upload 上传凭证表（非破坏性新增；旧库升级后为空行）
            assert "h5_upload" in tables
            # 旧库数据逐字节不变（非破坏性）
            for table, rows in snap.items():
                assert _dump_rows(conn, table) == rows, table
            # 新表为空行（实例靠 T0 自然发现，⛔ 不预建）
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM qmx_robot_instance"
            ).fetchone()["n"] == 0
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM h5_upload").fetchone()["n"] == 0
        finally:
            store.close()

    def test_reopen_is_idempotent(self, tmp_path):
        db = tmp_path / "v5b.db"
        _make_v5_db(db)
        store = StateStore(db)
        store.ensure_robot_instance(INST_GROUP)
        store.close()
        # 重复打开：迁移早退，不重复执行、状态不丢
        store2 = StateStore(db)
        try:
            row = store2.get_robot_instance(INST_GROUP)
            assert row is not None and row["status"] == "WAIT_BIND"
        finally:
            store2.close()


# ---------- 2. 状态机 CRUD（state 层） ----------

class TestRobotInstanceCrud:
    def test_t0_ensure_is_idempotent(self, tmp_db):
        tmp_db.ensure_robot_instance(INST_GROUP)
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["status"] == "WAIT_BIND"
        assert row["display_name"] is None
        assert row["webhook_base_url"] is None
        first_seen = row["created_at"]
        tmp_db.ensure_robot_instance(INST_GROUP)  # 重复 -> 零副作用
        row2 = tmp_db.get_robot_instance(INST_GROUP)
        assert row2["created_at"] == first_seen
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM qmx_robot_instance").fetchone()["n"] == 1

    def test_list_with_status_filter(self, tmp_db):
        tmp_db.ensure_robot_instance("G901")
        tmp_db.ensure_robot_instance("G902")
        assert len(tmp_db.list_robot_instances()) == 2
        assert len(tmp_db.list_robot_instances("WAIT_BIND")) == 2
        assert tmp_db.list_robot_instances("ACTIVE") == []
        assert tmp_db.bind_robot_instance(
            "G901", "http://127.0.0.1:18099/hook")
        assert len(tmp_db.list_robot_instances("ACTIVE")) == 1
        assert len(tmp_db.list_robot_instances("WAIT_BIND")) == 1

    def test_t2_bind_guards(self, tmp_db):
        tmp_db.ensure_robot_instance(INST_GROUP)
        # 非法 url -> ValueError（API 转 400，fail-closed 拒写）
        for bad in ("", "ftp://x", "http://u:p@host/hook", "   ",
                    "not-a-url", "http://"):
            with pytest.raises(ValueError):
                tmp_db.bind_robot_instance(INST_GROUP, bad)
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["status"] == "WAIT_BIND" and row["webhook_base_url"] is None
        # 正常绑定 -> ACTIVE；再绑 -> False（409 语义）
        assert tmp_db.bind_robot_instance(
            INST_GROUP, "http://127.0.0.1:18099/hook/")
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["status"] == "ACTIVE"
        assert row["webhook_base_url"] == "http://127.0.0.1:18099/hook"
        assert tmp_db.bind_robot_instance(
            INST_GROUP, "http://127.0.0.1:18099/other") is False
        assert tmp_db.get_robot_instance(INST_GROUP)[
            "webhook_base_url"] == "http://127.0.0.1:18099/hook"
        # 绑定不带备注 -> 原显示名不被覆盖（COALESCE）
        assert tmp_db.bind_robot_instance(
            "G903", "http://127.0.0.1:18099/hook") is False  # 无记录
        tmp_db.ensure_robot_instance("G903")
        assert tmp_db.bind_robot_instance(
            "G903", "http://127.0.0.1:18099/hook", display_name="备群")
        assert tmp_db.get_robot_instance("G903")["display_name"] == "备群"
        assert tmp_db.bind_robot_instance("G903", "http://h/x") is False

    def test_update_partial_and_no_clear_url(self, tmp_db):
        tmp_db.ensure_robot_instance(INST_GROUP)
        assert tmp_db.update_robot_instance(
            INST_GROUP, display_name="先命名") is True
        # 只改名不动 url（url 仍空）
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["display_name"] == "先命名" and row["webhook_base_url"] is None
        assert tmp_db.update_robot_instance(
            INST_GROUP, webhook_base_url="https://bot.example.com/hook") \
            is True
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["webhook_base_url"] == "https://bot.example.com/hook"
        assert row["display_name"] == "先命名"  # ⛔ 未被清
        # 清空 url -> ValueError（⛔ 禁止；设计 §3.1）
        with pytest.raises(ValueError):
            tmp_db.update_robot_instance(INST_GROUP, webhook_base_url="")
        with pytest.raises(ValueError):
            tmp_db.update_robot_instance(INST_GROUP, display_name="")
        # 无记录 -> False（API 转 404）
        assert tmp_db.update_robot_instance("G999", display_name="x") is False

    def test_status_transitions_t3_t4(self, tmp_db):
        tmp_db.ensure_robot_instance(INST_GROUP)
        # T4 无 url -> False（API 转 400）
        assert tmp_db.set_robot_instance_status(INST_GROUP, "ACTIVE") is False
        assert tmp_db.get_robot_instance(INST_GROUP)["status"] == "WAIT_BIND"
        assert tmp_db.bind_robot_instance(
            INST_GROUP, "https://bot.example.com/hook")
        # T3 禁用（幂等：重复禁用仍 True）
        assert tmp_db.set_robot_instance_status(
            INST_GROUP, "DISABLED") is True
        assert tmp_db.set_robot_instance_status(
            INST_GROUP, "DISABLED") is True
        assert tmp_db.get_robot_instance(INST_GROUP)["status"] == "DISABLED"
        # T4 启用（url 已在）-> ACTIVE；已 ACTIVE 再启用 = 幂等 True
        assert tmp_db.set_robot_instance_status(INST_GROUP, "ACTIVE") is True
        assert tmp_db.set_robot_instance_status(INST_GROUP, "ACTIVE") is True
        # 无记录 -> False；WAIT_BIND 不对外设置
        assert tmp_db.set_robot_instance_status("G999", "DISABLED") is False
        with pytest.raises(ValueError):
            tmp_db.set_robot_instance_status(INST_GROUP, "WAIT_BIND")
        with pytest.raises(ValueError):
            tmp_db.set_robot_instance_status(INST_GROUP, "BOGUS")

    def test_update_renames_without_status_change(self, tmp_db):
        """后台改名（display_name）经 update_robot_instance，⛔ 不改 status。

        2026-10-17 用户指令一移除群内「绑定: 备注」命名命令后，改名只剩
        后台这一条路（API PUT /api/admin/robot-instances/{gid}）。
        """
        tmp_db.ensure_robot_instance(INST_GROUP)
        assert tmp_db.update_robot_instance(
            INST_GROUP, display_name="华东巡检群") is True
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["display_name"] == "华东巡检群"
        assert row["status"] == "WAIT_BIND"
        # 空备注 -> ValueError（fail-closed）
        with pytest.raises(ValueError):
            tmp_db.update_robot_instance(INST_GROUP, display_name="")


# ---------- 3. 命令解析边界（2026-10-17：「绑定:」已移除） ----------

class TestBindColonFormRemovedAtInstance:
    """「绑定: 备注」命名命令移除后的解析边界（详细锁定见 test_commands.py）。"""

    def test_plain_bind_unchanged(self):
        # 「绑定」= 两轮第 1 轮的触发词；⛔ 带任何变体都是无关文本
        r = commands.parse("绑定")
        assert (r.result, r.command) == (commands.RESULT_DISPATCH,
                                         commands.CMD_BIND)
        r = commands.parse("绑定 abc")
        assert (r.result, r.command) == (commands.RESULT_UNKNOWN, None)

    def test_bind_colon_forms_are_unknown(self):
        # ⛔ 一步到位路径已移除：各种「绑定: X」形态一律无关文本
        for text in ("绑定: 华东巡检群", "绑定：华东巡检群", "绑定: 打卡",
                     "绑定: A群 打卡", "绑定:", "绑定：",
                     "绑定: " + "x" * 65, "  @机器人 绑定:  巡检A组  "):
            r = commands.parse(text)
            assert (r.result, r.command) == (commands.RESULT_UNKNOWN, None), text


# ---------- 4. 回调端到端（T0 挂钩 + 命名命令） ----------

class TestCallbackAutoDiscoveryAndNaming:
    def test_t0_ensure_on_unknown_group(self, tmp_db):
        client, sender = _callback_app(tmp_db)
        resp = _post_callback(client, INST_GROUP, "帮助")
        assert resp.status_code == 200
        # ⭐ 2026-10-19：未绑定群回统一引导（⛔ 不再静默）
        assert resp.json()["replied"] is True
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        # T0 已登记 WAIT_BIND（group_seen 之外新增实例行）
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row is not None and row["status"] == "WAIT_BIND"

    def test_ensure_failure_does_not_break_callback(self, tmp_db,
                                                    monkeypatch):
        def _boom(_gid):
            raise RuntimeError("db down")
        monkeypatch.setattr(tmp_db, "ensure_robot_instance", _boom)
        client, _ = _callback_app(tmp_db)
        # ensure 记账失败 -> 主流程不受影响（静默压制路径照旧）
        resp = _post_callback(client, INST_GROUP, "帮助")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_bind_plain_uses_round_two_group_name(self, tmp_db, monkeypatch):
        """纯「绑定」= 两轮（第 1 轮只提示）；群名取**第 2 轮用户输入**。"""
        client, sender = _callback_app(tmp_db)
        resp = _post_callback(client, INST_GROUP, "绑定",
                              payload_extra={"groupName": "量子密信群机器人开发沟通群"})
        assert resp.status_code == 200
        assert "请输入要绑定的群名称" in sender.sent[-1]["text"]
        # ⭐ 第 1 轮不建群
        assert tmp_db.get_group_config(INST_GROUP) is None

        resp = _post_callback(client, INST_GROUP, "我的群名")
        assert resp.status_code == 200
        cfg = tmp_db.get_group_config(INST_GROUP)
        assert cfg is not None
        assert cfg["group_name"] == "我的群名"

    def test_bind_colon_form_in_unbound_group_gets_guide(self, tmp_db):
        """⭐ 2026-10-17：「绑定: 群名」= 无关文本（⛔ 不建群）。

        ⭐ 2026-10-19：该文本在未绑定群 -> 回统一引导话术（⛔ 不再静默）；
        仍不建群、不产生命名事件。
        """
        client, sender = _callback_app(tmp_db)
        resp = _post_callback(client, INST_GROUP, "绑定: 华东巡检群")
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        assert tmp_db.get_group_config(INST_GROUP) is None
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["display_name"] is None
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event"
            " WHERE event_type LIKE 'robot_instance_named%'").fetchall()
        assert len(events) == 0

    def test_three_gates_are_independent(self, tmp_db):
        """三门纪律：实例 ACTIVE ≠ 群授权；实例 DISABLED ⛔ 不阻断 reply。"""
        tmp_db.ensure_robot_instance(INST_GROUP)
        tmp_db.bind_robot_instance(INST_GROUP, "https://bot.example.com/hook")
        client, sender = _callback_app(tmp_db)
        # 实例 ACTIVE 但群未授权（无 group_config）-> 回未绑定引导（⛔ 不跑业务）
        resp = _post_callback(client, INST_GROUP, "打卡")
        assert resp.json()["replied"] is True
        assert "本群尚未接入机器人" in sender.sent[-1]["text"]
        # 群授权 + 业务开 + 实例 DISABLED -> 业务照常处理、回推照常
        tmp_db.create_group(INST_GROUP, "授权群")
        tmp_db.upsert_group_config(
            INST_GROUP,
            biz_switches={k: True for k in tmp_db.BIZ_SWITCH_KEYS})
        tmp_db.set_robot_instance_status(INST_GROUP, "DISABLED")
        resp = _post_callback(client, INST_GROUP, "帮助")
        assert resp.status_code == 200
        assert resp.json()["replied"] is True  # 未知命令帮助文案照回
        assert sender.sent[-1]["text"] == commands.HELP_TEXT


# ---------- 5. 管理后台 API（六端点 + 掩码 + 审计脱敏） ----------

class TestAdminRobotInstanceApi:
    def _client(self, tmp_db, tmp_path, monkeypatch):
        return _ready_admin_client(tmp_db, tmp_path, monkeypatch)

    def test_list_and_detail(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch)
        tmp_db.ensure_robot_instance(INST_GROUP)
        resp = client.get("/api/admin/robot-instances")
        assert resp.status_code == 200
        items = resp.json()["instances"]
        assert len(items) == 1 and items[0]["status"] == "WAIT_BIND"
        # 掩码载荷：⛔ 无密文列；未设置 key -> masked=None / set=False
        assert "webhook_key_encrypted" not in items[0]
        assert items[0]["webhook_key_set"] is False
        assert items[0]["webhook_key_masked"] is None
        # 状态过滤 + 非法过滤
        assert client.get(
            "/api/admin/robot-instances?status=WAIT_BIND").status_code == 200
        assert client.get(
            "/api/admin/robot-instances?status=BOGUS").status_code == 400
        # 详情 404 / 200
        assert client.get(
            "/api/admin/robot-instances/G999").status_code == 404
        assert client.get(
            "/api/admin/robot-instances/%s" % INST_GROUP).status_code == 200

    def test_bind_success_and_masking(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch)
        tmp_db.ensure_robot_instance(INST_GROUP)
        resp = mutate(client, "POST",
                      "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                      {"webhook_base_url": "https://bot.example.com/hook/",
                       "display_name": "华东巡检群"})
        assert resp.status_code == 200, resp.text
        inst = resp.json()["instance"]
        assert inst["status"] == "ACTIVE"
        assert inst["webhook_base_url"] == "https://bot.example.com/hook"
        assert inst["display_name"] == "华东巡检群"
        assert inst["webhook_key_set"] is False
        assert "webhook_key_encrypted" not in inst
        # 审计留痕 + ⛔ 不含任何 key 字段
        rows = tmp_db._conn.execute(
            "SELECT * FROM admin_audit WHERE action='robot_instance.bind'"
        ).fetchall()
        assert len(rows) == 1
        assert "webhook_key" not in (rows[0]["detail"] or "")

    def test_bind_guards(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch)
        tmp_db.ensure_robot_instance(INST_GROUP)
        # 404 无记录
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/G999/bind",
                      {"webhook_base_url": "https://x.example.com/h"}
                      ).status_code == 404
        # 400 非法 url
        for bad in ("", "ftp://x", "http://u:p@h/x"):
            assert mutate(client, "POST",
                          "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                          {"webhook_base_url": bad}).status_code == 400
        # 400 fail-closed 拒收 webhook_key（V1：平台签名机制未确认）
        resp = mutate(client, "POST",
                      "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                      {"webhook_base_url": "https://x.example.com/h",
                       "webhook_key": "sk-should-not-land"})
        assert resp.status_code == 400
        row = tmp_db.get_robot_instance(INST_GROUP)
        assert row["webhook_key_encrypted"] is None  # ⛔ 零落库
        assert row["status"] == "WAIT_BIND"
        # 正常绑定后 409 重绑
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                      {"webhook_base_url": "https://x.example.com/h"}
                      ).status_code == 200
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                      {"webhook_base_url": "https://x.example.com/h2"}
                      ).status_code == 409

    def test_update_disable_enable(self, tmp_db, tmp_path, monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch)
        tmp_db.ensure_robot_instance(INST_GROUP)
        # PUT 404
        assert mutate(client, "PUT",
                      "/api/admin/robot-instances/G999",
                      {"display_name": "x"}).status_code == 404
        # 先命名后绑 url
        assert mutate(client, "PUT",
                      "/api/admin/robot-instances/%s" % INST_GROUP,
                      {"display_name": "先命名"}).status_code == 200
        # 清空 url -> 400；拒收 key -> 400
        assert mutate(client, "PUT",
                      "/api/admin/robot-instances/%s" % INST_GROUP,
                      {"webhook_base_url": ""}).status_code == 400
        assert mutate(client, "PUT",
                      "/api/admin/robot-instances/%s" % INST_GROUP,
                      {"webhook_key": "sk-x"}).status_code == 400
        # enable 无 url -> 400
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/enable" % INST_GROUP
                      ).status_code == 400
        # bind -> disable -> enable 闭环
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/bind" % INST_GROUP,
                      {"webhook_base_url": "https://x.example.com/h"}
                      ).status_code == 200
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/disable" % INST_GROUP
                      ).status_code == 200
        assert tmp_db.get_robot_instance(INST_GROUP)["status"] == "DISABLED"
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/enable" % INST_GROUP
                      ).status_code == 200
        assert tmp_db.get_robot_instance(INST_GROUP)["status"] == "ACTIVE"
        # 幂等：再 enable 仍 200
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/%s/enable" % INST_GROUP
                      ).status_code == 200
        # disable 404
        assert mutate(client, "POST",
                      "/api/admin/robot-instances/G999/disable"
                      ).status_code == 404

    def test_mutations_require_csrf_and_password(self, tmp_db, tmp_path,
                                                 monkeypatch):
        client = self._client(tmp_db, tmp_path, monkeypatch)
        tmp_db.ensure_robot_instance(INST_GROUP)
        # 无 CSRF -> 403
        resp = client.post(
            "/api/admin/robot-instances/%s/disable" % INST_GROUP)
        assert resp.status_code == 403
        # 未登录 -> 401
        anon = admin_client(tmp_db, tmp_path)
        assert anon.get("/api/admin/robot-instances").status_code == 401
