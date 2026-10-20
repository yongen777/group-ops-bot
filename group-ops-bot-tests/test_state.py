# -*- coding: utf-8 -*-
"""SQLite 状态层测试（范围键判定 / TTL / 恢复任务 / 字段池）。"""

from datetime import datetime, timedelta

from app import state as state_mod
from app.period import TZ

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
GROUP = "G100"
PHONE = "22644212509"


class TestTokenStore:
    def test_put_get_consume(self, tmp_db):
        from app.tokens import generate_token_value, new_record, token_id_of
        value = generate_token_value()
        record = new_record(value, PHONE, GROUP, "report", "2026-10-13", None, now=NOW)
        tmp_db.put_token(record)
        row = tmp_db.get_token(record.token_id)
        assert row is not None
        assert row["state"] == "active"
        # ⛔ 库内不存明文令牌
        assert row["token_id"] == token_id_of(value)
        assert value not in dict(row)
        tmp_db.consume_token(record.token_id, "req-1")
        row = tmp_db.get_token(record.token_id)
        assert row["state"] == "consumed"
        assert row["consumed_request_id"] == "req-1"


class TestScopeQuota:
    """额度判定必须走业务记录范围键，⛔ 不靠请求幂等键（§2.5.0 / §2.5.3）。"""

    def test_pending_intent_is_not_success(self, tmp_db):
        tmp_db.put_intent("req-1", "S1", "report", GROUP, PHONE, "2026-10-13",
                          None, {"items": []}, status="pending")
        assert tmp_db.has_successful_scope("S1") is False

    def test_confirmed_intent_counts_as_success(self, tmp_db):
        tmp_db.put_intent("req-1", "S1", "report", GROUP, PHONE, "2026-10-13",
                          None, {"items": []}, status="pending")
        tmp_db.update_intent_status("req-1", "confirmed")
        assert tmp_db.has_successful_scope("S1") is True

    def test_receipt_counts_as_success(self, tmp_db):
        tmp_db.put_receipt("req-9", "S2", "checkin", "rec-9")
        assert tmp_db.has_successful_scope("S2") is True

    def test_different_scope_independent(self, tmp_db):
        # 打卡双时段：上午成功不影响下午（§2.3 两个时段各自独立计数）
        tmp_db.put_intent("r1", "G|U|checkin|2026-10-13|am", "checkin", GROUP,
                          PHONE, "2026-10-13", "am", {}, status="confirmed")
        assert tmp_db.has_successful_scope("G|U|checkin|2026-10-13|am") is True
        assert tmp_db.has_successful_scope("G|U|checkin|2026-10-13|pm") is False

    def test_new_request_same_scope_still_blocked(self, tmp_db):
        # B 后修正是新请求键，但范围键下已有 confirmed -> 仍判定已成功
        tmp_db.put_intent("r1", "S1", "report", GROUP, PHONE, "2026-10-13",
                          None, {}, status="confirmed")
        tmp_db.put_intent("r2", "S1", "report", GROUP, PHONE, "2026-10-13",
                          None, {}, status="pending")
        assert tmp_db.has_successful_scope("S1") is True


class TestRecoveryTask:
    def test_due_and_resolve(self, tmp_db):
        tmp_db.put_recovery_task("req-c", "S1", "checkin",
                                 NOW + timedelta(minutes=5))
        due = tmp_db.due_recovery_tasks(now=NOW + timedelta(minutes=10))
        assert [r["request_id"] for r in due] == ["req-c"]
        tmp_db.resolve_recovery_task("req-c", "A")
        assert list(tmp_db.due_recovery_tasks(now=NOW + timedelta(hours=1))) == []

    def test_not_due_yet(self, tmp_db):
        tmp_db.put_recovery_task("req-c", "S1", "checkin",
                                 NOW + timedelta(hours=1))
        assert list(tmp_db.due_recovery_tasks(now=NOW)) == []

    def test_naive_datetime_input_normalized(self, tmp_db):
        # naive 入参不得炸 TypeError（_ensure_aware）
        tmp_db.put_recovery_task("req-n", "S1", "checkin",
                                 datetime(2026, 10, 13, 10, 30))  # naive
        due = tmp_db.due_recovery_tasks(now=datetime(2026, 10, 13, 11, 0))
        assert [r["request_id"] for r in due] == ["req-n"]


