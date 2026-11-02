# -*- coding: utf-8 -*-
"""D51（2026-11-02 用户报障）：凌晨被机器人刷屏的兜底保护。

事故
----
2026-10-31（周六）03:09-04:13，云中台群被机器人连发 60 条回复
（「今天不是本群打卡日…」+「未识别该指令」），而正常一整天只有 7~9 条。

查明
----
- 平台**只回推 @ 了机器人的消息**（用户 09:00 实测：不 @ 的消息平台不推，
  我们零日志）。所以不是「机器人不判 @」。
- 载荷没有消息 ID / 时间戳（2026-10-19 探针：`textMsg{content, tenantId}`），
  **无法按消息去重** —— 平台把历史消息重推一遍，我们完全无感。
- 61 条分散在一小时内（约 1 条/分钟），**每分钟限流挡不住**。

对策
----
1. **夜间静默**（主力）：[00:00, 07:00) 一律不回复、不跑业务，只记日志。
2. **每群每分钟上限 20 条**（白天兜底）：超出的静默 + WARNING。
   阈值不能太小 —— 早上 08:50 提醒后全群集中打卡，5 条/分钟会误伤。
"""

from __future__ import annotations

from datetime import datetime

from app.web import server as web


class TestQuietHours:
    def test_inside_window(self):
        for hour in (0, 1, 3, 6):
            assert web.in_quiet_hours(datetime(2026, 11, 2, hour, 30)) is True

    def test_boundaries(self):
        assert web.in_quiet_hours(datetime(2026, 11, 2, 0, 0)) is True
        assert web.in_quiet_hours(datetime(2026, 11, 2, 7, 0)) is False
        assert web.in_quiet_hours(datetime(2026, 11, 2, 23, 59)) is False

    def test_incident_window_covered(self):
        """事发时刻 03:09 必须落在静默窗口内。"""
        assert web.in_quiet_hours(datetime(2026, 10, 31, 3, 9)) is True
        assert web.in_quiet_hours(datetime(2026, 10, 31, 4, 13)) is True

    def test_workday_untouched(self):
        assert web.in_quiet_hours(datetime(2026, 11, 2, 8, 50)) is False
        assert web.in_quiet_hours(datetime(2026, 11, 2, 17, 30)) is False


class TestReplyRateLimit:
    def _reset(self):
        web._REPLY_WINDOW.clear()

    def test_allows_up_to_limit(self):
        self._reset()
        t0 = datetime(2026, 11, 2, 10, 0, 0)
        for i in range(web.REPLY_RATE_LIMIT_PER_MIN):
            assert web.reply_rate_ok("G1", t0.replace(second=i)) is True

    def test_blocks_over_limit(self):
        self._reset()
        t0 = datetime(2026, 11, 2, 10, 0, 0)
        for i in range(web.REPLY_RATE_LIMIT_PER_MIN):
            web.reply_rate_ok("G1", t0.replace(second=i))
        assert web.reply_rate_ok("G1", t0.replace(second=30)) is False

    def test_window_slides(self):
        self._reset()
        t0 = datetime(2026, 11, 2, 10, 0, 0)
        for i in range(web.REPLY_RATE_LIMIT_PER_MIN):
            web.reply_rate_ok("G1", t0.replace(second=i))
        assert web.reply_rate_ok("G1", t0.replace(second=30)) is False
        # 61 秒后窗口滑空
        assert web.reply_rate_ok(
            "G1", datetime(2026, 11, 2, 10, 1, 1)) is True

    def test_groups_independent(self):
        self._reset()
        t0 = datetime(2026, 11, 2, 10, 0, 0)
        for i in range(web.REPLY_RATE_LIMIT_PER_MIN):
            web.reply_rate_ok("GA", t0.replace(second=i))
        assert web.reply_rate_ok("GA", t0.replace(second=30)) is False
        assert web.reply_rate_ok("GB", t0.replace(second=31)) is True

    def test_limit_is_not_too_tight_for_morning_rush(self):
        """阈值必须明显大于「早上集中打卡」的量（旧值 5 会误伤）。"""
        assert web.REPLY_RATE_LIMIT_PER_MIN >= 15
