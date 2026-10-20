# -*- coding: utf-8 -*-
"""管理后台测试（:18003，TestClient 进程内 ASGI，§4.7 完成标准逐条对应）。

覆盖 §4.7 完成标准：
- 未登录不能改配置 / 绕前端直调被拒
- 改密后初始凭据失效 / 旧会话吊销
- 登录限流锁定
- 变更留痕（admin_audit）
- 三概念分离（群 / 应交名单 / 白名单分权）
- CSRF + Origin 校验
- 群停用 -> 回调抑制（与 :18002 联动）

⛔ 这是本地自测证据，不构成真实部署 / 平台验证（§7.2）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.admin import auth as admin_auth
from app.admin.server import create_admin_app
from app.config import ConfigError
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx

ADMIN_USER = "admin"
ADMIN_PASS = "initial-pass-1"
NEW_PASS = "formal-pass-2"


def bootstrap(tmp_db, monkeypatch, username=ADMIN_USER, password=ADMIN_PASS):
    monkeypatch.setenv("GOPS_ADMIN_USERNAME", username)
    monkeypatch.setenv("GOPS_ADMIN_PASSWORD", password)
    return admin_auth.bootstrap_admin(tmp_db)


def admin_client(tmp_db, tmp_path, now_fn=None):
    """now_fn：手动提醒等时钟敏感端点的注入口（⛔ 不传 = 真实时钟）。"""
    app = create_admin_app(state=tmp_db, backend=FakeBackend(),
                           sender=RecordingSender(),
                           config=make_config(tmp_path), now_fn=now_fn)
    return TestClient(app)


def login(client, username=ADMIN_USER, password=ADMIN_PASS):
    return client.post("/api/admin/login",
                       json={"username": username, "password": password})


def mutate(client, method, path, body=None, headers=None):
    """带 CSRF 头的变更请求（从 /me 取会话绑定令牌）。"""
    me = client.get("/api/admin/me")
    if me.status_code != 200:
        return me
    merged = {"X-CSRF-Token": me.json()["csrf_token"]}
    if headers:
        merged.update(headers)
    return client.request(method, path, json=body, headers=merged)


# ---------- 初始凭据引导 ----------

class TestBootstrap:
    def test_missing_env_fails_closed(self, tmp_db, monkeypatch):
        monkeypatch.delenv("GOPS_ADMIN_USERNAME", raising=False)
        monkeypatch.delenv("GOPS_ADMIN_PASSWORD", raising=False)
        with pytest.raises(ConfigError):
            admin_auth.bootstrap_admin(tmp_db)

    def test_short_password_fails_closed(self, tmp_db, monkeypatch):
        with pytest.raises(ConfigError):
            bootstrap(tmp_db, monkeypatch, password="short")

    def test_creates_must_change_admin(self, tmp_db, monkeypatch):
        result = bootstrap(tmp_db, monkeypatch)
        assert result == "created"
        row = tmp_db.get_admin_user(ADMIN_USER)
        assert row is not None
        assert row["must_change_password"] == 1
        assert row["password_hash"].startswith("pbkdf2_sha256$")
        assert ADMIN_PASS not in row["password_hash"]  # ⛔ 不存明文

    def test_existing_admin_ignores_env(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        # 再设一套不同环境变量：⛔ 不创建第二个账号（初始凭据不可复用）
        monkeypatch.setenv("GOPS_ADMIN_USERNAME", "intruder")
        monkeypatch.setenv("GOPS_ADMIN_PASSWORD", "another-pass-9")
        assert admin_auth.bootstrap_admin(tmp_db) == "existing"
        assert tmp_db.get_admin_user("intruder") is None
        assert tmp_db.count_admin_users() == 1


# ---------- 密码哈希 ----------

class TestPasswordHash:
    def test_verify_roundtrip(self):
        stored = admin_auth.hash_password("correct-horse")
        assert admin_auth.verify_password("correct-horse", stored)
        assert not admin_auth.verify_password("wrong", stored)

    def test_tampered_hash_rejected(self):
        stored = admin_auth.hash_password("correct-horse")
        assert not admin_auth.verify_password("correct-horse", stored + "x")
        assert not admin_auth.verify_password("correct-horse", "plain$1$a$b")


# ---------- 登录 / 限流 ----------

class TestLogin:
    def test_wrong_password_counts_failure(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        resp = login(client, password="wrong-pass-1")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"

    def test_unknown_user_same_message(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        resp = login(client, username="ghost", password="whatever1")
        assert resp.status_code == 401
        assert resp.json()["detail"] == "用户名或密码错误"  # ⛔ 防枚举

    def test_lock_after_max_failures(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        for _ in range(5):
            assert login(client, password="wrong-pass-1").status_code == 401
        # 第 6 次：即使密码正确也锁定（429）
        resp = login(client, password=ADMIN_PASS)
        assert resp.status_code == 429

    def test_success_resets_failures(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        for _ in range(3):
            login(client, password="wrong-pass-1")
        assert login(client, password=ADMIN_PASS).status_code == 200
        # 成功后失败计数清零：再错 4 次不应锁定
        for _ in range(4):
            assert login(client, password="wrong-pass-1").status_code == 401
        assert login(client, password=ADMIN_PASS).status_code == 200


# ---------- 会话 ----------

class TestSession:
    def _login_identity(self, tmp_db):
        ok, identity = admin_auth.login(tmp_db, ADMIN_USER, ADMIN_PASS,
                                        "1.2.3.4")
        assert ok == "ok"
        return identity

    def test_load_and_expiry(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        identity = self._login_identity(tmp_db)
        token = admin_auth.create_session(tmp_db, identity, "1.2.3.4")
        loaded = admin_auth.load_session(tmp_db, token)
        assert loaded is not None
        assert loaded.username == ADMIN_USER
        # 乱码令牌 -> None
        assert admin_auth.load_session(tmp_db, "tampered") is None

    def test_no_idle_expiry_by_default(self, tmp_db, monkeypatch):
        """⭐ 2026-10-19 用户指令：凭证过期前一直保持登录（空闲不失效）。

        把 last_seen_at 推到 10 小时前（远超旧的 30 分钟），会话仍有效。
        """
        bootstrap(tmp_db, monkeypatch)
        identity = self._login_identity(tmp_db)
        token = admin_auth.create_session(tmp_db, identity, "1.2.3.4")
        past = (datetime.now(TZ) - timedelta(hours=10)).isoformat(
            timespec="seconds")
        tmp_db._conn.execute(
            "UPDATE admin_session SET last_seen_at=?", (past,))
        tmp_db._conn.commit()
        assert admin_auth.load_session(tmp_db, token) is not None

    def test_idle_expiry_when_explicitly_enabled(self, tmp_db, monkeypatch):
        """空闲超时仍可显式开启（env / 常量 >0 时才生效）。"""
        bootstrap(tmp_db, monkeypatch)
        monkeypatch.setattr(admin_auth, "SESSION_IDLE_MINUTES", 30)
        identity = self._login_identity(tmp_db)
        token = admin_auth.create_session(tmp_db, identity, "1.2.3.4")
        past = (datetime.now(TZ) - timedelta(minutes=31)).isoformat(
            timespec="seconds")
        tmp_db._conn.execute(
            "UPDATE admin_session SET last_seen_at=?", (past,))
        tmp_db._conn.commit()
        assert admin_auth.load_session(tmp_db, token) is None
        assert tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM admin_session").fetchone()["n"] == 0

    def test_default_lifetime_is_one_week(self, tmp_db, monkeypatch):
        """默认会话凭证 7 天（168h），Cookie Max-Age 与之对齐。"""
        assert admin_auth.SESSION_ABSOLUTE_HOURS == 168
        assert admin_auth.SESSION_IDLE_MINUTES == 0
        bootstrap(tmp_db, monkeypatch)
        identity = self._login_identity(tmp_db)
        token = admin_auth.create_session(tmp_db, identity, "1.2.3.4")
        row = tmp_db._conn.execute(
            "SELECT expires_at FROM admin_session").fetchone()
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=TZ)
        remaining = expires_at - datetime.now(TZ)
        assert timedelta(hours=167) < remaining <= timedelta(hours=168)
        assert admin_auth.load_session(tmp_db, token) is not None

    def test_login_cookie_max_age_matches(self, tmp_db, monkeypatch):
        """登录响应 Cookie Max-Age = SESSION_ABSOLUTE_HOURS*3600（跨浏览器重启仍有效）。"""
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        resp = login(client, password=ADMIN_PASS)
        assert resp.status_code == 200
        set_cookie = resp.headers.get("set-cookie", "")
        assert "Max-Age=%d" % (admin_auth.SESSION_ABSOLUTE_HOURS * 3600) \
            in set_cookie

    def test_absolute_expiry_drops_session(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        identity = self._login_identity(tmp_db)
        token = admin_auth.create_session(tmp_db, identity, "1.2.3.4")
        past = (datetime.now(TZ) - timedelta(hours=169)).isoformat(
            timespec="seconds")
        tmp_db._conn.execute(
            "UPDATE admin_session SET expires_at=?", (past,))
        tmp_db._conn.commit()
        assert admin_auth.load_session(tmp_db, token) is None


# ---------- API：鉴权门 ----------

class TestApiAuth:
    def test_unauthenticated_rejected(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert client.get("/api/admin/groups").status_code == 401
        # 绕前端直调变更同样被拒
        assert client.post("/api/admin/groups",
                           json={"group_id": "G1"}).status_code == 401

    def test_mutation_without_csrf_rejected(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        resp = client.post("/api/admin/groups", json={"group_id": "G1"})
        assert resp.status_code == 403  # ⛔ 无 CSRF 头

    def test_mutation_with_wrong_origin_rejected(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        resp = mutate(client, "POST", "/api/admin/groups",
                      {"group_id": "G1"},
                      headers={"Origin": "http://evil.example"})
        assert resp.status_code == 403

    def test_forced_change_password_gate(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        # 强制改密未完成：其他功能一律 403
        assert client.get("/api/admin/groups").status_code == 403
        assert mutate(client, "POST", "/api/admin/groups",
                      {"group_id": "G1"}).status_code == 403
        # /me 与 logout 可用
        assert client.get("/api/admin/me").status_code == 200

    def test_change_password_flow(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        # 旧密码错 -> 400
        resp = mutate(client, "POST", "/api/admin/change-password",
                      {"old_password": "not-the-one", "new_password": NEW_PASS})
        assert resp.status_code == 400
        # 新密码与旧相同 -> 400
        resp = mutate(client, "POST", "/api/admin/change-password",
                      {"old_password": ADMIN_PASS, "new_password": ADMIN_PASS})
        assert resp.status_code == 400
        # 正常改密 -> ok，旧会话吊销
        resp = mutate(client, "POST", "/api/admin/change-password",
                      {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert resp.status_code == 200
        assert client.get("/api/admin/me").status_code == 401  # 会话已吊销
        # 初始凭据失效；新密码可登且不再强制改密
        assert login(client, password=ADMIN_PASS).status_code == 401
        resp = login(client, password=NEW_PASS)
        assert resp.status_code == 200
        assert resp.json()["must_change_password"] is False
        assert client.get("/api/admin/groups").status_code == 200

    def test_logout_clears_session(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        assert client.post("/api/admin/logout").status_code == 200
        assert client.get("/api/admin/me").status_code == 401


# ---------- API：群管理 / 开关 ----------

class TestGroupAdmin:
    def _client(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        return client

    def test_group_crud(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "POST", "/api/admin/groups",
                      {"group_id": GROUP, "group_name": "测试群"})
        assert resp.status_code == 200
        # 重复建群 -> 409
        assert mutate(client, "POST", "/api/admin/groups",
                      {"group_id": GROUP}).status_code == 409
        # 列表默认五业务全开 + active
        groups = client.get("/api/admin/groups").json()["groups"]
        row = [g for g in groups if g["group_id"] == GROUP][0]
        assert row["group_name"] == "测试群"
        assert row["status"] == "active"
        assert all(row["switches"].values())
        # 改名 + 停用
        assert mutate(client, "PUT", "/api/admin/groups/" + GROUP,
                      {"group_name": "新名", "status": "disabled"}
                      ).status_code == 200
        row = [g for g in client.get("/api/admin/groups").json()["groups"]
               if g["group_id"] == GROUP][0]
        assert row["group_name"] == "新名"
        assert row["status"] == "disabled"
        # 不存在的群 -> 404
        assert mutate(client, "PUT", "/api/admin/groups/GNOPE",
                      {"status": "active"}).status_code == 404

    def test_switch_partial_merge(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"switches": {"checkin": False}})
        assert resp.status_code == 200
        switches = resp.json()["config"]["switches"]
        assert switches["checkin"] is False
        assert switches["summary"] is True  # 其余保持
        assert switches["statistics"] is True

    def test_config_validation(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
        # 截止时间格式错 -> 400
        assert mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"daily_cutoff": "25:99"}).status_code == 400
        # 打卡截止缺 pm -> 400
        assert mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"checkin_deadlines": {"am": "08:00"}}
                      ).status_code == 400
        # 合法配置 -> ok
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/config" % GROUP,
                      {"period_type": "weekly", "daily_cutoff": "18:30",
                       "checkin_deadlines": {"am": "08:00", "pm": "18:00"},
                       "checkin_require_image": False})
        assert resp.status_code == 200
        cfg = resp.json()["config"]
        assert cfg["period_type"] == "weekly"
        assert cfg["daily_cutoff"] == "18:30"
        assert cfg["checkin_deadlines"] == {"am": "08:00", "pm": "18:00"}
        assert cfg["checkin_require_image"] is False


# ---------- API：应交人员 ----------

class TestMemberAdmin:
    def _client(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
        return client

    def test_member_lifecycle(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/members" % GROUP,
                      {"user_id": PHONE, "nickname": "张三",
                       "role": "开发"})
        assert resp.status_code == 200
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert len(members) == 1
        assert members[0]["user_id"] == PHONE
        assert members[0]["nickname"] == "张三"
        assert members[0]["enabled"] is True
        # 停用 = 停止未来任务（行保留）
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"enabled": False})
        assert resp.status_code == 200
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert members[0]["enabled"] is False
        # 重新启用
        mutate(client, "PUT",
               "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
               {"enabled": True, "nickname": "张三丰"})
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert members[0]["enabled"] is True
        assert members[0]["nickname"] == "张三丰"
        # 删除 -> 停止未来任务（历史数据保留，见审计备注）
        resp = mutate(client, "DELETE",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE))
        assert resp.status_code == 200
        assert client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"] == []

    def test_update_missing_member_404(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"enabled": False})
        assert resp.status_code == 404

    def test_member_phone_can_be_edited(self, tmp_db, monkeypatch):
        """⭐ 2026-10-19 用户指令：群成员编辑可修改手机号（唯一标识）。"""
        client = self._client(tmp_db, monkeypatch)
        mutate(client, "POST", "/api/admin/groups/%s/members" % GROUP,
               {"user_id": PHONE, "nickname": "张三", "role": "开发"})
        # 先把授权挂在旧手机号上，改号后必须跟着走（⛔ 不失联）
        tmp_db.upsert_whitelist(GROUP, PHONE, "ADMIN_ALL")
        new_phone = "13800000009"
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"nickname": "张三丰", "role": "测试",
                       "new_user_id": new_phone})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == new_phone
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert [m["user_id"] for m in members] == [new_phone]
        assert members[0]["nickname"] == "张三丰"
        assert members[0]["role"] == "测试"
        assert [r["user_id"] for r in tmp_db.list_whitelist(GROUP)] == \
            [new_phone]

    def test_member_phone_conflict_409(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        mutate(client, "POST", "/api/admin/groups/%s/members" % GROUP,
               {"user_id": PHONE, "nickname": "张三", "role": "开发"})
        mutate(client, "POST", "/api/admin/groups/%s/members" % GROUP,
               {"user_id": "13800000009", "nickname": "李四", "role": "开发"})
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": "13800000009"})
        assert resp.status_code == 409


# ---------- API：导出白名单（2026-10-19 单权化 + 限群成员） ----------

class TestWhitelistAdmin:
    def _client(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
        return client

    def _add_member(self, client, user_id, nickname=None):
        body = {"user_id": user_id}
        if nickname:
            body["nickname"] = nickname
        return mutate(client, "POST",
                      "/api/admin/groups/%s/members" % GROUP, body)

    def test_whitelist_single_permission(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        assert self._add_member(client, PHONE, "郑佳").status_code == 200
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/whitelist" % GROUP,
                      {"user_id": PHONE})
        assert resp.status_code == 200
        entries = client.get(
            "/api/admin/groups/%s/whitelist" % GROUP).json()["whitelist"]
        assert len(entries) == 1
        assert entries[0]["permission_type"] == "ADMIN_ALL"
        assert entries[0]["nickname"] == "郑佳"
        # 单权：重复授权不产生第二行（INSERT OR IGNORE）
        mutate(client, "POST",
               "/api/admin/groups/%s/whitelist" % GROUP,
               {"user_id": PHONE})
        entries = client.get(
            "/api/admin/groups/%s/whitelist" % GROUP).json()["whitelist"]
        assert len(entries) == 1
        assert tmp_db.has_whitelist_permission(GROUP, PHONE,
                                               "ADMIN_ALL") is True
        # 移除授权
        resp = mutate(client, "DELETE",
                      "/api/admin/groups/%s/whitelist" % GROUP,
                      {"user_id": PHONE})
        assert resp.status_code == 200
        assert tmp_db.has_whitelist_permission(GROUP, PHONE,
                                               "ADMIN_ALL") is False
        assert client.get(
            "/api/admin/groups/%s/whitelist" % GROUP
        ).json()["whitelist"] == []

    def test_whitelist_rejects_non_member(self, tmp_db, monkeypatch):
        """非应交人员一律 400（含未添加/已停用成员）。"""
        client = self._client(tmp_db, monkeypatch)
        # 未添加的成员
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/whitelist" % GROUP,
                      {"user_id": PHONE})
        assert resp.status_code == 400
        assert "应交人员" in resp.json()["detail"]
        assert tmp_db.in_whitelist(GROUP, PHONE) is False
        # 已停用的成员同样拒绝
        assert self._add_member(client, PHONE).status_code == 200
        assert mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"enabled": False}).status_code == 200
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/whitelist" % GROUP,
                      {"user_id": PHONE})
        assert resp.status_code == 400
        assert tmp_db.in_whitelist(GROUP, PHONE) is False

    def test_member_remove_cascades_whitelist(self, tmp_db, monkeypatch):
        """移除应交人员 -> 其导出授权级联失效（⛔ 不留孤儿授权）。"""
        client = self._client(tmp_db, monkeypatch)
        assert self._add_member(client, PHONE).status_code == 200
        assert mutate(client, "POST",
                      "/api/admin/groups/%s/whitelist" % GROUP,
                      {"user_id": PHONE}).status_code == 200
        assert tmp_db.in_whitelist(GROUP, PHONE) is True
        # 删除成员
        resp = mutate(client, "DELETE",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE))
        assert resp.status_code == 200
        assert tmp_db.in_whitelist(GROUP, PHONE) is False
        # 再删一次成员（白名单已空）不报错（幂等）
        assert mutate(client, "DELETE",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE)
                      ).status_code == 200


# ---------- API：字段池 ----------

class TestFieldAdmin:
    def _client(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
        return client

    def test_field_lifecycle(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/fields" % GROUP,
                      {"field_name": "销售额", "unit": "元",
                       "roles": ["产品"]})
        assert resp.status_code == 200
        assert resp.json()["field_id"] == 1
        fields = client.get(
            "/api/admin/groups/%s/fields" % GROUP).json()["fields"]
        assert fields[0]["field_name"] == "销售额"
        assert fields[0]["enabled"] is True
        # 改名 + 停用
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/fields/1" % GROUP,
                      {"field_name": "销售额(万元)", "enabled": False})
        assert resp.status_code == 200
        fields = client.get(
            "/api/admin/groups/%s/fields" % GROUP).json()["fields"]
        assert fields[0]["field_name"] == "销售额(万元)"
        assert fields[0]["enabled"] is False
        # 2026-10-20 用户指令：删除 = **物理删除该字段**（⛔ 不再降级成停用）
        resp = mutate(client, "DELETE",
                      "/api/admin/groups/%s/fields/1" % GROUP)
        assert resp.status_code == 200
        assert "删除" in resp.json()["message"]
        fields = client.get(
            "/api/admin/groups/%s/fields" % GROUP).json()["fields"]
        assert fields == []          # 行真删了（⛔ 不是 enabled=False 留着）
        # 再删同一字段 -> 404（⛔ 不伪装成功）
        assert mutate(client, "DELETE",
                      "/api/admin/groups/%s/fields/1" % GROUP
                      ).status_code == 404
        # 删掉最大号后新建字段 ⛔ 不复用旧 id（MySQL report_item 按 field_id
        # 归并历史列，复用会把已删字段的历史数量并进新字段）
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/fields" % GROUP,
                      {"field_name": "新增字段"})
        assert resp.json()["field_id"] == 2


# ---------- API：审计 ----------

class TestAudit:
    def test_mutations_are_audited(self, tmp_db, monkeypatch):
        bootstrap(tmp_db, monkeypatch)
        client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
        assert login(client).status_code == 200
        mutate(client, "POST", "/api/admin/change-password",
               {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
        assert login(client, password=NEW_PASS).status_code == 200
        mutate(client, "POST", "/api/admin/groups",
               {"group_id": GROUP, "group_name": "审计群"})
        mutate(client, "POST", "/api/admin/groups/%s/members" % GROUP,
               {"user_id": PHONE})
        audit = client.get("/api/admin/audit").json()["audit"]
        actions = [a["action"] for a in audit]
        assert "group.create" in actions
        assert "member.add" in actions
        assert "change_password" in actions
        assert "login" in actions
        for entry in audit:
            assert entry["username"] == ADMIN_USER
            assert entry["result"] in ("ok", "rejected")
            # ⛔ 审计不含任何密码材料
            assert NEW_PASS not in (entry["detail"] or "")
            assert ADMIN_PASS not in (entry["detail"] or "")


# ---------- 与 :18002 联动：群停用抑制回调 ----------

class TestDisabledGroupSuppression:
    def test_disabled_group_replies_disabled_text(self, tmp_db):
        """⭐ 2026-10-19 起停用群不再静默：回「已停用」固定话术且不跑业务。"""
        from app.web.server import create_app
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, status="disabled")
        app = create_app(state=tmp_db, backend=FakeBackend(),
                         sender=RecordingSender(),
                         config=make_config(tmp_db.db_path and "/tmp"),
                         now_fn=lambda: datetime(2026, 10, 13, 10, 0,
                                                 tzinfo=TZ))
        client = TestClient(app)
        resp = client.post("/api/callback", json={
            "group_id": GROUP, "phone": PHONE, "text": "打卡",
            "reply_url": "http://reply.test/hook"})
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert resp.json()["reason"] == "disabled"