class TestPendingSession:
    """总结两轮待处理，TTL 30 分钟（§2.2）。"""

    def test_active_within_ttl(self, tmp_db):
        tmp_db.put_pending_session(GROUP, PHONE, "summary", None, NOW)
        row = tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW)
        assert row is not None
        assert row["arg"] is None

    def test_expired_after_30min(self, tmp_db):
        tmp_db.put_pending_session(GROUP, PHONE, "summary", "日报", NOW)
        row = tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW)
        assert row["arg"] == "日报"
        # 30 分钟 TTL：把库内时间改到 NOW 之前 31 分钟，模拟过期（时钟注入，确定性）
        old = (NOW - timedelta(minutes=31)).isoformat(timespec="seconds")
        tmp_db._conn.execute(
            "UPDATE pending_session SET created_at=?, expires_at=? WHERE scope_key=?",
            (old, old, "G100|22644212509|summary"))
        tmp_db._conn.commit()
        assert tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW) is None


class TestFieldPool:
    """报数字段池权威在机器人侧（§2.12 第 4 项 / §4.11.2）。"""

    def test_next_field_id_increments(self, tmp_db):
        assert tmp_db.next_field_id(GROUP) == 1
        tmp_db.upsert_field(GROUP, 1, "bug修复数", "个", ["开发"], True, 1)
        assert tmp_db.next_field_id(GROUP) == 2

    def test_upsert_and_list(self, tmp_db):
        tmp_db.upsert_field(GROUP, 1, "bug修复数", "个", ["开发"], True, 1)
        tmp_db.upsert_field(GROUP, 2, "代码行数", "行", ["开发"], True, 2)
        tmp_db.upsert_field(GROUP, 3, "需求响应数", "个", ["产品"], False, 3)
        rows = tmp_db.list_fields(GROUP)
        assert [r["field_name"] for r in rows] == ["bug修复数", "代码行数"]
        assert tmp_db.get_field(GROUP, 3)["enabled"] == 0

    def test_upsert_updates_existing(self, tmp_db):
        tmp_db.upsert_field(GROUP, 1, "旧名", None, [], True, 1)
        tmp_db.upsert_field(GROUP, 1, "新名", "个", ["开发"], True, 1)
        row = tmp_db.get_field(GROUP, 1)
        assert row["field_name"] == "新名"
        assert row["unit"] == "个"
        assert tmp_db.next_field_id(GROUP) == 2  # 不产生新 id


class TestWhitelist:
    def test_add_remove_check(self, tmp_db):
        assert tmp_db.in_whitelist(GROUP, PHONE) is False
        tmp_db.upsert_whitelist(GROUP, PHONE)
        assert tmp_db.in_whitelist(GROUP, PHONE) is True
        assert tmp_db.has_whitelist_permission(
            GROUP, PHONE, "ADMIN_ALL") is True
        # 单权化：旧业务权限值一律不再命中（⛔ 无分权）
        assert tmp_db.has_whitelist_permission(
            GROUP, PHONE, "EXPORT_CHECKIN") is False
        tmp_db.remove_whitelist(GROUP, PHONE)
        assert tmp_db.in_whitelist(GROUP, PHONE) is False

    def test_reject_unknown_permission(self, tmp_db):
        import pytest
        with pytest.raises(ValueError):
            tmp_db.upsert_whitelist(GROUP, PHONE, "EXPORT_EVERYTHING")


