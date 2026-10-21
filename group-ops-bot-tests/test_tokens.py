# -*- coding: utf-8 -*-
"""令牌测试（§2.4 绑定 / 最小授权 / 过期 / 一次提交 / 安全重试边界）。"""

from datetime import datetime, timedelta

import pytest

from app import period, tokens
from app.period import TZ
from app.tokens import (
    RejectReason, check_authorization, generate_token_value, new_record,
    token_id_of,
)

PHONE = "13800138009"
GROUP = "G100"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)


def make_token(biz="report", period_key="2026-10-13", session=None):
    value = generate_token_value()
    record = new_record(value, PHONE, GROUP, biz, period_key, session, now=NOW)
    return value, record


class TestGenerate:
    def test_high_entropy_unique(self):
        values = {generate_token_value() for _ in range(100)}
        assert len(values) == 100

    def test_token_id_is_hash_not_plaintext(self):
        value = generate_token_value()
        tid = token_id_of(value)
        assert tid != value
        assert len(tid) == 64  # sha256 hex
        assert value not in tid


class TestAuthorization:
    def test_valid(self):
        value, record = make_token()
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-13", None, now=NOW) is None

    def test_not_found_when_missing(self):
        assert check_authorization(None, "x", PHONE, GROUP, "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.NOT_FOUND

    def test_tampered_value_rejected(self):
        value, record = make_token()
        assert check_authorization(record, value + "x", PHONE, GROUP, "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.NOT_FOUND

    def test_wrong_user_rejected(self):
        value, record = make_token()
        assert check_authorization(record, value, "13800138010", GROUP, "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.WRONG_USER

    def test_wrong_group_rejected(self):
        value, record = make_token()
        assert check_authorization(record, value, PHONE, "G200", "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.WRONG_GROUP

    def test_wrong_biz_rejected(self):
        value, record = make_token()
        assert check_authorization(record, value, PHONE, GROUP, "checkin",
                                   "2026-10-13", None, now=NOW) == RejectReason.WRONG_BIZ

    def test_wrong_period_rejected(self):
        value, record = make_token()
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-14", None, now=NOW) == RejectReason.WRONG_PERIOD

    def test_checkin_session_binding(self):
        value, record = make_token(biz="checkin", session="am")
        assert check_authorization(record, value, PHONE, GROUP, "checkin",
                                   "2026-10-13", "am", now=NOW) is None
        assert check_authorization(record, value, PHONE, GROUP, "checkin",
                                   "2026-10-13", "pm", now=NOW) == RejectReason.WRONG_PERIOD

    def test_expires_at_30min_cap(self):
        """第七阶段收紧：令牌有效期 = 签发后 30 分钟硬上限。

        ⛔ 不再按「周期截止 + 24h」算 —— 即便周期窗口更长，也一律封顶到 30 分钟。
        """
        value, record = make_token()
        assert record.expires_at == NOW + timedelta(minutes=30)
        before = datetime(2026, 10, 13, 10, 29, tzinfo=TZ)
        at_edge = datetime(2026, 10, 13, 10, 30, tzinfo=TZ)
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-13", None, now=before) is None
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-13", None, now=at_edge) == RejectReason.EXPIRED

    def test_explicit_longer_expiry_is_capped(self):
        """调用方传更长的 expires_at 也无效（fail-closed，不可绕过）。"""
        value = generate_token_value()
        record = new_record(
            value, PHONE, GROUP, "report", "2026-10-13", None, now=NOW,
            expires_at=NOW + timedelta(hours=24))
        assert record.expires_at == NOW + timedelta(minutes=30)

    def test_consumed_rejected(self):
        from app.tokens import TokenState
        value, record = make_token()
        record.state = TokenState.CONSUMED.value
        record.consumed_request_id = "req-1"
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.ALREADY_CONSUMED

    def test_revoked_rejected_as_not_found(self):
        from app.tokens import TokenState
        value, record = make_token()
        record.state = TokenState.REVOKED.value
        assert check_authorization(record, value, PHONE, GROUP, "report",
                                   "2026-10-13", None, now=NOW) == RejectReason.NOT_FOUND
