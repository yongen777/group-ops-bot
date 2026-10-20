# -*- coding: utf-8 -*-
"""H5 安全第七阶段测试（用户指令：30 分钟有效期 / 绑定四要素 /
规范拒绝话术 / 成功后立即失效）。

口径来源：用户第七阶段指令覆盖 §2.4「到本业务周期截止后 24 小时为止」，
冲突已记为待确认项 Q-H5；代码以 30 分钟硬上限为准。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import commands
from app import tokens as tokens_mod
from app.handlers import checkin as checkin_handler
from app.handlers import report as report_handler
from app.handlers.base import KIND_A, KIND_B
from app.tokens import RejectReason, token_id_of
from doubles.fakes import FakeBackend
from helpers import (GROUP, PHONE, TZ, enable_all, extract_token, make_config,
                     make_ctx, make_upload, setup_fields)

OTHER_PHONE = "13900000002"
OTHER_GROUP = "G999"

BASE = datetime(2026, 10, 13, 9, 0, tzinfo=TZ)          # 签发时刻
WITHIN = BASE + timedelta(minutes=29)                    # 窗口内
AT_EDGE = BASE + timedelta(minutes=30)                   # 边界（到期）
BEYOND = BASE + timedelta(minutes=31)                    # 超窗


def ctx_at(tmp_db, backend, now, phone=PHONE, group_id=GROUP):
    from pathlib import Path
    return make_ctx(tmp_db, backend,
                    make_config(Path(tmp_db.db_path).parent), now=now,
                    phone=phone, group_id=group_id)


def checkin_setup(tmp_db):
    """打卡：需图模式（require_image=1）才会签发 H5 链接；
    截止放宽到深夜，避免截止因素干扰本文件的有效期/绑定判定。
    """
    enable_all(tmp_db)
    tmp_db.upsert_group_config(
        GROUP, checkin_deadlines={"am": "23:00", "pm": "23:59"},
        checkin_require_image=1)


def calls_of(backend, name):
    """FakeBackend.calls 的元素是 (工具名, kwargs)。"""
    return [kwargs for call, kwargs in backend.calls if call == name]


def issue_checkin(tmp_db, backend, now=BASE, phone=PHONE):
    """签发打卡令牌（⛔ 不 split：链接话术带 30 分钟有效尾注）。"""
    ctx = ctx_at(tmp_db, backend, now, phone=phone)
    reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
    return extract_token(reply)


# ---------- 一、30 分钟有效期 ----------

class TestThirtyMinuteValidity:

    def test_valid_within_window(self, tmp_db):
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        result = checkin_handler.submit_from_h5(
            ctx, token, make_upload(tmp_db, token), None)
        assert result.kind == KIND_A

    def test_expired_at_edge_is_rejected(self, tmp_db):
        """第 30 分钟整即失效（⛔ 不含糊到 30 分 01 秒）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, AT_EDGE)
        result = checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "30 分钟" in result.message

    def test_expired_beyond_window(self, tmp_db):
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, BEYOND)
        info = checkin_handler.token_info(ctx, token)
        assert info["ok"] is False
        assert info["reason"] == RejectReason.EXPIRED.value

    def test_expired_token_creates_no_record(self, tmp_db):
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, BEYOND)
        checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        assert len(calls_of(backend, "checkin_submit")) == 0


# ---------- 二、绑定四要素（群 / 用户 / 业务 / 周期） ----------

