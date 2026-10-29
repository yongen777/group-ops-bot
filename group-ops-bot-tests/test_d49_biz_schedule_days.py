# -*- coding: utf-8 -*-
"""D49（2026-10-29 用户指令）：每个业务单独配置执行日（周几）。

背景
----
核查发现打卡 / 总结 / 报数的**业务执行**与**提醒**都没有任何工作日判断，
周末照常催（10-24 周六、10-25 周日三业务提醒全部发出）。用户要求改成
「按业务单独配周期」：工作日（周一~周五）/ 指定星期几 / 每天。

口径
----
- 配置落在 `group_config.reminder_rules[biz]["days"]`，ISO 编号 1=周一 … 7=周日
- **默认全周**（[1..7]）—— 与改动前行为完全一致，⛔ 不做隐式行为变更
- 非执行日：业务命令直接拦（⛔ 不挂会话、⛔ 不写库），自动提醒整个业务跳过
- 空数组 / 全非法 -> 回落全周（宁多催一天，也不能因配置失误让业务失联）
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import commands
from app.admin.api import _validate_reminder_rules
from app.handlers import base as base_handler
from app.handlers import reminder as reminder_handler
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config
from test_d26_reminder_source import setup as setup_slots
from test_reminder_stats import ReminderBackend, setup_group_with_required

SATURDAY = datetime(2026, 10, 31, 10, 0, tzinfo=TZ)      # 周六
SUNDAY = datetime(2026, 11, 1, 10, 0, tzinfo=TZ)        # 周日
MONDAY = datetime(2026, 11, 2, 10, 0, tzinfo=TZ)        # 周一


def _payload(text, phone=PHONE):
    return {"group_id": GROUP, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}


def _bot(tmp_db, now=SATURDAY):
    enable_all(tmp_db)
    sender = RecordingSender()
    app = create_app(state=tmp_db, backend=FakeBackend(), sender=sender,
                     config=make_config(Path(str(tmp_db.db_path)).parent),
                     now_fn=lambda: now)
    return TestClient(app), sender


def _say(client, text):
    resp = client.post("/api/callback", json=_payload(text))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _last(sender):
    return sender.sent[-1]["text"] if sender.sent else ""


def _set_days(tmp_db, **per_biz):
    """per_biz: checkin=[1,2,3,4,5] 这种。"""
    rules = {biz: {"enabled": True, "times": [], "text": "", "days": days}
             for biz, days in per_biz.items()}
    tmp_db.upsert_group_config(GROUP, reminder_rules=json.dumps(rules))


class TestNormalizeDays:
    def test_default_all_week(self):
        assert base_handler.normalize_days(None) == (1, 2, 3, 4, 5, 6, 7)
        assert base_handler.normalize_days([]) == (1, 2, 3, 4, 5, 6, 7)
        assert base_handler.normalize_days(["x", 9, 0]) == (1, 2, 3, 4, 5, 6, 7)

    def test_dedupe_and_sort(self):
        assert base_handler.normalize_days([5, 1, 5, "3"]) == (1, 3, 5)

    def test_days_text(self):
        f = base_handler.days_text
        assert f([1, 2, 3, 4, 5, 6, 7]) == "每天"
        assert f([1, 2, 3, 4, 5]) == "周一至周五"
        assert f([6, 7]) == "周六、周日"
        assert f([1, 3, 5]) == "周一、周三、周五"


class TestBizDaysFromConfig:
    def test_missing_config_means_all_week(self, tmp_db):
        enable_all(tmp_db)
        row = tmp_db.get_group_config(GROUP)
        assert base_handler.biz_days_of_row(row, "checkin") == (1, 2, 3, 4, 5, 6, 7)

    def test_configured_weekdays_only(self, tmp_db):
        enable_all(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5])
        row = tmp_db.get_group_config(GROUP)
        assert base_handler.biz_days_of_row(row, "checkin") == (1, 2, 3, 4, 5)
        # 未配置的业务仍全周
        assert base_handler.biz_days_of_row(row, "report") == (1, 2, 3, 4, 5, 6, 7)


class TestBusinessBlockedOnNonScheduledDay:
    def test_checkin_blocked_on_saturday(self, tmp_db):
        client, sender = _bot(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5])
        _say(client, "打卡")
        text = _last(sender)
        assert "今天不是本群打卡日" in text
        assert "周一至周五" in text
        assert tmp_db.peek_pending_session(GROUP, PHONE, "checkin") is None

    def test_summary_blocked_on_sunday(self, tmp_db):
        client, sender = _bot(tmp_db, now=SUNDAY)
        _set_days(tmp_db, summary=[1, 2, 3, 4, 5])
        _say(client, "总结")
        assert "今天不是本群总结日" in _last(sender)
        # ⛔ 不能挂会话（否则第二个字被当成总结内容）
        assert tmp_db.peek_pending_session(GROUP, PHONE, "summary") is None

    def test_report_blocked_on_weekend(self, tmp_db):
        client, sender = _bot(tmp_db)
        _set_days(tmp_db, report=[6, 7])   # 只周末可报
        _say(client, "报数")            # 今天周六 -> 可以
        assert "今天不是本群报数日" not in _last(sender)

    def test_three_biz_independent(self, tmp_db):
        """三个业务各自独立：周六只开打卡，则总结/报数被拦、打卡正常。"""
        client, sender = _bot(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5, 6, 7],
                  summary=[1, 2, 3, 4, 5], report=[1, 2, 3, 4, 5])
        _say(client, "总结")
        assert "今天不是本群总结日" in _last(sender)
        _say(client, "报数")
        assert "今天不是本群报数日" in _last(sender)
        _say(client, "打卡")
        assert "今天不是本群打卡日" not in _last(sender)

    def test_weekday_allows(self, tmp_db):
        client, sender = _bot(tmp_db, now=MONDAY)
        _set_days(tmp_db, summary=[1, 2, 3, 4, 5])
        _say(client, "总结")
        assert "今天不是本群总结日" not in _last(sender)
        assert tmp_db.peek_pending_session(GROUP, PHONE, "summary") is not None


class TestReminderSkippedOnNonScheduledDay:
    def _run(self, tmp_db, now, biz, manual=False):
        pushed = []

        def push(group_id, text):
            pushed.append(text)
            return True, ""

        row = tmp_db.get_group_config(GROUP)
        return pushed, reminder_handler.run_group(
            tmp_db, ReminderBackend(), push, row, now, biz=biz, manual=manual)

    def test_auto_skipped_on_saturday(self, tmp_db):
        """周六 14:30（pm 的 remind_at）打卡提醒整业务跳过。

        ⚠️ 必须用 d26 的 slot 夹具：helpers.enable_all 的默认 remind_at 是
        00:01 / 12:01，14:30 根本不在窗口内，测出来会是「没触发」而不是
        「被 days 跳过」——两者结果都是 0 条，但性质不同。
        """
        setup_slots(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5])
        pushed, summary = self._run(
            tmp_db, SATURDAY.replace(hour=14, minute=30), biz="checkin")
        assert pushed == []
        assert summary.get("skipped_days") == ["checkin"]
        assert summary["targets"] == 0

    def test_auto_runs_on_weekday(self, tmp_db):
        setup_slots(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5])
        pushed, summary = self._run(
            tmp_db, MONDAY.replace(hour=14, minute=30), biz="checkin")
        assert "skipped_days" not in summary
        assert summary["targets"] > 0

    def test_manual_also_respects_days(self, tmp_db):
        """手动催同样尊重执行日（否则周末点了照样发，等于没配）。"""
        setup_slots(tmp_db)
        _set_days(tmp_db, checkin=[1, 2, 3, 4, 5])
        pushed, summary = self._run(
            tmp_db, SATURDAY.replace(hour=14, minute=30), biz="checkin",
            manual=True)
        assert pushed == []
        assert summary.get("skipped_days") == ["checkin"]


class TestAdminValidation:
    def test_accepts_days(self):
        out = _validate_reminder_rules(
            {"checkin": {"enabled": True, "times": ["09:00"], "days": [1, 2, 3]}})
        assert out["checkin"]["days"] == [1, 2, 3]

    def test_missing_days_keeps_previous(self):
        current = json.dumps({"checkin": {"enabled": True, "times": ["09:00"],
                                         "text": "", "days": [1, 2, 3, 4, 5]}})
        out = _validate_reminder_rules({"checkin": {"enabled": False}},
                                       current_raw=current)
        assert out["checkin"]["days"] == [1, 2, 3, 4, 5]
        assert out["checkin"]["enabled"] is False

    def test_rejects_empty_days(self):
        with pytest.raises(HTTPException) as exc:
            _validate_reminder_rules({"checkin": {"days": []}})
        assert exc.value.status_code == 400

    def test_rejects_out_of_range(self):
        with pytest.raises(HTTPException) as exc:
            _validate_reminder_rules({"report": {"days": [0, 8]}})
        assert exc.value.status_code == 400

    def test_rejects_non_list(self):
        with pytest.raises(HTTPException) as exc:
            _validate_reminder_rules({"summary": {"days": "mon-fri"}})
        assert exc.value.status_code == 400
