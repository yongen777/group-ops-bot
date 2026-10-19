# -*- coding: utf-8 -*-
"""报数去角色化 + 动态字段 + 成功 @ 回执（用户指令 2026-10-16 一/二/三/四）。

覆盖：
一、去角色绑定：H5 不再收/返角色；role 服务端派生（应交名单优先，
   缺失回落 DEFAULT_ROLE「成员」）；⛔ 不按角色过滤字段（全局共享）
二、交互重构的结构性锁定：submit_from_h5 签名无 role 形参；
   H5 页为「+ 添加报数字段」动态逐行、⛔ 无角色输入
三、成功后群内 @ 通知（真实手机号）；重复提交不重复打扰；
   推送失败不改写 A/B/C 归类
四、成功话术与打卡/总结统一（✅ …已成功完成。）
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from pathlib import Path

from app import commands
from app.handlers import report as report_handler
from app.handlers.base import KIND_A, KIND_B
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx, \
    setup_fields

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)

# ⛔ 不 split：链接话术可能带尾注，正则只取令牌本体
_TOKEN_RE = re.compile(r"#t=([A-Za-z0-9_-]+)")


def ctx_at(tmp_db, backend, now, sender=None, phone=PHONE):
    from pathlib import Path as _Path
    return make_ctx(tmp_db, backend,
                    make_config(_Path(tmp_db.db_path).parent), now=now,
                    phone=phone, sender=sender)


def issue(tmp_db, sender=None, now=AM, hhmm="23:59"):
    """开群 + 字段池 + 宽松截止，签发报数令牌。"""
    enable_all(tmp_db)
    setup_fields(tmp_db)
    tmp_db.upsert_group_config(GROUP, daily_cutoff=hhmm)
    backend = FakeBackend()
    ctx = ctx_at(tmp_db, backend, now, sender=sender)
    reply = report_handler.handle_command(commands.parse("报数"), ctx)
    token = _TOKEN_RE.search(reply).group(1)
    return backend, ctx, token


# ---------- 一、去角色绑定 ----------

class TestRoleRemoved:
    def test_signature_has_no_role_param(self):
        """结构性锁定：⛔ submit_from_h5 不再接收客户端 role。"""
        params = list(inspect.signature(
            report_handler.submit_from_h5).parameters)
        assert params == ["ctx", "value", "items"]

    def test_token_info_has_no_role_key(self, tmp_db):
        _backend, ctx, token = issue(tmp_db)
        info = report_handler.token_info(ctx, token)
        assert info["ok"] is True
        assert "role" not in info  # ⛔ 不再把角色透给 H5

    def test_role_derived_from_member_task(self, tmp_db):
        """应交名单角色优先（服务端派生，⛔ 不问用户）。"""
        backend, ctx, token = issue(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, role="测试")
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 2}])
        assert result.kind == KIND_A
        _name, kwargs = backend.calls[-1]
        assert kwargs["role"] == "测试"

    def test_role_falls_back_to_default(self, tmp_db):
        """无应交名单配置 -> 回落 DEFAULT_ROLE，⛔ 不因此拒提交。"""
        backend, ctx, token = issue(tmp_db)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 2}])
        assert result.kind == KIND_A
        _name, kwargs = backend.calls[-1]
        assert kwargs["role"] == report_handler.DEFAULT_ROLE == "成员"

    def test_fields_shared_regardless_of_roles_column(self, tmp_db):
        """全局字段共享：roles 列配「测试」的字段，开发用户也看得见。"""
        enable_all(tmp_db)
        tmp_db.upsert_field(GROUP, 1, "bug修复数", None, ["开发"], True, 1)
        tmp_db.upsert_field(GROUP, 2, "用例数", "个", ["测试"], True, 2)
        tmp_db.upsert_group_config(GROUP, daily_cutoff="23:59")
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, AM)
        reply = report_handler.handle_command(commands.parse("报数"), ctx)
        token = _TOKEN_RE.search(reply).group(1)
        info = report_handler.token_info(ctx, token)
        assert [f["field_name"] for f in info["fields"]] == \
            ["bug修复数", "用例数"]


# ---------- 二、动态逐个添加（结构性锁定） ----------

class TestDynamicAddUi:
    def test_report_page_has_add_button_and_no_role_input(self):
        """H5 页：「+ 添加报数字段」动态入口 + ⛔ 无角色输入行。"""
        import app
        tpl = (Path(app.__file__).parent / "h5templates" /
               "report.html").read_text(encoding="utf-8")
        assert "+ 添加报数字段" in tpl
        assert 'id="role"' not in tpl  # ⛔ 角色绑定已去除
        assert "字段加载中" in tpl      # 占位存在（由 JS 在终态替换）

    def test_admin_fields_page_has_no_role_config(self):
        """后台字段池（React SPA）：⛔ 不再有「适用角色」配置（全局共享）。"""
        import app
        src = (Path(app.__file__).parent.parent / "admin-ui" / "src" /
               "components" / "system" / "GroupManagement.tsx"
               ).read_text(encoding="utf-8")
        assert "适用角色" not in src
        assert "fd-roles" not in src

    def test_admin_ui_no_longer_sends_roles(self):
        import app
        src = (Path(app.__file__).parent.parent / "admin-ui" / "src" /
               "components" / "system" / "GroupManagement.tsx"
               ).read_text(encoding="utf-8")
        assert "roles:" not in src  # 字段池新增只送 名称 / 单位
        assert "field_name" in src


# ---------- 三、成功后群内 @ 通知 ----------

class TestSuccessNotify:
    def test_success_pushes_mention(self, tmp_db):
        """A 类成功 -> 群内 @ 推送（真实手机号 + 统一话术）。"""
        sender = RecordingSender()
        sender.push_ok = True
        _backend, ctx, token = issue(tmp_db, sender=sender)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        assert len(sender.pushed) == 1
        assert sender.pushed[0]["mention_phones"] == [PHONE]
        assert sender.pushed[0]["text"] == \
            "✅ 今日工作报数已成功完成。"

    def test_duplicate_does_not_re_notify(self, tmp_db):
        """安全重试（duplicate）-> ⛔ 不重复 @ 打扰。"""
        sender = RecordingSender()
        sender.push_ok = True
        _backend, ctx, token = issue(tmp_db, sender=sender)
        first = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        second = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 5}])
        assert first.kind == KIND_A and second.kind == KIND_A
        assert second.duplicate is True
        assert len(sender.pushed) == 1

    def test_push_failure_keeps_success_message(self, tmp_db):
        """推送未接通 -> best-effort：仍归 A，H5 页面照常显示成功话术。"""
        sender = RecordingSender()  # push_ok 默认 False（未接通）
        _backend, ctx, token = issue(tmp_db, sender=sender)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        assert result.message == "✅ 今日工作报数已成功完成。"
        assert sender.pushed == []  # ⛔ 不伪造已发送

    def test_no_sender_does_not_break(self, tmp_db):
        """未织 sender（⛔ 测试替身 / 旧端口）-> 不炸、仍归 A。"""
        _backend, ctx, token = issue(tmp_db, sender=None)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A

    def test_rejected_submit_does_not_push(self, tmp_db):
        """B 类拒绝 -> ⛔ 不推送任何群内消息。"""
        sender = RecordingSender()
        sender.push_ok = True
        _backend, ctx, token = issue(tmp_db, sender=sender)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 999, "quantity": 1}])
        assert result.kind == KIND_B
        assert sender.pushed == []
