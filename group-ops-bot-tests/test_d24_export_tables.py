# -*- coding: utf-8 -*-
"""⭐ 2026-10-20 用户指令（D24）：导出表格结构收口 + 后台统一 xlsx。

用户九条里的六条落到这里：
- 打卡表：同一成员的多个时段**合并成一行**；凭证列只说「已提交 / 未提交」
  （⛔ 不显示图片文件名）；群配「免图直提」时凭证列整列消失；移除「录入来源」
- 总结表：移除「录入来源」
- 报数表：移除「录入来源」；已提交但某字段没填 -> 「未选择」
- 底色：⛔ 不整行铺粉（用户实测「首行正常、后面全是粉底」），只染状态单元格
- 后台统计导出：统一 xlsx，CSV 能力下线（400，⛔ 不静默降级）

⛔ 与 test_d23_export（三文件 / 全员视角 / 回执）不重复。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from app import commands
from app.handlers import stats as stats_handler
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx

OTHER = "13800138011"
AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)


def _setup(tmp_db, require_image: bool = True, now=AM):
    enable_all(tmp_db)
    tmp_db.upsert_whitelist(GROUP, PHONE)
    tmp_db.upsert_group_config(GROUP,
                              checkin_require_image=1 if require_image else 0)
    for idx, phone in enumerate((PHONE, OTHER)):
        tmp_db.upsert_member_task(GROUP, phone, "成员%d" % (idx + 1))
    return make_ctx(tmp_db, FakeBackend(),
                    make_config(Path(str(tmp_db.db_path)).parent), now=now)


def _export(tmp_db, ctx, biz: str, records: dict = None):
    """跑一次「导出」，返回指定业务的 sheet。"""
    backend = FakeBackend()
    for key, value in (records or {}).items():
        backend.results[key] = value
    ctx.backend = backend
    stats_handler.handle_command(commands.parse("导出"), ctx)
    files = sorted((Path(str(tmp_db.db_path)).parent / "exports")
                   .glob(biz + "_*.xlsx"))
    assert files, "no %s file" % biz
    return load_workbook(files[-1]).active


def _header(ws):
    return [c.value for c in ws[1]]


def _rows(ws):
    return [list(r) for r in ws.iter_rows(min_row=2, values_only=True)]


class TestCheckinTable:
    """用户指令 4：合并时段行、凭证状态化、免图删列、去录入来源。"""

    RECORDS = {"checkin_query_records": {"records": [
        {"phone": PHONE, "name": "成员1", "session": "am",
         "checkin_time": "2026-10-13T09:10:00", "status": "LATE",
         "entry_source": "admin", "image_name": "现场照片-20261013.jpg"},
        {"phone": PHONE, "name": "成员1", "session": "pm",
         "checkin_time": "2026-10-13T14:05:00", "status": "NORMAL",
         "entry_source": "user", "image_name": "b.png"},
    ]}}

    def test_one_row_per_member_with_slot_columns(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "checkin", self.RECORDS)
        assert _header(ws) == ["序号", "手机号", "姓名", "上午", "下午",
                               "凭证图片", "备注"]
        rows = _rows(ws)
        assert len(rows) == 2                     # 2 人 = 2 行
        mine = [r for r in rows if r[1] == PHONE][0]
        assert mine[3] == "09:10:00（迟到、补录）"  # 时间 + 标注，仍在同一格
        assert mine[4] == "14:05:00"

    def test_image_column_shows_state_not_filename(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "checkin", self.RECORDS)
        mine = [r for r in _rows(ws) if r[1] == PHONE][0]
        assert mine[5] == "已提交"
        flat = "".join(str(c.value) for row in ws.iter_rows()
                       for c in row if c.value is not None)
        assert "现场照片" not in flat and ".png" not in flat

    def test_no_entry_source_column(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "checkin", self.RECORDS)
        assert "录入来源" not in _header(ws)
        flat = "".join(str(c.value) for row in ws.iter_rows()
                       for c in row if c.value is not None)
        assert "群员" not in flat and "管理员" not in flat

    def test_image_column_removed_when_group_needs_no_photo(self, tmp_db):
        """⭐ 免图直提的群：凭证列整列不出现（⛔ 留一列全「未提交」误导）。"""
        ws = _export(tmp_db, _setup(tmp_db, require_image=False), "checkin",
                     self.RECORDS)
        assert _header(ws) == ["序号", "手机号", "姓名", "上午", "下午",
                               "备注"]

    def test_not_started_state_wording(self, tmp_db):
        """下午还没到点 -> 「未到打卡时间段」（⛔ 不是含糊的「未到时段」）。"""
        ws = _export(tmp_db, _setup(tmp_db), "checkin")
        rows = _rows(ws)
        assert any(r[4] == "未到打卡时间段" for r in rows), rows


class TestSummaryAndReportColumns:
    """用户指令 5 / 6：移除「录入来源」；报数空格显式「未选择」。"""

    SUMMARY = {"summary_query_records": {"records": [
        {"phone": PHONE, "name": "成员1", "period_type": "daily",
         "period_key": "2026-10-13", "content": "完成联调",
         "updated_time": "2026-10-13T18:00:00", "entry_source": "user"}]}}

    REPORT = {"report_query_records": {"records": [
        {"phone": PHONE, "name": "成员1",
         "created_time": "2026-10-13T18:10:00", "entry_source": "admin",
         "items": [{"field_id": 1, "field_name": "bug修复数",
                    "quantity": 3, "unit": None},
                   {"field_id": 2, "field_name": "测试用例数",
                    "quantity": None, "unit": "个"}]}]}}

    def test_summary_has_no_entry_source(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "summary", self.SUMMARY)
        assert _header(ws) == ["序号", "提交状态", "手机号", "姓名",
                               "周期类型", "周期", "提交时间", "内容", "备注"]
        flat = "".join(str(c.value) for row in ws.iter_rows()
                       for c in row if c.value is not None)
        assert "群员" not in flat and "管理员" not in flat

    def test_report_has_no_entry_source(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "report", self.REPORT)
        assert _header(ws) == ["序号", "提交状态", "手机号", "姓名",
                               "提交时间", "bug修复数", "测试用例数（个）",
                               "备注"]

    def test_report_missing_field_shows_unselected(self, tmp_db):
        """已提交但某字段没填 -> 「未选择」（⛔ 留空白让人猜是没填还是没这字段）。"""
        ws = _export(tmp_db, _setup(tmp_db), "report", self.REPORT)
        mine = [r for r in _rows(ws) if r[2] == PHONE][0]
        assert mine[5] == 3          # 有值：数字（Excel 可求和）
        assert mine[6] == "未选择"

    def test_report_unsubmitted_row_keeps_empty_cells(self, tmp_db):
        """整条没交：字段列留空（「未选择」只用于已提交的行）。"""
        ws = _export(tmp_db, _setup(tmp_db), "report", self.REPORT)
        other = [r for r in _rows(ws) if r[2] == OTHER][0]
        assert other[1] == "未提交"
        assert other[5] in (None, "") and other[6] in (None, "")


class TestRowFillScope:
    """用户指令 7：⛔ 不整行铺粉 —— 首行正常、后面全粉看着像表格坏了。"""

    FULL = {
        "checkin_query_records": {"records": [
            {"phone": PHONE, "name": "成员1", "session": "am",
             "checkin_time": "2026-10-13T09:10:00", "status": "NORMAL",
             "entry_source": "user", "image_name": "a.png"},
            {"phone": PHONE, "name": "成员1", "session": "pm",
             "checkin_time": "2026-10-13T14:05:00", "status": "NORMAL",
             "entry_source": "user", "image_name": "b.png"}]},
        "summary_query_records": {"records": [
            {"phone": PHONE, "name": "成员1", "period_type": "daily",
             "content": "完成联调", "updated_time": "2026-10-13T18:00:00",
             "entry_source": "user"}]},
        "report_query_records": {"records": [
            {"phone": PHONE, "name": "成员1",
             "created_time": "2026-10-13T18:10:00", "entry_source": "user",
             "items": [{"field_id": 1, "field_name": "bug修复数",
                        "quantity": 3, "unit": None}]}]},
    }

    def test_completed_rows_are_completely_white(self, tmp_db):
        """已提交的行必须整行白底 —— 这就是用户报的「后面全粉」的反例。"""
        ctx = _setup(tmp_db)
        for biz in ("summary", "report"):
            ws = _export(tmp_db, ctx, biz, self.FULL)
            done = [r for r in ws.iter_rows(min_row=2)
                    if r[1].value == "已提交"]
            assert done, biz
            for row in done:
                for cell in row:
                    assert str(cell.fill.fgColor.rgb).endswith("00000000"), \
                        (biz, cell.coordinate, cell.value)

    def test_only_status_cells_are_filled(self, tmp_db):
        ctx = _setup(tmp_db)
        for biz in ("checkin", "summary", "report"):
            ws = _export(tmp_db, ctx, biz)
            filled_rows = set()
            for row in ws.iter_rows(min_row=2):
                for cell in row:
                    pink = str(cell.fill.fgColor.rgb).endswith("FDE8E8")
                    if pink:
                        filled_rows.add(cell.row)
            # 每个被染色的行，其「非状态列」必须保持白底
            for row_idx in filled_rows:
                row = list(ws[row_idx])
                whites = [c for c in row
                          if str(c.fill.fgColor.rgb).endswith("00000000")]
                assert whites, (biz, row_idx)

    def test_header_fill_survives(self, tmp_db):
        ws = _export(tmp_db, _setup(tmp_db), "checkin")
        assert all(str(c.fill.fgColor.rgb).endswith("1F4E79") for c in ws[1])


class TestAdminExportFormat:
    """用户指令 2：后台统计导出统一 xlsx，CSV 下线。"""

    def test_router_source_defaults_to_xlsx(self):
        """前端不传 format 时也必须是 xlsx（⛔ 默认值留在 csv 会直接 400）。

        路由定义在 `create_admin_router` 闭包内，拿不到函数签名，
        所以用源码级守卫（改动默认值会当场红）。
        """
        src = (Path(__file__).resolve().parent.parent / "group-ops-bot" /
               "app" / "admin" / "api.py").read_text(encoding="utf-8")
        assert 'format: str = "xlsx"' in src
        assert 'format: str = "csv"' not in src
