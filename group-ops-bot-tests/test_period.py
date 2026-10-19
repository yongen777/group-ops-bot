# -*- coding: utf-8 -*-
"""周期键与时段测试（§2.3 边界必须全覆盖）。"""

from datetime import datetime

from app import period
from app.period import TZ


def dt(h, m, s=0):
    return datetime(2026, 10, 13, h, m, s, tzinfo=TZ)  # 2026-10-13 是周二


class TestCheckinSession:
    def test_am_before_noon(self):
        assert period.checkin_session(dt(9, 0)) == "am"
        assert period.checkin_session(dt(11, 59, 59)) == "am"

    def test_pm_from_noon(self):
        assert period.checkin_session(dt(12, 0)) == "pm"
        assert period.checkin_session(dt(14, 50)) == "pm"
        assert period.checkin_session(dt(23, 59)) == "pm"

    def test_midnight_is_am(self):
        assert period.checkin_session(dt(0, 0)) == "am"


class TestPeriodKeys:
    def test_daily_key(self):
        assert period.daily_key(dt(15, 0)) == "2026-10-13"

    def test_iso_week_key_tuesday(self):
        # 2026-10-13 是周二，ISO 2026-W42（周一 10-12 为该周始）
        assert period.iso_week_key(dt(15, 0)) == "2026-W42"

    def test_iso_week_monday_starts_week(self):
        # 周一 2026-10-12 与周日 2026-10-18 同属 2026-W42（§2.3：周一为一周开始）
        assert period.iso_week_key(datetime(2026, 10, 12, tzinfo=TZ)) == "2026-W42"
        assert period.iso_week_key(datetime(2026, 10, 18, tzinfo=TZ)) == "2026-W42"

    def test_iso_week_year_boundary(self):
        # 2027-01-01 是周五，属 ISO 2026 年第 53 周（跨年必须用 ISO 年）
        assert period.iso_week_key(datetime(2027, 1, 1, tzinfo=TZ)) == "2026-W53"

    def test_daily_and_weekly_keys_never_collide(self):
        # §2.3：日报与周报互不覆盖 —— 周期键形态不同，天然不冲突
        assert period.daily_key(dt(15, 0)) != period.iso_week_key(dt(15, 0))

    def test_period_key_dispatch(self):
        assert period.period_key("daily", dt(15, 0)) == "2026-10-13"
        assert period.period_key("weekly", dt(15, 0)) == "2026-W42"
        try:
            period.period_key("monthly", dt(15, 0))
            assert False, "unknown period_type must raise"
        except ValueError:
            pass


class TestPeriodEnd:
    def test_checkin_am_ends_at_noon(self):
        end = period.period_end("checkin", "2026-10-13", "am")
        assert (end.hour, end.minute) == (12, 0)

    def test_checkin_pm_ends_at_day_end(self):
        end = period.period_end("checkin", "2026-10-13", "pm")
        assert (end.hour, end.minute, end.second) == (23, 59, 59)

    def test_daily_biz_default_cutoff(self):
        end = period.period_end("report", "2026-10-13")
        assert (end.hour, end.minute) == (19, 0)

    def test_weekly_ends_next_monday(self):
        # 2026-W42 的下周一为 2026-10-19 00:00
        end = period.period_end("summary", "2026-W42")
        assert end.strftime("%Y-%m-%d %H:%M") == "2026-10-19 00:00"

    def test_token_expiry_is_cutoff_plus_24h(self):
        from datetime import timedelta
        end = period.period_end("report", "2026-10-13")
        expiry = period.token_expiry("report", "2026-10-13", None)
        assert expiry - end == timedelta(hours=24)
