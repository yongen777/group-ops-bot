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
from doubles.fakes import FakeBackend, RecordingSender
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


def _export_files(tmp_db, suffix):
    exports = Path(str(tmp_db.db_path)).parent / "exports"
    return sorted(exports.glob("*" + suffix))


def _export_file(tmp_db, biz="checkin"):
    """按业务取落盘的 xlsx。

    ⭐ 2026-10-20 用户指令：打卡 / 总结 / 报数 **各一个文件**，
    文件名 = 业务英文 + 时间（checkin_20261013_1000.xlsx 等）。
    """
    files = _export_files(tmp_db, ".xlsx")
    matched = [f for f in files if f.name.startswith(biz + "_")]
    assert matched, "no %s xlsx written to %s" % (
        biz, Path(str(tmp_db.db_path)).parent / "exports")
    return matched[-1]


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

    def test_export_writes_three_named_files(self, tmp_db):
        """⭐ 2026-10-20 用户指令：三个业务各一个文件，业务英文 + 时间命名。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        names = sorted(p.name for p in _export_files(tmp_db, ".xlsx"))
        assert names == ["checkin_20261013_1000.xlsx",
                         "report_20261013_1000.xlsx",
                         "summary_20261013_1000.xlsx"]

    def test_each_file_has_exactly_one_sheet(self, tmp_db):
        """一个文件一个 sheet（⛔ 不再合成多 sheet —— 在线预览只看第一张）。"""
        from io import BytesIO
        from openpyxl import load_workbook
        enable_all(tmp_db)
        _grant_all(tmp_db)
        backend = FakeBackend()
        reply = _run(tmp_db, backend)
        for biz, label in (("checkin", "打卡"), ("summary", "总结"),
                           ("report", "报数")):
            wb = load_workbook(BytesIO(_export_file(tmp_db, biz).read_bytes()))
            assert wb.sheetnames == [label], biz


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
            {"phone": "13800138010", "name": "乙", "role": "测试",
             "items": [{"field_id": 3, "field_name": "bug发现数",
                        "quantity": 7, "unit": None}]}]}
        reply = _run(tmp_db, backend)
        wb = load_workbook(BytesIO(_export_file(tmp_db, "report").read_bytes()))
        header = [c.value for c in wb["报数"][1]]
        # 字段 1 没人填 -> ⛔ 不出列；两人各填不同字段 -> 两列都在
        # ⭐ 2026-10-20 用户指令：报数表移除「录入来源」列
        assert header == ["序号", "提交状态", "手机号", "姓名", "提交时间",
                          "代码行数（行）", "bug发现数", "备注"]
        assert wb["报数"].max_row == 3  # 表头 + 两人
        assert wb["报数"]["B2"].value == "已提交"


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

class TestExportThreeFiles:
    """⭐ 2026-10-20 用户指令：**三个业务各一个 XLSX 文件**，单独命名。

    理由：① 量子密信电脑端打不开 CSV；② 合成一个多 sheet 的 xlsx，
    在线预览只显示第一张表，用户会以为「只有打卡」。
    """

    def _backend(self):
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [
            {"phone": PHONE, "name": "甲", "session": "am",
             "checkin_time": "09:10:00", "status": "NORMAL",
             "entry_source": "user", "image_name": "a.jpg"}]}
        backend.results["summary_query_records"] = {"records": [
            {"phone": PHONE, "name": "甲", "period_type": "daily",
             "content": "第一行\n第二行", "updated_time": "20:00:00",
             "entry_source": "user"}]}
        backend.results["report_query_records"] = {"records": [
            {"phone": PHONE, "name": "甲", "role": "开发",
             "created_time": "20:01:00", "entry_source": "user",
             "items": [{"field_id": 1, "field_name": "bug修复数",
                        "quantity": 3.0, "unit": None}]}]}
        return backend

    def test_three_files_sent_one_per_business(self, tmp_db):
        enable_all(tmp_db)
        _grant_all(tmp_db)
        sender = RecordingSender()
        sender.file_ok = True
        ctx = ctx_at(tmp_db, self._backend())
        ctx.sender = sender
        reply = stats_handler.handle_command(commands.parse("导出"), ctx)
        assert [f["filename"] for f in sender.files_sent] == [
            "checkin_20261013_1000.xlsx", "summary_20261013_1000.xlsx",
            "report_20261013_1000.xlsx"]
        # ⛔ 不再产生任何 csv；回执 = 用户指定的固定文案
        assert not _export_files(tmp_db, ".csv")
        assert reply == stats_handler.EXPORT_SENT_TEXT

    def test_each_business_data_lands_in_its_own_file(self, tmp_db):
        """三份文件各含对应业务的数据（⛔ 不再「表里只有打卡」）。"""
        from io import BytesIO
        from openpyxl import load_workbook
        enable_all(tmp_db)
        _grant_all(tmp_db)
        _run(tmp_db, self._backend())

        checkin = load_workbook(BytesIO(
            _export_file(tmp_db, "checkin").read_bytes()))["打卡"]
        # ⭐ 打卡表：序号/手机号/姓名/上午/下午/凭证/备注（一人一行）
        assert checkin["B2"].value == PHONE           # 手机号
        assert checkin["C2"].value == "甲"            # 姓名
        assert checkin["D1"].value == "上午"          # 表头 = 本群配置的时段名
        assert checkin["D2"].value == "09:10:00"      # 该时段的打卡时刻

        summary = load_workbook(BytesIO(
            _export_file(tmp_db, "summary").read_bytes()))["总结"]
        # 多行正文压成一行（⛔ 不把换行带进单元格）；⛔ 已移除「录入来源」列
        assert summary["H2"].value == "第一行 第二行"

        report = load_workbook(BytesIO(
            _export_file(tmp_db, "report").read_bytes()))["报数"]
        assert [c.value for c in report[1]][-2] == "bug修复数"
        assert report["F2"].value == 3

    def test_partial_failure_is_reported_honestly(self, tmp_db):
        """三个文件里有一个发失败：如实报「2 成功 1 失败」，⛔ 不谎报全部成功。"""
        enable_all(tmp_db)
        _grant_all(tmp_db)

        class RejectReport(RecordingSender):
            def send_file(self, group_id, path, filename=None):
                if Path(str(path)).name.startswith("report_"):
                    return False, "platform rejects report file"
                return super(RejectReport, self).send_file(
                    group_id, path, filename=filename)

        sender = RejectReport()
        sender.file_ok = True
        ctx = ctx_at(tmp_db, self._backend())
        ctx.sender = sender
        reply = stats_handler.handle_command(commands.parse("导出"), ctx)
        assert "2 个文件发送成功、1 个发送失败" in reply

    def test_all_failed_reports_failure(self, tmp_db):
        enable_all(tmp_db)
        _grant_all(tmp_db)
        sender = RecordingSender()      # file_ok=False：通道未接通
        ctx = ctx_at(tmp_db, self._backend())
        ctx.sender = sender
        reply = stats_handler.handle_command(commands.parse("导出"), ctx)
        assert reply == stats_handler.EXPORT_SEND_FAILED_TEXT
        assert "发送到群聊失败" in reply

    def test_labels_and_time_format_match_admin_export(self):
        """群内 CSV 的中文标签 / 时间形态与后台导出同口径。

        标签表是两处各放一份（避免 handlers -> admin 的导入环），
        这里用断言锁相等：⛔ 改一处漏另一处会当场红。
        """
        from app.admin import stats_export as admin_stats
        assert stats_handler.SESSION_LABEL == admin_stats.SESSION_LABEL
        assert stats_handler.STATUS_LABEL == admin_stats.STATUS_LABEL
        assert stats_handler.SOURCE_LABEL == admin_stats.SOURCE_LABEL
        assert stats_handler._fmt_time("2026-10-20T09:39:58.879000") == \
            "2026-10-20 09:39:58"
        assert stats_handler._fmt_time(None) == ""
