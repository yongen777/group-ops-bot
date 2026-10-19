# -*- coding: utf-8 -*-
"""周期键与时段计算（§2.3）。

硬约束（已确认方案，不得擅自变更）：
- 时区集中配置 Asia/Shanghai，禁止分散硬编码（§2.3）
- 打卡：自然日 + 时段，12:00 前 = am，12:00 起 = pm，由服务端时间判定，
  不由用户自选、不猜、不模糊归类
- 日报：自然日；周报：ISO 周，周一为一周开始
- 报数：自然日
- 日报与周报互不覆盖：周期键形态不同（YYYY-MM-DD vs YYYY-Www），天然不冲突

period_key 对 MCP Server 是不透明字符串（§4.11.0），语义只存在于本模块。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:  # Python 3.9+ 标准库；3.11 起 zoneinfo 稳定可用
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - 理论上不会走到
    raise RuntimeError("zoneinfo unavailable; Python 3.9+ required")

TZ_NAME = "Asia/Shanghai"
TZ = ZoneInfo(TZ_NAME)

SESSION_AM = "am"
SESSION_PM = "pm"
# 12:00 分界（§2.3）：此时刻起归 pm
SESSION_BOUNDARY_HOUR = 12

PERIOD_DAILY = "daily"
PERIOD_WEEKLY = "weekly"

# 周期截止时刻（令牌有效期锚点，§2.4：令牌有效到本业务周期截止后 24 小时）。
# 默认值可被群级配置覆盖（§4.7 提醒规则）；此处是全局默认，不得散落到业务代码里。
DEFAULT_DAILY_CUTOFF = (19, 0)        # 日报 / 报数默认截止 19:00（Q-BIZ5 默认随总结）
DEFAULT_AM_CUTOFF = (12, 0)           # 上午打卡时段截止 12:00（与时段分界一致）
DEFAULT_PM_CUTOFF = (23, 59, 59)      # 下午打卡时段截止当日 23:59:59
WEEKLY_CUTOFF_WEEKDAY = 1             # ISO 周截止：下周一 00:00


def now(tz: ZoneInfo | None = None) -> datetime:
    """服务端当前时间（默认 Asia/Shanghai）。"""
    return datetime.now(tz or TZ)


def parse_hhmm(value: str) -> tuple:
    """'HH:MM' -> (hour, minute)。非法返回 None（调用方决定 fail 行为）。"""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def daily_key(dt: datetime | None = None) -> str:
    """自然日周期键 YYYY-MM-DD。"""
    return (dt or now()).strftime("%Y-%m-%d")


def iso_week_key(dt: datetime | None = None) -> str:
    """ISO 周周期键 YYYY-Www（周一为一周开始，§2.3）。

    注意用 isocalendar() 的 ISO 年，不用日历年——12 月底 / 1 月初
    存在 ISO 年与日历年不一致的情况（如 2027-01-01 属 2026-W53）。
    """
    d = dt or now()
    iso_year, iso_week, _ = d.isocalendar()
    return "%04d-W%02d" % (iso_year, iso_week)


def checkin_session(dt: datetime | None = None) -> str:
    """打卡时段：12:00 前 am，12:00 起 pm（§2.3）。

    边界：11:59:59 -> am；12:00:00 -> pm。必须为边界写测试。
    """
    d = dt or now()
    return SESSION_AM if d.hour < SESSION_BOUNDARY_HOUR else SESSION_PM


def parse_daily_key(key: str) -> datetime:
    """把 YYYY-MM-DD 解析回 TZ 感知的 datetime（当日起点）。"""
    return datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=TZ)


def period_key(period_type: str, dt: datetime | None = None) -> str:
    """按周期类型取周期键。"""
    if period_type == PERIOD_DAILY:
        return daily_key(dt)
    if period_type == PERIOD_WEEKLY:
        return iso_week_key(dt)
    raise ValueError("unknown period_type: %r" % (period_type,))


def period_end(
    biz: str,
    period_key_str: str,
    session: str | None = None,
    daily_cutoff: tuple[int, int] = DEFAULT_DAILY_CUTOFF,
) -> datetime:
    """周期截止时刻（TZ 感知）。令牌有效期 = 该时刻 + 24h（§2.4）。

    默认截止点：
    - 打卡 am：当日 12:00（与时段分界一致）
    - 打卡 pm：当日 23:59:59
    - 日报 / 报数：当日 daily_cutoff（默认 19:00，可群级配置）
    - 周报：该 ISO 周的下周一 00:00
    """
    if biz == "checkin":
        day = parse_daily_key(period_key_str)
        if session == SESSION_AM:
            return day.replace(hour=DEFAULT_AM_CUTOFF[0], minute=DEFAULT_AM_CUTOFF[1])
        if session == SESSION_PM:
            h, m, s = DEFAULT_PM_CUTOFF
            return day.replace(hour=h, minute=m, second=s)
        raise ValueError("checkin period_end requires session am/pm")
    # 周期键形态判定（不依赖 biz）：形如 2026-W42 为 ISO 周，否则为自然日
    if _is_iso_week_key(period_key_str):
        year = int(period_key_str[:4])
        week = int(period_key_str[6:])
        monday = datetime.fromisocalendar(year, week, WEEKLY_CUTOFF_WEEKDAY).replace(tzinfo=TZ)
        # 周报截止 = 该 ISO 周的**下周一** 00:00
        return monday + timedelta(days=7)
    if _is_daily_key(period_key_str):
        day = parse_daily_key(period_key_str)
        return day.replace(hour=daily_cutoff[0], minute=daily_cutoff[1])
    raise ValueError("cannot resolve period_end for biz=%r key=%r session=%r"
                     % (biz, period_key_str, session))


def _is_daily_key(key: str) -> bool:
    if len(key) != 10 or key[4] != "-" or key[7] != "-":
        return False
    try:
        parse_daily_key(key)
        return True
    except ValueError:
        return False


def _is_iso_week_key(key: str) -> bool:
    """形如 2026-W41（含 ISO 年，可能为 4 位；周两位）。"""
    parts = key.split("-W")
    if len(parts) != 2:
        return False
    year_s, week_s = parts
    if not (year_s.isdigit() and week_s.isdigit()):
        return False
    if len(week_s) != 2 or not (1 <= int(week_s) <= 53):
        return False
    return 1 <= datetime.fromisocalendar(int(year_s), int(week_s), 1).isocalendar()[1] <= 53


def token_expiry(
    biz: str,
    period_key_str: str,
    session: str | None,
    daily_cutoff: tuple[int, int] = DEFAULT_DAILY_CUTOFF,
) -> datetime:
    """令牌过期时刻 = 周期截止 + 24 小时（§2.4）。"""
    return period_end(biz, period_key_str, session, daily_cutoff) + timedelta(hours=24)
