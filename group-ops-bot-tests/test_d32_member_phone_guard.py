# -*- coding: utf-8 -*-
"""D32 回归（2026-10-21，交互巡检 P1-1 / P1-3）。

背景：后台巡检实测「新增群成员」手机号填 `12345` 可以成功入库 ——
该成员永远无法被 @、无法实名，也收不到提醒。本文件把「群成员唯一标识
必须是 11 位大陆手机号」钉成断言，并加固「切到授权 Tab 时候选必须是最新的」。

覆盖：
1. 后端新增成员：非法手机号 400「手机号格式不正确」；合法 200
2. 后端改号（PUT new_user_id）：非法 400、合法 200（新增拦住了、编辑不能开后门）
3. 前端源码守卫：addMember 用 `PHONE_RE`、输入框有失焦校验、授权 Tab 切页重拉
"""

from __future__ import annotations

from pathlib import Path

import pytest

from helpers import BOT_ROOT, GROUP, PHONE
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

INVALID_TEXT = "手机号格式不正确"


def _ready(tmp_db):
    """建号 -> 登录 -> 过强制改密门 -> 建一个空群。"""
    bootstrap(tmp_db, None)
    client = admin_client(tmp_db, Path(str(tmp_db.db_path)).parent)
    assert login(client).status_code == 200
    assert mutate(client, "POST", "/api/admin/change-password",
                  {"old_password": ADMIN_PASS,
                   "new_password": NEW_PASS}).status_code == 200
    assert login(client, password=NEW_PASS).status_code == 200
    mutate(client, "POST", "/api/admin/groups", {"group_id": GROUP})
    return client


def _add(client, user_id, nickname="张三"):
    return mutate(client, "POST", "/api/admin/groups/%s/members" % GROUP,
                  {"user_id": user_id, "nickname": nickname,
                   "role": "开发"})


# ---------- 1. 新增成员：手机号格式 ----------

class TestAddMemberPhone:
    @pytest.mark.parametrize("bad", [
        "12345",              # 巡检实测被放过的那个
        "12345678901",        # 11 位但号段非法（12 开头）
        "1380000000",         # 10 位
        "138000000000",       # 12 位
        "1380000000a",        # 混字母
        "01380000000",        # 0 开头
        "+8613800000000",     # 带国际码
    ])
    def test_rejects_invalid_phone(self, tmp_db, bad):
        client = _ready(tmp_db)
        resp = _add(client, bad)
        assert resp.status_code == 400
        assert resp.json()["detail"] == INVALID_TEXT
        # ⛔ 库里一条都不能落
        assert client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"] == []

    def test_accepts_valid_phone(self, tmp_db):
        client = _ready(tmp_db)
        assert _add(client, PHONE).status_code == 200
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert [m["user_id"] for m in members] == [PHONE]

    def test_error_text_is_exact(self, tmp_db):
        """文案必须就是「手机号格式不正确」（前端提示同一条）。"""
        client = _ready(tmp_db)
        assert _add(client, "abc").json()["detail"] == INVALID_TEXT


# ---------- 2. 改号：同一口径（⛔ 不留后门） ----------

class TestRenamePhone:
    def test_rejects_invalid_new_phone(self, tmp_db):
        client = _ready(tmp_db)
        assert _add(client, PHONE).status_code == 200
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": "abc"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == INVALID_TEXT
        # 名单原样（没有被半改）
        members = client.get(
            "/api/admin/groups/%s/members" % GROUP).json()["members"]
        assert [m["user_id"] for m in members] == [PHONE]

    def test_accepts_valid_new_phone(self, tmp_db):
        client = _ready(tmp_db)
        assert _add(client, PHONE).status_code == 200
        new_phone = "13800138099"
        resp = mutate(client, "PUT",
                      "/api/admin/groups/%s/members/%s" % (GROUP, PHONE),
                      {"new_user_id": new_phone})
        assert resp.status_code == 200
        assert resp.json()["user_id"] == new_phone


# ---------- 3. 前端源码守卫（⛔ 防回退到宽松校验） ----------

class TestFrontendGuard:
    def _src(self) -> str:
        return (BOT_ROOT / "admin-ui/src/components/system/"
                "GroupManagement.tsx").read_text(encoding="utf-8")

    def test_phone_regex_single_source(self):
        src = self._src()
        assert "const PHONE_RE = /^1[3-9]\\d{9}$/" in src
        # 原来那条 4~32 位数字/字母的宽松规则必须彻底消失
        assert "0-9A-Za-z_-]{4,32}" not in src

    def test_add_form_blocks_and_hints(self):
        src = self._src()
        assert "PHONE_INVALID_TEXT" in src
        # 失焦即校验 + 输入长度收敛
        assert 'maxLength={11}' in src
        assert "onBlur={() => {" in src

    def test_admins_tab_refetches_candidates(self):
        """切到授权 Tab 时重拉成员与授权（候选 = 在岗成员 − 已授权）。"""
        src = self._src()
        assert "const openAdminsTab = useCallback" in src
        assert "onClick={() => setSubView('admins')}" not in src
        assert "onClick={openAdminsTab}" in src
