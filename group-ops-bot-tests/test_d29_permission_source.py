# -*- coding: utf-8 -*-
"""D29（2026-10-20 用户指令）：权限唯一真源守卫。

用户口径（原话要点）：
- 「群聊管理员是后台管理员操作定义的，禁止其他地方定义这个权限」
- 「后台管理员现在是郑佳，但上线后会交给特定人维护，这样我就不是后台
  管理员了 —— 禁止写死这些东西变量」

因此本文件把「权限不能从旁路来」固化成可回归的断言：
1. `BotConfig` ⛔ 不含任何权限名单字段（原 `bind_admin_phones` 已删除）；
2. `load_bot_config` 即使环境变量里还写着老名单，也**读不出任何权限**；
3. 群管理员（`export_whitelist`）**只有后台 API 能写**，且群内「绑定」
   ⛔ 不产生授权行（绑定 ≠ 授权）；
4. 机器人代码里 ⛔ 不出现硬编码手机号（后台管理员身份只存在 `admin_user` 表，
   首次引导之后环境变量一律被忽略）。
"""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path

import pytest

from app import state as state_mod
from app.admin import auth as admin_auth
from app.config import BotConfig, load_bot_config
from app.handlers import bind as bind_handler
from helpers import make_config, make_ctx

APP_DIR = Path(state_mod.__file__).resolve().parent
PHONE_RE = re.compile(r"1[3-9]\d{9}")


def _py_files():
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in p.parts]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------- 1. 配置层不承载权限 ----------

class TestNoPermissionInConfig:
    def test_bot_config_has_no_permission_fields(self):
        """配置类是「运行参数」，⛔ 不是权限表。"""
        names = {f.name for f in dataclasses.fields(BotConfig)}
        for banned in ("bind_admin_phones", "admin_phones", "admin_users",
                       "admins", "whitelist", "super_admin", "allowed_phones"):
            assert banned not in names, banned

    def test_legacy_env_var_grants_nothing(self, monkeypatch):
        """线上 env 里还留着老名单也不会生效（D28 已不判定，D29 字段已删）。"""
        monkeypatch.setenv("GOPS_MCP_BASE_URL", "http://127.0.0.1:18001")
        monkeypatch.setenv("GOPS_EXTERNAL_BASE_URL", "http://127.0.0.1:18002")
        monkeypatch.setenv("GOPS_BIND_ADMIN_PHONES", "17313771297")
        cfg = load_bot_config()
        assert not hasattr(cfg, "bind_admin_phones")
        assert "17313771297" not in repr(cfg)

    def test_no_hardcoded_phone_in_bot_code(self):
        """⛔ 机器人代码里不得出现硬编码手机号（身份只存库里）。"""
        offenders = []
        for path in _py_files():
            for lineno, line in enumerate(_read(path).splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                if PHONE_RE.search(line):
                    offenders.append("%s:%d" % (path.name, lineno))
        assert offenders == [], offenders


# ---------- 2. 群管理员只能由后台授权 ----------

class TestWhitelistSingleSource:
    def test_only_backend_api_grants_whitelist(self):
        """写授权行的调用点只允许出现在后台 API（state.py 只作定义）。"""
        callers = set()
        for path in _py_files():
            for line in _read(path).splitlines():
                if "upsert_whitelist(" in line and "def " not in line:
                    callers.add(path.name)
        assert callers == {"api.py"}, callers

    def test_no_sql_insert_outside_state(self):
        """⛔ 任何模块都不得绕过状态层直接 INSERT 授权表。"""
        for path in _py_files():
            if path.name == "state.py":
                continue
            text = _read(path).upper()
            assert "INSERT INTO EXPORT_WHITELIST" not in text, path.name
            assert "INSERT OR REPLACE INTO EXPORT_WHITELIST" not in text, path.name

    def test_bind_command_grants_no_permission(self, tmp_db):
        """群内「绑定」只建群配置，⛔ 绝不写授权表（绑定 ≠ 群管理员）。"""
        config = make_config(Path(tmp_db.db_path).parent)
        ctx = make_ctx(tmp_db, None, config, group_id="G-BIND",
                       phone="22644212509", text="绑定")
        first = bind_handler.handle_command(
            bind_handler.commands.parse("绑定"), ctx)
        assert "请输入要绑定的群名称" in first
        second = bind_handler.handle_pending_text_if_any(
            make_ctx(tmp_db, None, config, group_id="G-BIND",
                     phone="22644212509", text="新群"))
        assert "绑定成功" in second
        assert tmp_db.get_group_config("G-BIND") is not None
        assert list(tmp_db.list_whitelist("G-BIND")) == []


# ---------- 3. 后台管理员身份不写死 ----------

class TestAdminIdentityNotHardcoded:
    def test_env_used_only_for_first_bootstrap(self, tmp_db, monkeypatch):
        """首次按 env 建号；之后 env 改成任何人都不影响现有账号。"""
        monkeypatch.setenv("GOPS_ADMIN_USERNAME", "first-admin")
        monkeypatch.setenv("GOPS_ADMIN_PASSWORD", "Aa12345678")
        assert admin_auth.bootstrap_admin(tmp_db) == "created"

        monkeypatch.setenv("GOPS_ADMIN_USERNAME", "郑佳")
        assert admin_auth.bootstrap_admin(tmp_db) == "existing"
        names = [r["username"] for r in tmp_db._conn.execute(
            "SELECT username FROM admin_user ORDER BY id")]
        assert names == ["first-admin"]
        assert tmp_db.count_admin_users() == 1

    def test_admin_can_be_renamed_for_handover(self, tmp_db, monkeypatch):
        """交接口径：改名 + 改密即可，无需改任何代码或环境变量。"""
        monkeypatch.setenv("GOPS_ADMIN_USERNAME", "first-admin")
        monkeypatch.setenv("GOPS_ADMIN_PASSWORD", "Aa12345678")
        admin_auth.bootstrap_admin(tmp_db)
        status, identity = admin_auth.login(
            tmp_db, "first-admin", "Aa12345678", "127.0.0.1")
        assert status == "ok"
        ok, err = admin_auth.change_username(
            tmp_db, identity, "ops-owner", "Aa12345678")
        assert ok and err is None
        assert tmp_db.get_admin_user_by_id(identity.user_id)["username"] \
            == "ops-owner"
        assert admin_auth.login(
            tmp_db, "ops-owner", "Aa12345678", "127.0.0.1")[0] == "ok"


# ---------- 4. 源码级断言：绑定处理器不再引用任何名单 ----------

def _code_only(path: Path) -> str:
    """去掉行内注释后的代码（注释里允许写历史沿革，代码里不允许）。"""
    return "\n".join(line.split("#", 1)[0]
                     for line in _read(path).splitlines())


def test_bind_handler_has_no_whitelist_reference():
    src = _code_only(APP_DIR / "handlers" / "bind.py")
    assert "bind_admin_phones" not in src
    assert "_is_bind_admin(" not in src
    assert "NOT_ADMIN_TEXT =" not in src