class TestMigrationV7ToV8:
    """v7 库（export_whitelist 三权行）-> v8 合并为单一 ADMIN_ALL。

    2026-10-19 用户指令：⛔ 推翻 v4.3 按业务分权；同一人多行三权
    合并为一行 ADMIN_ALL，updated_at 取 MAX。
    """

    def _seed_v7(self, db, rows):
        import sqlite3
        from app.state import StateStore
        store = StateStore(db)
        store.close()
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE meta SET value='7' WHERE key='schema_version'")
        conn.execute("DELETE FROM export_whitelist WHERE group_id=?", (GROUP,))
        for user, perm, ts in rows:
            conn.execute(
                "INSERT INTO export_whitelist(group_id, user_id,"
                " permission_type, updated_at) VALUES(?,?,?,?)",
                (GROUP, user, perm, ts))
        conn.commit()
        conn.close()

    def test_three_perms_merge_to_admin_all(self, tmp_path):
        from app.state import StateStore
        db = tmp_path / "v7.db"
        self._seed_v7(db, [
            (PHONE, "EXPORT_CHECKIN", "2026-10-15 16:35:00+08:00"),
            (PHONE, "EXPORT_SUMMARY", "2026-10-16 09:00:00+08:00"),
            (PHONE, "EXPORT_REPORT", "2026-10-15 16:35:00+08:00"),
        ])
        store = StateStore(db)
        try:
            conn = store._conn
            rows = conn.execute(
                "SELECT user_id, permission_type, updated_at"
                " FROM export_whitelist WHERE group_id=?", (GROUP,)).fetchall()
            assert [(r["user_id"], r["permission_type"]) for r in rows] == \
                [(PHONE, "ADMIN_ALL")]
            assert rows[0]["updated_at"] == "2026-10-16 09:00:00+08:00"
            assert conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"] == "11"   # v8/v9/v10 之后还有 v11（实例回调地址列）
            assert store.has_whitelist_permission(GROUP, PHONE) is True
        finally:
            store.close()

    def test_merge_keeps_existing_admin_all_row(self, tmp_path):
        """ADMIN_ALL 行与旧三权行并存：合并保留已有 ADMIN_ALL 行不动。"""
        from app.state import StateStore
        db = tmp_path / "v7mix.db"
        self._seed_v7(db, [
            (PHONE, "ADMIN_ALL", "2026-10-19 09:00:00+08:00"),
            (PHONE, "EXPORT_CHECKIN", "2026-10-15 16:35:00+08:00"),
        ])
        store = StateStore(db)
        try:
            rows = store._conn.execute(
                "SELECT permission_type, updated_at FROM export_whitelist"
                " WHERE group_id=?", (GROUP,)).fetchall()
            assert len(rows) == 1
            assert rows[0]["permission_type"] == "ADMIN_ALL"
            assert rows[0]["updated_at"] == "2026-10-19 09:00:00+08:00"
        finally:
            store.close()

    def test_merge_idempotent(self, tmp_path):
        """迁移可重复执行：v8 库再开一次不重复合并。"""
        from app.state import StateStore
        db = tmp_path / "v7idem.db"
        self._seed_v7(db, [
            (PHONE, "EXPORT_CHECKIN", "2026-10-15 16:35:00+08:00"),
            (PHONE, "EXPORT_SUMMARY", "2026-10-15 16:35:00+08:00"),
        ])
        store = StateStore(db)
        store.close()
        store2 = StateStore(db)
        try:
            n = store2._conn.execute(
                "SELECT COUNT(*) AS n FROM export_whitelist"
                " WHERE group_id=?", (GROUP,)).fetchone()["n"]
            assert n == 1
        finally:
            store2.close()


class TestGroupConfig:
    def test_upsert_and_get(self, tmp_db):
        tmp_db.upsert_group_config(GROUP, biz_switches={"checkin": True},
                                   period_type="daily")
        row = tmp_db.get_group_config(GROUP)
        assert row["period_type"] == "daily"
        import json
        assert json.loads(row["biz_switches"]) == {"checkin": True}

    def test_partial_update_keeps_others(self, tmp_db):
        tmp_db.upsert_group_config(GROUP, biz_switches={"checkin": True},
                                   daily_cutoff="19:00")
        tmp_db.upsert_group_config(GROUP, period_type="weekly")
        row = tmp_db.get_group_config(GROUP)
        assert row["period_type"] == "weekly"
        assert row["daily_cutoff"] == "19:00"


