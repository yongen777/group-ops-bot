# -*- coding: utf-8 -*-
"""D48（2026-10-28 用户反馈）：手动提醒后，自动调度同窗口内⛔ 不再补发。

现象
----
2026-10-27 17:39 用户在后台**分别**点了一次「总结」和「报数」一键提醒，
群里却收到 4 条（总结 2 条、报数 2 条）。审计日志证明他只点了各一次：

    reminder.run  summary  17:39:08  targets=11 new=11 sent=11
    reminder.run  report   17:39:21  targets=11 new=11 sent=11

但推送日志里还有：

    17:39:30  outbound_probe text_len=170   <- 自动调度 tick
    17:39:30  outbound_probe text_len=170
    reminder tick: {'groups': 3, 'targets': 22, 'new': 22, 'sent': 22}

根因
----
`record_reminder_plan` 的 D26 口径：自动调度遇到 `status='sent' AND
source='manual'` 就「接管重发一次」（否则手动催过会把定时提醒整场吃掉，
2026-10-20 真实事故）。但它**只看 source 不看时间**——手动 17:39:08 发完，
22 秒后定时窗口到了就被判成「还没自动发过」，于是同一条内容再发一遍。

修复
----
给接管加时间下限 `MANUAL_TAKEOVER_MIN_MINUTES = 30`：
手动那行已过去 ≥30 分钟才允许接管。
- 同窗口内手动点完 -> 自动不再重复（本用例）
- 上午手动、下午定时（间隔数小时）-> 照常补发，D26 修复不受影响
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.state import MANUAL_TAKEOVER_MIN_MINUTES
from helpers import GROUP, PHONE, TZ
from test_d26_reminder_source import T_1430, run, setup

PERIOD = "2026-10-27"
T_MANUAL = datetime(2026, 10, 27, 17, 39, 8, tzinfo=TZ)


def _plan(tmp_db, now, source="auto", resend_sent=False, biz="summary"):
    return tmp_db.record_reminder_plan(
        group_id=GROUP, biz=biz, period_key=PERIOD, phone=PHONE,
        session=None, now=now, resend_sent=resend_sent, source=source)


def _manual_then_auto_after(tmp_db, seconds):
    """手动发一条并标记 sent，然后在 seconds 秒后走自动调度门控。"""
    assert _plan(tmp_db, T_MANUAL, source="manual") is True
    tmp_db.mark_reminder_sent(GROUP, "summary", PERIOD, PHONE, None,
                              now=T_MANUAL)
    return _plan(tmp_db, T_MANUAL + timedelta(seconds=seconds), source="auto")


class TestTakeoverNeedsTimeGap:
    def test_same_window_22_seconds_no_takeover(self, tmp_db):
        """用户实测的 22 秒间隔：⛔ 自动不能再发一遍。"""
        assert _manual_then_auto_after(tmp_db, 22) is False

    def test_just_under_threshold_no_takeover(self, tmp_db):
        minutes = MANUAL_TAKEOVER_MIN_MINUTES - 1
        assert _manual_then_auto_after(tmp_db, minutes * 60) is False

    def test_over_threshold_takes_over(self, tmp_db):
        """超过阈值仍要接管 —— D26「手动催过不吃掉定时提醒」必须保留。"""
        minutes = MANUAL_TAKEOVER_MIN_MINUTES + 1
        assert _manual_then_auto_after(tmp_db, minutes * 60) is True

    def test_auto_after_takeover_is_not_repeated(self, tmp_db):
        """接管发过一次之后，同窗口再跑自动⛔ 不再发。"""
        minutes = MANUAL_TAKEOVER_MIN_MINUTES + 1
        assert _manual_then_auto_after(tmp_db, minutes * 60) is True
        tmp_db.mark_reminder_sent(GROUP, "summary", PERIOD, PHONE, None,
                                  now=T_MANUAL + timedelta(minutes=minutes))
        assert _plan(tmp_db, T_MANUAL + timedelta(minutes=minutes, seconds=30),
                     source="auto") is False

    def test_manual_cooldown_unchanged(self, tmp_db):
        """手动连点仍受 5 分钟冷却（⛔ 没被这次改动放开）。"""
        assert _plan(tmp_db, T_MANUAL, source="manual") is True
        tmp_db.mark_reminder_sent(GROUP, "summary", PERIOD, PHONE, None,
                                  now=T_MANUAL)
        assert _plan(tmp_db, T_MANUAL + timedelta(minutes=2),
                     source="manual") is False

    def test_report_biz_same_rule(self, tmp_db):
        """报数走同一条门控（用户是总结、报数各重复一次）。"""
        assert _plan(tmp_db, T_MANUAL, source="manual", biz="report") is True
        tmp_db.mark_reminder_sent(GROUP, "report", PERIOD, PHONE, None,
                                  now=T_MANUAL)
        assert _plan(tmp_db, T_MANUAL + timedelta(seconds=22),
                     source="auto", biz="report") is False


class TestRunGroupNoDuplicate:
    """整轮级别：手动一轮 + 22 秒后自动一轮，群里只应有一条消息。"""

    def test_checkin_manual_then_auto_same_window(self, tmp_db):
        setup(tmp_db)     # pm 提醒时间 14:30
        pushed_manual, _ = run(tmp_db, T_1430, manual=True)
        assert len(pushed_manual) == 1

        pushed_auto, summary = run(
            tmp_db, T_1430.replace(second=22), manual=False)
        assert pushed_auto == [], "手动点完 22 秒后自动又发了一遍"
        assert summary["new"] == 0

    def test_next_window_still_fires(self, tmp_db):
        """跨时段不受影响：手动催过之后，下一次定时（间隔 > 阈值）照发。

        ⚠️ 时间必须**落在 pm 窗口（14:00~14:50）内**，否则自动调度会被
        时段门控拦下，测出来就是假失败（首版用 14:30+31min=15:01 踩过）。
        """
        setup(tmp_db)
        pushed_manual, _ = run(
            tmp_db, T_1430.replace(hour=14, minute=0), manual=True)
        assert len(pushed_manual) == 1
        # 14:00 手动 -> 14:31 自动（间隔 31 分钟 > 阈值，且仍在窗口内）
        pushed_auto, summary = run(
            tmp_db, T_1430.replace(hour=14, minute=MANUAL_TAKEOVER_MIN_MINUTES + 1),
            manual=False)
        assert len(pushed_auto) == 1, "跨时段定时提醒被手动提醒吃掉了"
        assert summary["sent"] > 0
