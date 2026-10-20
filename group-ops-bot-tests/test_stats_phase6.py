# -*- coding: utf-8 -*-
"""⭐ 统计导出测试（2026-10-16 用户指令三.1：一键导出全部业务；
2026-10-19 用户指令：白名单单权化 ADMIN_ALL + 授权对象限群成员）。

与 test_reminder_stats.py::TestExportWorkbook 的分工：那里测纯构建器；
本文件测命令入口（鉴权 / 周期 / 查询失败 / 落盘 / 动态字段）与端到端行为。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from app import commands
from app.handlers import stats as stats_handler
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
SUNDAY = datetime(2026, 10, 18, 23, 0, tzinfo=TZ)  # 2026-W42 周日


def ctx_at(tmp_db, backend, now=AM):
    return make_ctx(tmp_db, backend, make_config(Path(str(tmp_db.db_path)).parent),
                    now=now)


def _run(tmp_db, backend, text="导出", now=AM):
    parsed = commands.parse(text)
    assert parsed.result == commands.RESULT_DISPATCH, text
    return stats_handler.handle_command(parsed, ctx_at(tmp_db, backend, now))


def _grant_all(tmp_db, phone=PHONE):
    tmp_db.upsert_whitelist(GROUP, phone)


def _export_file(tmp_db):
    """导出落盘位置。

    ⭐ 2026-10-20：失败话术不再携带服务器绝对路径（内部信息不进群聊），
    因此测试改为直接看数据目录的 exports/（文件仍照常生成）。
    """
    exports = Path(str(tmp_db.db_path)).parent / "exports"
    files = sorted(exports.glob("*.xlsx"))
    assert files, "no export file written to %s" % exports
    return files[-1]


class TestExportCommandShape:
    def test_export_is_single_command(self):
        """唯一入口 = 「导出」两个字（⛔ 旧四命令已废）。"""
        parsed = commands.parse("导出")
        assert parsed.result == commands.RESULT_DISPATCH
        assert parsed.command == commands.CMD_EXPORT

    def test_old_export_commands_are_unknown(self):
        for text in ("导出打卡", "导出日报", "导出周报", "导出报数",
                     "统计"):
            assert commands.parse(text).result == commands.RESULT_UNKNOWN

    def test_all_three_businesses_queried(self, tmp_db):
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        _run(tmp_db, backend)
        names = [c[0] for c in backend.calls]
        assert "checkin_query_records" in names
        assert "summary_query_records" in names
        assert "report_query_records" in names


class TestExportPermission:
    def test_no_permission_rejected(self, tmp_db):
        enable_all(tmp_db)
        reply = _run(tmp_db, FakeBackend())
        assert "导出权限" in reply
        # ⛔ 未触达任何查询
        assert FakeBackend().calls == []

    def test_legacy_split_permissions_not_honored(self, tmp_db):
        """单权化：历史遗留的旧三权行一律不命中（⛔ 不再有部分权限概念）。

        旧行只能来自 v8 迁移前的存量数据（upsert 已拒绝旧权限值），
        故用原生 SQL 插入模拟。
        """
        enable_all(tmp_db)
        raw = tmp_db._conn
        for perm in ("EXPORT_CHECKIN", "EXPORT_SUMMARY", "EXPORT_REPORT"):
            raw.execute(
                "INSERT OR IGNORE INTO export_whitelist(group_id, user_id,"
                " permission_type, updated_at) VALUES(?,?,?,?)",
                (GROUP, PHONE, perm, "2026-10-15 16:35:00+08:00"))
        raw.commit()
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        assert "导出权限" in reply
        assert backend.calls == []

    def test_switch_off_does_not_block_export(self, tmp_db):
        """业务开关关闭不影响导出（导的是历史账，§4.7）。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, biz_switches={"checkin": False})
        backend = FakeBackend()
        assert "发送到群聊失败" in _run(tmp_db, backend)  # 开关不影响导出


class TestExportPeriod:
    def test_daily_group_uses_daily_key_for_all(self, tmp_db):
        """日报群：总结 / 打卡 / 报数都取当日（2026-10-13）。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        _run(tmp_db, backend)
        for name, kwargs in backend.calls:
            assert kwargs["period_key"] == "2026-10-13", name

    def test_weekly_group_uses_iso_week_for_summary_only(self, tmp_db):
        """⭐ 周报群：总结取 ISO 周，打卡 / 报数仍是当日。

        （日报与周报同为总结内容，只是数据量不同。）
        """
        enable_all(tmp_db, period_type="weekly")
        _grant_all(tmp_db)
        backend = FakeBackend()
        _run(tmp_db, backend, now=SUNDAY)
        keys = {name: kwargs["period_key"] for name, kwargs in backend.calls}
        assert keys["summary_query_records"] == "2026-W42"
        assert keys["checkin_query_records"] == "2026-10-18"
        assert keys["report_query_records"] == "2026-10-18"


class TestExportFailureModes:
    def test_query_failure_no_fake_file(self, tmp_db):
        """查询失败：⛔ 不猜数据、不生成空表冒充成功。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        backend.raise_on.add("checkin_query_records")
        reply = _run(tmp_db, backend)
        assert "查询失败" in reply
        exports = Path(str(tmp_db.db_path)).parent / "exports"
        assert not exports.exists() or not list(exports.glob("*.xlsx"))

    def test_export_writes_single_named_file(self, tmp_db):
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        path = _export_file(tmp_db)
        assert path.exists()
        assert path.name == "export_all_G100_20261013.xlsx"

    def test_workbook_has_three_sheets(self, tmp_db):
        from io import BytesIO
        from openpyxl import load_workbook
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        wb = load_workbook(BytesIO(_export_file(tmp_db).read_bytes()))
        assert wb.sheetnames == ["打卡", "总结", "报数"]


class TestExportReportDynamicFields:
    def test_only_filled_fields_become_columns(self, tmp_db):
        """端到端：报数导出只展示有人填的字段（快照列名 + 单位）。"""
        from io import BytesIO
        from openpyxl import load_workbook
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        backend.results["report_query_records"] = {"records": [
            {"phone": PHONE, "name": "甲", "role": "开发",
             "items": [{"field_id": 2, "field_name": "代码行数",
                        "quantity": 500, "unit": "行"}]},
            {"phone": "22644212510", "name": "乙", "role": "测试",
             "items": [{"field_id": 3, "field_name": "bug发现数",
                        "quantity": 7, "unit": None}]}]}
        reply = _run(tmp_db, backend)
        wb = load_workbook(BytesIO(_export_file(tmp_db).read_bytes()))
        header = [c.value for c in wb["报数"][1]]
        # 字段 1 没人填 -> ⛔ 不出列；两人各填不同字段 -> 两列都在
        assert header == ["手机号", "姓名", "角色", "代码行数（行）",
                          "bug发现数"]
        assert wb["报数"].max_row == 3  # 表头 + 两人


class TestExportFileSend:
    def test_send_file_blocked_is_reported_honestly(self, tmp_db):
        """平台文件能力未确认（D12）：如实告知，⛔ 不把落盘当已送达。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        # 2026-10-20：真实通道已接通，但本用例的 sender 没有文件通道 ->
        # 如实告知失败，⛔ 不谎称已发送，⛔ 不贴服务器路径
        assert "发送到群聊失败" in reply
        assert "本地路径" not in reply
        assert "已发送" not in reply