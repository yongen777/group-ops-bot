# -*- coding: utf-8 -*-
"""D30 回归（2026-10-20）：webhook 两项加固 + 后台建号 env 引导移除。

覆盖：
1. schema v11 —— 实例表新增 ``last_callback_url`` / ``last_callback_at``，
   新库直接到 v11，旧库（v10）由迁移补列
2. ``record_callback_url`` —— 只吃合法 http(s) 地址，⛔ 不改状态/⛔ 不写
   webhook_base_url（只是候选值），异常输入不抛错
3. ``robot_push_ready`` —— 唯一「能主动推送」判据（ACTIVE + 地址非空）
4. 入站回调真的把 reply_url 记下来（集成路径，不是只测 store）
5. 加固一：未绑定的群 ⛔ 不许把「定时提醒 / 数据导出」打开（后端 400 兜底），
   已绑定可开；⛔ 不拦其它字段保存
6. 加固后仍能离线建号：``python -m app.admin.create_admin`` 的退出码与规则
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.admin import auth as admin_auth
from app.admin import create_admin as cli_mod
from app.admin.server import create_admin_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import make_config
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

GID = "2100060349044391937"
HOOK = "http://reply.test/hook"
REAL_HOOK = "https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=abc123"


def callback_payload(text="打卡", group_id=GID, phone="17313771297"):
    return {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": REAL_HOOK, "name": "测试用户", "user_id": "puid-1"}


def admin_ready(tmp_db):
    """建号 -> 登录 -> 过强制改密门（⛔ 不过门其它端点一律 403）。"""
    bootstrap(tmp_db, None)
    client = admin_client(tmp_db, Path(tmp_db.db_path).parent)
    assert login(client).status_code == 200
    resp = mutate(client, "POST", "/api/admin/change-password",
                  {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert resp.status_code == 200
    assert login(client, password=NEW_PASS).status_code == 200
    return client


def seed_group(tmp_db, bind=False):
    """建群（五开关全关）+ 实例行；bind=True 时绑定为 ACTIVE。"""
    tmp_db.create_group(GID, "项目群聊机器人测试",
                        switches={k: False for k in tmp_db.BIZ_SWITCH_KEYS})
    tmp_db.ensure_robot_instance(GID)
    if bind:
        assert tmp_db.bind_robot_instance(GID, REAL_HOOK, display_name="测试群")


# ---------- 1. schema v11 ----------

class TestMigrationV11:
    def test_new_db_is_v11(self, tmp_db):
        version = tmp_db._conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()["value"]
        assert version == "13"
        cols = {r["name"] for r in tmp_db._conn.execute(
            "PRAGMA table_info(qmx_robot_instance)")}
        assert {"last_callback_url", "last_callback_at"} <= cols

    def test_v10_db_gains_callback_columns(self, tmp_path):
        """旧库（v10，实例表没有两列）打开后自动补列且版本到 11。"""
        import sqlite3

        from app.state import StateStore

        db = tmp_path / "v10.db"
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            "INSERT INTO meta VALUES('schema_version','10');"
            "CREATE TABLE qmx_robot_instance("
            " group_id TEXT PRIMARY KEY, display_name TEXT,"
            " webhook_base_url TEXT, webhook_key_encrypted TEXT,"
            " status TEXT NOT NULL DEFAULT 'WAIT_BIND',"
            " created_at TEXT, updated_at TEXT NOT NULL);")
        conn.commit()
        conn.close()

        store = StateStore(db)
        try:
            cols = {r["name"] for r in store._conn.execute(
                "PRAGMA table_info(qmx_robot_instance)")}
            assert {"last_callback_url", "last_callback_at"} <= cols
            assert store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"] == "13"
        finally:
            store.close()


# ---------- 2. record_callback_url ----------

class TestRecordCallbackUrl:
    def test_records_without_binding(self, tmp_db):
        seed_group(tmp_db)
        assert tmp_db.record_callback_url(GID, REAL_HOOK) is True
        row = tmp_db.get_robot_instance(GID)
        assert row["last_callback_url"] == REAL_HOOK
        assert row["last_callback_at"]
        # ⛔ 只是候选值：状态与真正的推送地址都不许被改
        assert row["status"] == "WAIT_BIND"
        assert row["webhook_base_url"] is None

    def test_overwrites_with_latest(self, tmp_db):
        seed_group(tmp_db)
        tmp_db.record_callback_url(GID, REAL_HOOK)
        newer = REAL_HOOK.replace("abc123", "xyz789")
        assert tmp_db.record_callback_url(GID, newer) is True
        assert tmp_db.get_robot_instance(GID)["last_callback_url"] == newer

    @pytest.mark.parametrize("bad", [
        "", "   ", "ftp://host/hook", "not-a-url", "//host/hook",
        "http://user:pw@host/hook", "http://" + "a" * 600,
    ])
    def test_rejects_dirty_values_silently(self, tmp_db, bad):
        seed_group(tmp_db)
        assert tmp_db.record_callback_url(GID, bad) is False
        assert tmp_db.get_robot_instance(GID)["last_callback_url"] is None

    def test_unknown_group_returns_false(self, tmp_db):
        assert tmp_db.record_callback_url("no-such-group", REAL_HOOK) is False


# ---------- 3. push_ready ----------

class TestPushReady:
    def test_no_instance(self, tmp_db):
        assert tmp_db.robot_push_ready(GID) is False

    def test_wait_bind_with_url(self, tmp_db):
        """WAIT_BIND 即便库里已经有地址也不算就绪（状态才是开关）。"""
        seed_group(tmp_db)
        tmp_db._conn.execute(
            "UPDATE qmx_robot_instance SET webhook_base_url=? WHERE group_id=?",
            (REAL_HOOK, GID))
        tmp_db._conn.commit()
        assert tmp_db.robot_push_ready(GID) is False

    def test_active_with_url(self, tmp_db):
        seed_group(tmp_db, bind=True)
        assert tmp_db.robot_push_ready(GID) is True

    def test_disabled(self, tmp_db):
        seed_group(tmp_db, bind=True)
        tmp_db.set_robot_instance_status(GID, "DISABLED")
        assert tmp_db.robot_push_ready(GID) is False


# ---------- 4. 入站回调真的记账（集成路径） ----------

class TestInboundRecordsCallbackUrl:
    def _client(self, tmp_db):
        from app.web.server import create_app
        sender = RecordingSender()
        app = create_app(state=tmp_db, backend=FakeBackend(),
                         sender=sender,
                         config=make_config(Path(tmp_db.db_path).parent))
        return TestClient(app), sender

    def test_callback_stores_reply_url(self, tmp_db):
        client, _ = self._client(tmp_db)
        resp = client.post("/api/callback", json=callback_payload())
        assert resp.status_code == 200
        row = tmp_db.get_robot_instance(GID)
        assert row is not None and row["status"] == "WAIT_BIND"
        assert row["last_callback_url"] == REAL_HOOK
        assert row["last_callback_at"]

    def test_payload_exposes_candidate_but_not_binding(self, tmp_db):
        client, _ = self._client(tmp_db)
        client.post("/api/callback", json=callback_payload())
        admin = admin_ready(tmp_db)
        data = admin.get(f"/api/admin/robot-instances/{GID}").json()["instance"]
        assert data["last_callback_url"] == REAL_HOOK
        assert data["push_ready"] is False
        assert data["webhook_base_url"] is None
        assert data["status"] == "WAIT_BIND"


# ---------- 5. 加固一：未绑定不许开「定时提醒 / 数据导出」 ----------

class TestSwitchGuard:
    def test_reminder_rejected_when_not_ready(self, tmp_db):
        seed_group(tmp_db)
        client = admin_ready(tmp_db)
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"switches": {"reminder": True}})
        assert resp.status_code == 400
        assert "推送地址" in resp.json()["detail"]
        # 库里必须一点没动
        assert tmp_db.normalized_switches(
            tmp_db.get_group_config(GID))["reminder"] is False

    def test_statistics_rejected_when_not_ready(self, tmp_db):
        seed_group(tmp_db)
        client = admin_ready(tmp_db)
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"switches": {"statistics": True}})
        assert resp.status_code == 400

    def test_passive_switches_unaffected(self, tmp_db):
        """打卡/总结/报数走被动回复，未绑定也能开。"""
        seed_group(tmp_db)
        client = admin_ready(tmp_db)
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"switches": {"checkin": True, "summary": True,
                                    "report": True}})
        assert resp.status_code == 200
        switches = tmp_db.normalized_switches(tmp_db.get_group_config(GID))
        assert switches["checkin"] and switches["summary"] and switches["report"]

    def test_allowed_after_bind(self, tmp_db):
        seed_group(tmp_db, bind=True)
        client = admin_ready(tmp_db)
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"switches": {"reminder": True, "statistics": True}})
        assert resp.status_code == 200
        switches = tmp_db.normalized_switches(tmp_db.get_group_config(GID))
        assert switches["reminder"] is True and switches["statistics"] is True

    def test_existing_on_state_does_not_block_other_fields(self, tmp_db):
        """历史遗留：开关早已开着但实例没绑 —— ⛔ 不能连别的时间设置都保存不了。"""
        seed_group(tmp_db)
        tmp_db.upsert_group_config(
            GID, biz_switches={"checkin": True, "summary": True,
                               "report": True, "reminder": True,
                               "statistics": True})
        client = admin_ready(tmp_db)
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"daily_cutoff": "18:30"})
        assert resp.status_code == 200
        assert tmp_db.get_group_config(GID)["daily_cutoff"] == "18:30"
        # 同一份开关原样回传（没有 off->on 跃迁）也放行
        resp = mutate(client, "PUT", f"/api/admin/groups/{GID}/config",
                      {"switches": {"reminder": True}})
        assert resp.status_code == 200

    def test_config_payload_carries_push_ready(self, tmp_db):
        seed_group(tmp_db)
        client = admin_ready(tmp_db)
        config = client.get(f"/api/admin/groups/{GID}/config").json()["config"]
        assert config["push_ready"] is False
        tmp_db.bind_robot_instance(GID, REAL_HOOK)
        config = client.get(f"/api/admin/groups/{GID}/config").json()["config"]
        assert config["push_ready"] is True


# ---------- 6. 建号：env 引导移除后的唯一入口 ----------

class TestCreateAdminCli:
    def test_missing_db_exits_3(self, tmp_path, capsys):
        code = cli_mod.main(["--username", "ops",
                             "--db", str(tmp_path / "nope.db")])
        assert code == 3
        assert not (tmp_path / "nope.db").exists()   # ⛔ 不静默新建

    def test_creates_admin(self, tmp_db):
        code = cli_mod.main(["--username", "ops", "--password", "Aa12345678",
                             "--db", tmp_db.db_path])
        assert code == 0
        row = tmp_db.get_admin_user("ops")
        assert row is not None
        assert row["must_change_password"] == 0
        assert "Aa12345678" not in row["password_hash"]

    def test_must_change_flag(self, tmp_db):
        assert cli_mod.main(["--username", "ops", "--password", "Aa12345678",
                             "--db", tmp_db.db_path, "--must-change"]) == 0
        assert tmp_db.get_admin_user("ops")["must_change_password"] == 1

    def test_duplicate_and_weak_rejected(self, tmp_db):
        assert cli_mod.main(["--username", "ops", "--password", "Aa12345678",
                             "--db", tmp_db.db_path]) == 0
        assert cli_mod.main(["--username", "ops", "--password", "Aa12345678",
                             "--db", tmp_db.db_path]) == 2
        assert cli_mod.main(["--username", "ops2", "--password", "short",
                             "--db", tmp_db.db_path]) == 2
        assert tmp_db.get_admin_user("ops2") is None

    def test_auth_module_has_no_env_bootstrap(self):
        """⭐ 守卫：auth 模块 ⛔ 不得再出现任何 env 建号路径。"""
        import inspect
        src = inspect.getsource(admin_auth)
        assert "GOPS_ADMIN_USERNAME" not in src
        assert "GOPS_ADMIN_PASSWORD" not in src
        assert not hasattr(admin_auth, "bootstrap_admin")
