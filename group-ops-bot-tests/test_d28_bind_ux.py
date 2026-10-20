# -*- coding: utf-8 -*-
"""D28（2026-10-20 用户指令）：新群绑定实测暴露的三个问题。

用户原话（要点）：
1. 「我这里录入了群名：项目群聊机器人测试，为什么后台还是显示群 id 而不是我
   录入的群名」→ 根因：群内两轮绑定只写 `group_config.group_name`，实例行的
   `display_name`（后台备注名）仍为空，前端 `display_name || group_id` 回退成
   一串数字。修法：实例查询 LEFT JOIN 群配置带出 `group_name`，前端三级兜底。
2. 「这个群我还没有来得及授权群成员为管理员，为什么郑佳也可以触发绑定。
   所以这里还是修复逻辑为全体用户都可以触发绑定」→ 移除手机号名单判定。
3. 「待绑定状态有两个可以进行绑定的按钮：确认绑定和修改。保留一个就行」
   → 待绑定行只留「确认绑定」（「修改」弹的是同一组字段）。

⛔ 前端交互用源码静态守卫验证（本项目无 JS 测试运行器），行为真值以构建产物为准。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import commands
from app.admin.api import _instance_payload
from app.handlers import bind as bind_handler
from doubles.fakes import FakeBackend
from helpers import OTHER_PHONE, PHONE, make_ctx

GROUP = "2100060349044391937"      # 用户实测的新群（脱敏后仅作标识）
GROUP_NAME = "项目群聊机器人测试"

BOT = Path(__file__).resolve().parent.parent / "group-ops-bot"
SRC = BOT / "admin-ui" / "src" / "components" / "system"


def _ctx(store, backend, phone=OTHER_PHONE, text="绑定"):
    from datetime import datetime
    from helpers import TZ
    return make_ctx(store, backend,
                    _config(Path(store.db_path).parent),
                    now=datetime(2026, 10, 20, 16, 30, tzinfo=TZ),
                    group_id=GROUP, phone=phone, text=text)


def _config(data_dir):
    """D29：BotConfig 已无 bind_admin_phones —— 绑定不需要任何名单。"""
    from app.config import BotConfig
    return BotConfig(
        callback_port=18002, admin_port=18003,
        external_base_url="http://127.0.0.1:18002",
        tz_name="Asia/Shanghai", data_dir=str(data_dir),
        daily_cutoff=(19, 0), mcp=None)


# ---------- 问题 1：群名录进去了，后台要显示出来 ----------

class TestInstanceGroupName:
    def test_instance_payload_carries_group_name(self, tmp_db):
        tmp_db.ensure_robot_instance(GROUP)          # T0 自动发现（display_name 空）
        tmp_db.create_group(GROUP, GROUP_NAME)       # 群内绑定写入的群名
        payload = _instance_payload(tmp_db.get_robot_instance(GROUP))
        assert payload["display_name"] is None       # 后台备注名仍未设
        assert payload["group_name"] == GROUP_NAME   # ⭐ 兜底来源

    def test_list_instances_carries_group_name(self, tmp_db):
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db.create_group(GROUP, GROUP_NAME)
        rows = list(tmp_db.list_robot_instances())
        assert [r["group_name"] for r in rows] == [GROUP_NAME]

    def test_未绑定实例没有群名(self, tmp_db):
        """WAIT_BIND（还没绑）-> group_name 为 None，前端继续显示群 ID + 新群接入。"""
        tmp_db.ensure_robot_instance(GROUP)
        payload = _instance_payload(tmp_db.get_robot_instance(GROUP))
        assert payload["group_name"] is None
        assert payload["status"] == "WAIT_BIND"

    def test_重命名群配置后显示名跟着变(self, tmp_db):
        """⛔ 不把群名复制进实例行：群配置改名后列表立刻反映（单一真源）。"""
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db.create_group(GROUP, "旧群名")
        tmp_db.upsert_group_config(GROUP, group_name="新群名")
        payload = _instance_payload(tmp_db.get_robot_instance(GROUP))
        assert payload["group_name"] == "新群名"

    def test_payload_display_name_still_wins(self, tmp_db):
        """后台备注名优先（管理员显式改过的名字不被群名覆盖）。"""
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db.create_group(GROUP, GROUP_NAME)
        tmp_db.bind_robot_instance(GROUP, "https://example.test/hook",
                                   display_name="运维备注名")
        payload = _instance_payload(tmp_db.get_robot_instance(GROUP))
        assert payload["display_name"] == "运维备注名"
        assert payload["group_name"] == GROUP_NAME

    def test_前端三级兜底顺序(self):
        """display_name -> group_name -> group_id（源码级锁定）。"""
        src = (SRC / "RobotInstance.tsx").read_text(encoding="utf-8")
        assert "row.display_name || row.group_name || row.group_id" in src


# ---------- 问题 2：全体群成员都能触发绑定 ----------

class TestBindOpenToAllMembers:
    def test_first_round_needs_no_phone_whitelist(self, tmp_db):
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend))
        assert "请输入要绑定的群名称" in reply
        assert "无权执行绑定" not in reply
        assert tmp_db.get_group_config(GROUP) is None

    def test_two_rounds_from_anyone_creates_group(self, tmp_db):
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend))
        second = bind_handler.handle_pending_text_if_any(
            _ctx(tmp_db, backend, text=GROUP_NAME))
        assert "绑定成功" in second
        row = tmp_db.get_group_config(GROUP)
        assert row["group_name"] == GROUP_NAME
        # 记录实际绑定人（审计用），⛔ 不是权限判据
        assert OTHER_PHONE in (row["bind_admins"] or "")

    def test_no_unauthorized_security_event(self, tmp_db):
        backend = FakeBackend()
        bind_handler.handle_command(commands.parse("绑定"),
                                    _ctx(tmp_db, backend))
        rows = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='bind_unauthorized'"
        ).fetchall()
        assert rows == []

    def test_already_bound_still_idempotent(self, tmp_db):
        """放开名单 ⛔ 不等于放开幂等：已绑定群照旧告知已绑定。"""
        tmp_db.create_group(GROUP, GROUP_NAME)
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend))
        assert "本群已绑定" in reply

    def test_disabled_instance_still_refused(self, tmp_db):
        """实例被停用照旧不给接入（这条护栏与绑定人是谁无关）。"""
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db._conn.execute(
            "UPDATE qmx_robot_instance SET status='DISABLED' WHERE group_id=?",
            (GROUP,))
        tmp_db._conn.commit()
        backend = FakeBackend()
        reply = bind_handler.handle_command(
            commands.parse("绑定"), _ctx(tmp_db, backend))
        assert "已停用" in reply
        assert tmp_db.get_group_config(GROUP) is None

    def test_no_dead_admin_gate_left_in_source(self):
        """源码里 ⛔ 不能残留名单判定（防以后又把 fail-closed 加回来时忘了文档）。"""
        src = (BOT / "app" / "handlers" / "bind.py").read_text(encoding="utf-8")
        assert "_is_bind_admin(ctx)" not in src
        assert "NOT_ADMIN_TEXT" not in src.replace("原 `NOT_ADMIN_TEXT`", "")


# ---------- 问题 3：待绑定只留一个绑定入口 ----------

class TestSingleBindAction:
    def test_edit_button_hidden_for_wait_bind(self):
        src = (SRC / "RobotInstance.tsx").read_text(encoding="utf-8")
        # 「修改」按钮必须被 !isWait 包住（否则待绑定行会同时出现两个绑定入口）
        assert re.search(
            r"\{!isWait && \(\s*<button\s+onClick=\{\(\) => setEditing", src), \
            "待绑定行仍然同时显示「修改」与「确认绑定」"
        assert "确认绑定" in src

    def test_confirm_bind_prefills_group_name(self):
        """确认绑定弹窗预填群名（群内已录过的不用管理员重打）。"""
        src = (SRC / "RobotInstance.tsx").read_text(encoding="utf-8")
        assert "setBindName(row.display_name || row.group_name || '')" in src


# ---------- 授权管理员：搜索框交互（原生 select -> 显式结果列表） ----------

class TestGrantSearch:
    @pytest.fixture()
    def src(self):
        return (SRC / "GroupManagement.tsx").read_text(encoding="utf-8")

    def test_no_native_select_for_grant_target(self, src):
        """⭐ 根因：过滤一个原生 <select> 的 option，0 命中时输入框「毫无反应」。"""
        assert "value={grantTarget}" not in src

    def test_three_empty_states_present(self, src):
        assert "该群还没有应交成员" in src             # 新群没有成员（用户实测场景）
        assert "没有匹配" in src                       # 关键字 0 命中
        assert "都已是群管理员或已停用" in src          # 全部授权完了

    def test_keyword_matching_ignores_case_and_spaces(self, src):
        assert "grantDigits" in src
        assert "toLowerCase()" in src

    def test_stale_selection_is_cleared(self, src):
        """选中项被过滤掉必须立刻清空，⛔ 不能「看不见却已选中」。"""
        assert "grantOptionIds" in src
        assert "setGrantTarget('')" in src

    def test_switching_group_resets_search(self, src):
        assert re.search(r"useEffect\(\(\) => \{\s*setGrantQuery\(''\);", src)

    def test_member_page_shortcut(self, src):
        """空态里给可执行动作：直接跳到「群成员」页添加。"""
        assert "去「群成员」页添加" in src
        assert "setSubView('members')" in src