class TestBindingEnforced:

    def test_wrong_user_rejected(self, tmp_db):
        """链接被转发给他人 -> 拒绝（§2.4 身份风险声明：降低而非消除）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN, phone=OTHER_PHONE)
        result = checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "不属于当前用户" in result.message
        assert len(calls_of(backend, "checkin_submit")) == 0

    def test_wrong_group_rejected(self, tmp_db):
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN, group_id=OTHER_GROUP)
        result = checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "不适用于当前群" in result.message

    def test_wrong_biz_rejected(self, tmp_db):
        """打卡令牌拿到报数提交口 -> 拒绝（⛔ 不得跨业务）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        setup_fields(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_B
        assert "不适用于当前业务" in result.message
        assert len(calls_of(backend, "report_submit")) == 0

    def test_wrong_period_rejected(self, tmp_db):
        """跨日再用昨天的链接 -> 周期不符拒绝。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        next_day = BASE + timedelta(days=1)
        ctx = ctx_at(tmp_db, backend, next_day)
        result = checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        assert result.kind == KIND_B
        assert "不适用于当前周期" in result.message

    def test_tampered_token_rejected(self, tmp_db):
        """改一个字符 -> 查不到即拒绝（not_found，⛔ 不泄露存在性）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        result = checkin_handler.submit_from_h5(ctx, token + "x", "img/1.jpg",
                                                None)
        assert result.kind == KIND_B
        assert "已被作废" in result.message


# ---------- 三、规范拒绝话术 ----------

class TestStandardRejectMessages:

    def test_same_reason_same_wording_across_biz(self):
        """同一原因在打卡 / 报数两处措辞一致（只差命令名）。"""
        for reason in (RejectReason.WRONG_USER, RejectReason.WRONG_GROUP,
                       RejectReason.WRONG_BIZ, RejectReason.WRONG_PERIOD):
            # 把各自的命令名替换成占位符后应完全同一句（唯一差异就是命令名）
            a = checkin_handler._reject_text(reason).replace("打卡", "{cmd}")
            b = report_handler._reject_text(reason).replace("报数", "{cmd}")
            assert a == b, reason

    def test_expired_message_states_the_window(self):
        """过期话术必须说清「多少分钟」，否则用户不知道为什么突然不能用。"""
        assert "30 分钟" in checkin_handler._reject_text(RejectReason.EXPIRED)
        assert "30 分钟" in report_handler._reject_text(RejectReason.EXPIRED)

    def test_reissue_hint_present(self):
        """每条话术都要告诉用户下一步：回群重发命令拿新链接。"""
        for reason in (RejectReason.NOT_FOUND, RejectReason.EXPIRED):
            assert "打卡" in checkin_handler._reject_text(reason)
            assert "报数" in report_handler._reject_text(reason)

    def test_no_biz_specific_dict_left(self):
        """⛔ 处理器不得自带一套话术（第七阶段：单一真源）。"""
        for module in (checkin_handler, report_handler):
            assert not hasattr(module, "REJECT_TEXT")


# ---------- 四、成功后立即失效 ----------

class TestInvalidatedOnSuccess:

    def test_token_consumed_immediately(self, tmp_db):
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        assert checkin_handler.submit_from_h5(
            ctx, token, make_upload(tmp_db, token), None).kind == KIND_A
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"
        assert row["consumed_request_id"]

    def test_reuse_after_success_creates_no_second_record(self, tmp_db):
        """成功后复用同一链接：不产生第二条记录（安全重试归 A 语义）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        ref = make_upload(tmp_db, token)
        checkin_handler.submit_from_h5(ctx, token, ref, None)
        again = checkin_handler.submit_from_h5(ctx, token, ref, None)
        assert again.kind == KIND_A
        assert again.duplicate is True
        assert len(calls_of(backend, "checkin_submit")) == 1

    def test_reopen_page_shows_done_not_submittable(self, tmp_db):
        """重开已完成页：可显示已完成，⛔ 不可再次独立提交。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN)
        checkin_handler.submit_from_h5(
            ctx, token, make_upload(tmp_db, token), None)
        info = checkin_handler.token_info(ctx, token)
        assert info["ok"] is True
        assert info["done"] is True

    def test_report_token_consumed_immediately(self, tmp_db):
        enable_all(tmp_db)
        setup_fields(tmp_db)
        backend = FakeBackend()
        ctx = ctx_at(tmp_db, backend, BASE)
        reply = report_handler.handle_command(commands.parse("报数"), ctx)
        token = extract_token(reply)
        result = report_handler.submit_from_h5(
            ctx, token, [{"field_id": 1, "quantity": 3}])
        assert result.kind == KIND_A
        row = tmp_db.get_token(token_id_of(token))
        assert row["state"] == "consumed"


# ---------- 五、越权拒绝留痕 ----------

class TestRejectAudited:

    def test_wrong_user_records_security_event(self, tmp_db):
        """越权尝试必须留痕，运维可查（⛔ 令牌明文不写日志）。"""
        backend = FakeBackend()
        checkin_setup(tmp_db)
        token = issue_checkin(tmp_db, backend)
        ctx = ctx_at(tmp_db, backend, WITHIN, phone=OTHER_PHONE)
        checkin_handler.submit_from_h5(ctx, token, "img/1.jpg", None)
        rows = tmp_db._conn.execute(
            "SELECT event_type FROM security_event ORDER BY id").fetchall()
        assert any(r["event_type"] == "token_wrong_user" for r in rows)

    def test_reject_security_event_covers_all_overreach(self):
        """越权类原因全部有对应事件名（⛔ 漏一个就是审计盲区）。"""
        for reason in (RejectReason.WRONG_USER, RejectReason.WRONG_GROUP,
                       RejectReason.WRONG_BIZ, RejectReason.WRONG_PERIOD,
                       RejectReason.NOT_FOUND):
            assert reason in tokens_mod.REJECT_SECURITY_EVENT, reason

    def test_expired_is_not_security_event(self):
        """过期是正常业务事件，⛔ 不当安全事件刷告警。"""
        assert RejectReason.EXPIRED not in tokens_mod.REJECT_SECURITY_EVENT


class TestReportPageRowManagement:
    """报数 H5 行增删（⭐ 2026-10-20 用户报障）。

    线上现象：点「×」不立即消失；有时点「+ 添加报数字段」反而少了一行。
    根因：旧实现把「DOM 节点」和「rows 数组」各改一半再互相 refresh，
    两组状态一旦错位就永久不一致。

    这里锁住页面结构口径（无 JS 测试运行环境，故按模板源码断言）：
    1. ⛔ 不再出现「用闭包里的 select.parentNode 删 DOM」的旧写法；
    2. 删除走事件委托（data-del 行 id）；
    3. 状态只有 rows 一个真源，增删都走 render() 整表重绘。
    """

    def _page(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "group-ops-bot"
        return (root / "app" / "h5templates" / "report.html").read_text(
            encoding="utf-8")

    def test_no_legacy_dom_only_removal(self):
        page = self._page()
        assert "row.select.parentNode" not in page
        assert "rows.indexOf(row)" not in page

    def test_delete_uses_event_delegation(self):
        page = self._page()
        assert 'del.setAttribute("data-del"' in page
        assert 'getAttribute("data-del")' in page

    def test_rows_are_single_source_of_truth(self):
        page = self._page()
        assert "function render(" in page
        assert "rows.push({ id: ++seq" in page    # 行有稳定 id（⛔ 不用下标）
        # 增删都必须重绘，⛔ 不允许只改一半的局部更新
        assert page.count("render();") >= 2
