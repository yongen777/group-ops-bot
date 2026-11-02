# -*- coding: utf-8 -*-
"""D51 v2（2026-11-02 用户指令）：同一条消息只处理一次。

用户口径
--------
「一天如果是用户在晚上或者 @ 触发机器人回复一对一的，这就是正确实现的效果。
**不是一对一这种才是问题**。」所以：
- ⛔ 不做夜间静默（晚上有人在用就是正常需求）
- ⛔ 不做总量限流（一对一多少条都由用户决定）
- ✅ 只修「同一条消息被重复处理」

铁证
----
2026-10-31 03:09-04:13 该群 60 条回复里，「39 字节」那一种出现 **38 次**，
而群里只有 **18 人** —— 回复数大于群人数，必然有人被重复回复。
说明平台把同一条消息重复投递了（载荷无消息 ID / 时间戳，无法按 ID 识别），
而我们没有按内容幂等。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app.web import server as web

T0 = datetime(2026, 10, 31, 3, 9, 0)


def _reset():
    web._REPLY_SEEN.clear()


class TestDuplicateMessage:
    def test_first_time_not_duplicate(self):
        _reset()
        assert web.is_duplicate_message("G1", "13800000001", "打卡", T0) is False

    def test_same_message_within_window_is_duplicate(self):
        """同群同人同文本 10 分钟内再来 -> 判重（这一条恢复一对一）。"""
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        # 窗口 2 秒：1 秒内算同一条消息的重投
        assert web.is_duplicate_message(
            "G1", "13800000001", "打卡", T0 + timedelta(seconds=1)) is True

    def test_after_window_allowed_again(self):
        """超过窗口允许再次处理（用户隔一阵又发一次是正常需求）。"""
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        assert web.is_duplicate_message(
            "G1", "13800000001", "打卡",
            T0 + timedelta(seconds=web.IDEMPOTENT_WINDOW_SECONDS + 1)) is False

    def test_different_phone_not_duplicate(self):
        """不同的人发同样的话 -> 各回各的（⛔ 不能互相吃掉）。"""
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        assert web.is_duplicate_message(
            "G1", "13800000002", "打卡", T0) is False

    def test_different_text_not_duplicate(self):
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        assert web.is_duplicate_message(
            "G1", "13800000001", "总结", T0) is False

    def test_different_group_not_duplicate(self):
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        assert web.is_duplicate_message(
            "G2", "13800000001", "打卡", T0) is False

    def test_whitespace_normalized(self):
        _reset()
        web.is_duplicate_message("G1", "13800000001", "打卡", T0)
        assert web.is_duplicate_message(
            "G1", "13800000001", "  打卡  ", T0) is True

    def test_missing_phone_never_blocked(self):
        """拿不到手机号时⛔ 不能判重（否则会把正常消息吞掉）。"""
        _reset()
        assert web.is_duplicate_message("G1", "", "打卡", T0) is False
        assert web.is_duplicate_message("", "13800000001", "打卡", T0) is False

    def test_night_time_is_not_special(self):
        """⛔ 夜间不再有特殊处理：凌晨与白天行为完全一致。"""
        _reset()
        assert web.is_duplicate_message(
            "G1", "13800000001", "打卡", T0) is False
        assert web.is_duplicate_message(
            "G1", "13800000002", "打卡", datetime(2026, 10, 31, 14, 30)) is False

    def test_quiet_hours_and_rate_limit_removed(self):
        """⛔ 夜间静默 / 每分钟限流已按用户指令彻底移除。"""
        assert not hasattr(web, "in_quiet_hours")
        assert not hasattr(web, "reply_rate_ok")
        assert not hasattr(web, "REPLY_RATE_LIMIT_PER_MIN")
        assert not hasattr(web, "QUIET_START_HOUR")
        assert not hasattr(web, "_REPLY_WINDOW")