class TestMigrationV3ToV4:
    """v3 旧库 -> v4 迁移（含「_SCHEMA 先建新表」的两表并存分支）。"""

    V3_SCHEMA = """
    CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE submit_intent (
        request_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, biz TEXT NOT NULL,
        group_id TEXT NOT NULL, phone TEXT NOT NULL, period_key TEXT NOT NULL,
        session TEXT, payload_json TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE recovery_task (
        request_id TEXT PRIMARY KEY, scope_key TEXT NOT NULL, biz TEXT NOT NULL,
        status TEXT NOT NULL, due_at TEXT NOT NULL, outcome TEXT,
        created_at TEXT NOT NULL, resolved_at TEXT);
    CREATE TABLE pending_session (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL UNIQUE,
        group_id TEXT NOT NULL, phone TEXT NOT NULL, biz TEXT NOT NULL,
        arg TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
    CREATE TABLE group_config (
        group_id TEXT PRIMARY KEY, biz_switches TEXT NOT NULL DEFAULT '{}',
        period_type TEXT NOT NULL DEFAULT 'daily',
        daily_cutoff TEXT NOT NULL DEFAULT '19:00',
        reminder_rules TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL);
    CREATE TABLE checkin_required (
        group_id TEXT NOT NULL, phone TEXT NOT NULL, role TEXT,
        source TEXT NOT NULL DEFAULT 'manual', updated_at TEXT NOT NULL,
        PRIMARY KEY (group_id, phone));
    CREATE TABLE export_whitelist (
        group_id TEXT NOT NULL, phone TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (group_id, phone));
    """

    def _make_v3_db(self, path):
        import sqlite3
        conn = sqlite3.connect(str(path))
        conn.executescript(self.V3_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '3')")
        conn.execute(
            "INSERT INTO group_config(group_id, biz_switches, updated_at)"
            " VALUES(?, ?, '2026-10-13 10:00:00+08:00')",
            (GROUP, '{"checkin": true, "stats": true}'))
        conn.execute(
            "INSERT INTO checkin_required(group_id, phone, role, updated_at)"
            " VALUES(?, ?, '开发', '2026-10-13 10:00:00+08:00')",
            (GROUP, PHONE))
        conn.execute(
            "INSERT INTO export_whitelist(group_id, phone, updated_at)"
            " VALUES(?, ?, '2026-10-13 10:00:00+08:00')", (GROUP, PHONE))
        conn.commit()
        conn.close()

    def test_v3_db_migrates_to_v4(self, tmp_path):
        from app.state import StateStore
        db = tmp_path / "v3.db"
        self._make_v3_db(db)
        store = StateStore(db)
        try:
            conn = store._conn
            version = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            # v4 -> v5（P0 群绑定 / pending_verify）、v5 -> v6（D3-B1 实例表）、
            # v6 -> v7（§4.11.4 D4 定夺：h5_upload 上传凭证表）、
            # v7 -> v8（2026-10-19 白名单单权化）、
            # v8 -> v9（2026-10-20 reminder_plan.source）、
            # v9 -> v10（2026-10-20 report_field_seq 字段 id 发号器）、
            # v10 -> v11（2026-10-20 实例表加 last_callback_url/at）叠加在同链上
            assert version == "11"
            tables = {r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "group_member_task_config" in tables
            assert "checkin_required" not in tables
            # v5：group_seen 发现表 + bind_admins 列
            assert "group_seen" in tables
            gcols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(group_config)")}
            assert "bind_admins" in gcols
            # v6：qmx_robot_instance 实例表（非破坏性新增；旧库升级后为空行）
            assert "qmx_robot_instance" in tables
            icols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(qmx_robot_instance)")}
            assert icols == {"group_id", "display_name", "webhook_base_url",
                             "webhook_key_encrypted", "status",
                             "last_callback_url", "last_callback_at",
                             "created_at", "updated_at"}
            n_inst = conn.execute(
                "SELECT COUNT(*) AS n FROM qmx_robot_instance").fetchone()["n"]
            assert n_inst == 0
            # v7：h5_upload 上传凭证表（非破坏性新增；旧库升级后为空行）
            assert "h5_upload" in tables
            ucols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(h5_upload)")}
            assert ucols == {"ref", "token_id", "group_id", "phone",
                             "file_name", "mime", "size", "rel_path",
                             "created_at", "consumed"}
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM h5_upload").fetchone()["n"] == 0
            # 应交人员数据保留（phone -> user_id，enabled 默认 1）
            row = conn.execute(
                "SELECT * FROM group_member_task_config WHERE group_id=?",
                (GROUP,)).fetchone()
            assert row is not None and row["user_id"] == PHONE
            assert row["role"] == "开发" and row["enabled"] == 1
            # 白名单：v4.3 旧行拆三权，v7->v8 再合并回单一 ADMIN_ALL
            # （2026-10-19 用户指令：单权化，⛔ 推翻按业务分权）
            wl_rows = conn.execute(
                "SELECT user_id, permission_type FROM export_whitelist"
            ).fetchall()
            assert [(r["user_id"], r["permission_type"]) for r in wl_rows] \
                == [(PHONE, "ADMIN_ALL")]
            # 旧 stats 键 -> statistics
            switches = store.normalized_switches(store.get_group_config(GROUP))
            assert switches["statistics"] is True
            assert switches["checkin"] is True
            # 群配置新列就位（默认值）
            cfg = store.get_group_config(GROUP)
            assert cfg["status"] == "active"
            assert cfg["checkin_require_image"] == 1
            # pending_session 新列
            pcols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(pending_session)")}
            assert {"ttl_minutes", "payload_json"} <= pcols
            # 迁移幂等：再开一次不变样
            store.close()
            store2 = StateStore(db)
            try:
                n = store2._conn.execute(
                    "SELECT COUNT(*) AS n FROM group_member_task_config"
                ).fetchone()["n"]
                assert n == 1
            finally:
                store2.close()
        finally:
            store.close()

    def test_both_tables_exist_migrates_data(self, tmp_path):
        """模拟 _SCHEMA 先建新表（两表并存）：搬数据 + 删旧表。"""
        import sqlite3
        from app.state import StateStore
        db = tmp_path / "both.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(self.V3_SCHEMA)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '3')")
        conn.execute(
            "INSERT INTO checkin_required(group_id, phone, role, updated_at)"
            " VALUES(?, ?, '测试', '2026-10-13 10:00:00+08:00')",
            (GROUP, PHONE))
        conn.commit()
        conn.close()
        # 先执行一次 _SCHEMA（建齐 v4 新表，含空 group_member_task_config），
        # 但不跑 _migrate —— 直接改 meta 版本模拟「半迁移」现场
        store_tmp = StateStore(db)
        store_tmp._conn.execute(
            "UPDATE meta SET value='3' WHERE key='schema_version'")
        store_tmp._conn.commit()
        # 手工把旧表塞回去（模拟 _SCHEMA 建新表后旧表仍在的形态）
        store_tmp._conn.execute(
            "CREATE TABLE checkin_required (group_id TEXT NOT NULL,"
            " phone TEXT NOT NULL, role TEXT, source TEXT NOT NULL"
            " DEFAULT 'manual', updated_at TEXT NOT NULL,"
            " PRIMARY KEY (group_id, phone))")
        store_tmp._conn.execute(
            "INSERT INTO checkin_required(group_id, phone, role, updated_at)"
            " VALUES(?, ?, '测试', '2026-10-13 10:00:00+08:00')",
            (GROUP, PHONE))
        store_tmp._conn.commit()
        store_tmp.close()
        # 再开：走两表并存分支
        store = StateStore(db)
        try:
            conn = store._conn
            row = conn.execute(
                "SELECT * FROM group_member_task_config WHERE group_id=?",
                (GROUP,)).fetchone()
            assert row is not None and row["user_id"] == PHONE
            assert row["role"] == "测试"
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
                " AND name='checkin_required'").fetchone()["n"] == 0
        finally:
            store.close()
