# -*- coding: utf-8 -*-
"""D50（2026-10-29）：非执行日不出「全员未提交」的表。

用户口径：「只要我周日没有开通业务，就没有业务，我催什么？没有业务就没有
催收的东西。」因此非执行日在**看板与导出**里也必须是「休息」，而不是
「未完成」—— 否则导出周末会得到一张全员浅红表。
"""

from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app.admin import board as board_mod
from app.admin.stats_export import resolve_period_key
from app.admin.stats_export import resolve_period_key
from app.handlers import stats as stats_mod
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, enable_all, make_config
from pathlib import Path

SAT = "2026-10-31"      # 周六
MON = "2026-11-02"      # 周一
ROSTER = [("13800000001", "张三"), ("13800000002", "李四")]


def _setup(tmp_db, **per_biz):
    enable_all(tmp_db)
    for phone, name in ROSTER:
        tmp_db.upsert_member_task(GROUP, phone, nickname=name, role="开发")
    if per_biz:
        rules = {biz: {"enabled": True, "times": [], "text": "", "days": days}
                 for biz, days in per_biz.items()}
        tmp_db.upsert_group_config(GROUP, reminder_rules=json.dumps(rules))


def _board(tmp_db, date_str=SAT):
    return board_mod.build_board(tmp_db, FakeBackend(), GROUP, date_str,
                                 today=date_str)


class TestPeriodScheduled:
    def test_daily_weekday(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5])
        assert stats_mod.period_scheduled(tmp_db, GROUP, "checkin", MON) is True

    def test_daily_weekend(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5])
        assert stats_mod.period_scheduled(tmp_db, GROUP, "checkin", SAT) is False

    def test_unconfigured_is_always(self, tmp_db):
        _setup(tmp_db)
        assert stats_mod.period_scheduled(tmp_db, GROUP, "checkin", SAT) is True

    def test_weekly_any_day_counts(self, tmp_db):
        """周报：本周有任意一天在执行日就不算休息（周末导周报不能被拦）。"""
        _setup(tmp_db, summary=[1, 2, 3, 4, 5])
        assert stats_mod.period_scheduled(
            tmp_db, GROUP, "summary", "2026-W44") is True

    def test_bad_key_fails_open(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5])
        assert stats_mod.period_scheduled(tmp_db, GROUP, "checkin", "") is True


class TestBoardOffDay:
    def test_all_biz_off(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5],
               summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        board = _board(tmp_db)
        assert board["scheduled"] == {"checkin": False, "summary": False,
                                      "report": False}
        for member in board["members"]:
            assert member["checkin_status"] == "off"
            assert member["summary_status"] == "off"
            assert member["report_status"] == "off"
        # ⛔ 不能算成未完成
        assert board["counts"]["checkin_done"] == 0
        assert board["counts"]["summary_done"] == 0

    def test_partial_biz_off(self, tmp_db):
        """只有总结/报数停周末，打卡仍每天 —— 打卡照常算。"""
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5, 6, 7],
               summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        board = _board(tmp_db)
        assert board["scheduled"]["checkin"] is True
        assert board["scheduled"]["summary"] is False
        member = board["members"][0]
        assert member["summary_status"] == "off"
        assert member["checkin_status"] != "off"

    def test_weekday_normal(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5],
               summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        board = _board(tmp_db, MON)
        assert all(board["scheduled"].values())
        assert board["members"][0]["checkin_status"] != "off"


class TestBoardRowsOffDay:
    def test_off_text_and_dash(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5],
               summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        board = _board(tmp_db)
        headers, rows = board_mod.board_rows(board)
        assert rows, "应当仍有成员行"
        row = rows[0]
        # 打卡时间列写「—」而不是「未打卡」
        assert "—" in row
        assert "未打卡" not in row
        # 达标列是「今日休息」
        assert row[-1] == "今日休息"

    def test_weekday_still_uses_未打卡(self, tmp_db):
        _setup(tmp_db)
        board = _board(tmp_db, MON)
        _headers, rows = board_mod.board_rows(board)
        assert "未打卡" in rows[0]


class TestExportOffDay:
    def _ctx(self, tmp_db):
        from app.handlers.base import HandlerContext
        from datetime import datetime
        from helpers import TZ
        return HandlerContext(
            group_id=GROUP, phone=PHONE, name="张三", platform_user_id=None,
            text="导出", state=tmp_db, backend=FakeBackend(),
            config=make_config(Path(str(tmp_db.db_path)).parent),
            now=datetime(2026, 10, 31, 10, 0, tzinfo=TZ), sender=None)

    def test_all_off_returns_hint_without_files(self, tmp_db):
        _setup(tmp_db, checkin=[1, 2, 3, 4, 5],
               summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        tmp_db.upsert_whitelist(GROUP, PHONE, stats_mod.EXPORT_PERMISSION)
        ctx = self._ctx(tmp_db)
        reply = stats_mod.handle_command(None, ctx)
        assert reply == stats_mod.EXPORT_OFF_DAY_TEXT
        exports = Path(ctx.config.data_dir) / "exports"
        assert not exports.exists() or not list(exports.glob("*.xlsx"))


class TestAdminExportValidation:
    def test_single_off_day_rejected(self, tmp_db):
        """后台单业务导出：非执行日 400（不生成浅红表）。"""
        _setup(tmp_db, summary=[1, 2, 3, 4, 5])
        period_k = resolve_period_key(tmp_db, GROUP, "summary", SAT)
        assert stats_mod.period_scheduled(
            tmp_db, GROUP, "summary", period_k) is False
