# -*- coding: utf-8 -*-
"""C 类恢复处理器测试（§4.8 第 7-8 条 / §2.5.4 / §2.5.5，脱库替身）。

覆盖（对应 §4.8 完成标准）：
- 「MCP Server 已写成功但机器人未落回执」-> 按原请求键查证补账 A
- rejected -> 补账 B（不占额度）
- unknown -> 退避重排 + attempts 计数；超限/超龄 -> 告警但⛔ 继续恢复
- BackendError（查证本身失败）-> ⛔ 不计 attempts，仅重排
- ⛔ 不自动重试、⛔ 不新建请求（原键查证是唯一动作）
- 令牌补消费（恢复判 A 后，§2.4）

⛔ 替身只验证机器人侧逻辑，不构成真实 MySQL 链路证据（§2.8）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from app import recovery as recovery_handler
from app import tokens as tokens_mod
from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, TZ, enable_all

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
SCOPE = "G100|22644212509|checkin|2026-10-13|am"
REQUEST_ID = "req-recovery-1"


def arm_task(store, request_id=REQUEST_ID, scope=SCOPE, biz="checkin",
             due=None, token_id=None, attempts=0, created_at=None):
    """落一条 C 类 intent + 到期恢复任务（测试辅助）。"""
    store.put_intent(request_id, scope, biz, GROUP, PHONE,
                     "2026-10-13", "am" if biz == "checkin" else None, {},
                     token_id=token_id)
    store.put_recovery_task(request_id, scope, biz, due or NOW)
    if attempts:
        store._conn.execute(
            "UPDATE recovery_task SET attempts=? WHERE request_id=?",
            (attempts, request_id))
        store._conn.commit()
    if created_at is not None:
        store._conn.execute(
            "UPDATE recovery_task SET created_at=? WHERE request_id=?",
            (created_at.isoformat(timespec="seconds"), request_id))
        store._conn.commit()
    return request_id


def task_row(store, request_id=REQUEST_ID):
    return store._conn.execute(
        "SELECT * FROM recovery_task WHERE request_id=?",
        (request_id,)).fetchone()


def set_due(store, request_id, due):
    store._conn.execute(
        "UPDATE recovery_task SET due_at=? WHERE request_id=?",
        (due.isoformat(timespec="seconds"), request_id))
    store._conn.commit()


def make_token(store, biz="checkin", period_key="2026-10-13", session="am"):
    value = tokens_mod.generate_token_value()
    record = tokens_mod.new_record(value, PHONE, GROUP, biz, period_key,
                                   session, NOW)
    store.put_token(record)
    return record


class TestVerdictA:
    def test_success_补账A_落回执占额度(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "success", "record_id": 77, "duplicate": True,
            "message": "ok"}

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["processed"] == 1 and summary["A"] == 1
        intent = tmp_db.get_intent(REQUEST_ID)
        assert intent["status"] == "confirmed"
        receipt = tmp_db._conn.execute(
            "SELECT * FROM receipt WHERE request_id=?", (REQUEST_ID,)).fetchone()
        assert receipt["record_id"] == "77"
        assert tmp_db.has_successful_scope(SCOPE) is True
        row = task_row(tmp_db)
        assert row["status"] == "resolved" and row["outcome"] == "A"
        # 只按原键查证一次，⛔ 没有重发写工具
        assert [c[0] for c in backend.calls] == ["op_query_by_request"]
        assert backend.calls[0][1] == {"request_id": REQUEST_ID}

    def test_success_补消费绑定令牌(self, tmp_db):
        enable_all(tmp_db)
        record = make_token(tmp_db)
        arm_task(tmp_db, token_id=record.token_id)
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "success", "record_id": 5, "duplicate": False}

        recovery_handler.tick(tmp_db, backend, NOW)

        token = tmp_db.get_token(record.token_id)
        assert token["state"] == "consumed"
        assert token["consumed_request_id"] == REQUEST_ID

    def test_success_无绑定令牌不报错(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)  # token_id=None
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "success", "record_id": 1}
        summary = recovery_handler.tick(tmp_db, backend, NOW)
        assert summary["A"] == 1


class TestVerdictB:
    def test_rejected_补账B_不占额度(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "rejected", "duplicate": True,
            "message": "已拒绝", "error_code": "scope_conflict"}

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["B"] == 1
        assert tmp_db.get_intent(REQUEST_ID)["status"] == "rejected"
        assert tmp_db.has_successful_scope(SCOPE) is False
        row = task_row(tmp_db)
        assert row["status"] == "resolved" and row["outcome"] == "B"
        # B 不再查第二次（任务已 resolved，不再 due）
        recovery_handler.tick(tmp_db, backend, NOW)
        assert len([c for c in backend.calls
                    if c[0] == "op_query_by_request"]) == 1


class TestVerdictUnknown:
    def test_unknown_保持pending_计attempts_退避重排(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()  # 默认 {"found": False} -> 无 status -> unknown

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["unknown"] == 1
        row = task_row(tmp_db)
        assert row["status"] == "pending"
        assert row["attempts"] == 1
        # 重排到未来：同一 now 再 tick 不会重复处理
        summary2 = recovery_handler.tick(tmp_db, backend, NOW)
        assert summary2["processed"] == 0

    def test_found_false_按unknown_不当失败(self, tmp_db):
        """请求可能从未到达 MCP：⛔ 不当失败、不当成功，保持待核验。"""
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "unknown", "found": False, "message": "无此请求记录"}

        recovery_handler.tick(tmp_db, backend, NOW)

        row = task_row(tmp_db)
        assert row["status"] == "pending"
        assert tmp_db.get_intent(REQUEST_ID)["status"] == "unknown"

    def test_超attempts_告警但继续恢复(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db, attempts=recovery_handler.MAX_ATTEMPTS - 1)
        backend = FakeBackend()

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["unknown"] == 1 and summary["escalated"] == 1
        row = task_row(tmp_db)
        assert row["status"] == "pending"      # ⛔ 不当未完成、⛔ 不丢弃
        assert row["escalated"] == 1
        assert row["attempts"] == recovery_handler.MAX_ATTEMPTS
        event = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='recovery_escalated'"
        ).fetchone()
        assert event is not None and REQUEST_ID in event["detail"]
        assert "22644212509" in event["phone"]

    def test_超龄_首次unknown即告警(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db, created_at=NOW - timedelta(hours=25))
        backend = FakeBackend()

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["escalated"] == 1
        assert task_row(tmp_db)["status"] == "pending"

    def test_告警只记一次(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db, attempts=recovery_handler.MAX_ATTEMPTS - 1)
        backend = FakeBackend()
        recovery_handler.tick(tmp_db, backend, NOW)
        set_due(tmp_db, REQUEST_ID, NOW)  # 强制再次到期

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["unknown"] == 1 and summary["escalated"] == 0
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='recovery_escalated'"
        ).fetchall()
        assert len(events) == 1


class TestQueryFailed:
    def test_BackendError_不计attempts_仅重排(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()
        backend.raise_on.add("op_query_by_request")

        summary = recovery_handler.tick(tmp_db, backend, NOW)

        assert summary["query_failed"] == 1
        row = task_row(tmp_db)
        assert row["status"] == "pending"
        assert row["attempts"] == 0          # ⛔ 查证没成功不计数
        assert row["last_error"] and "BackendError" in row["last_error"]
        # 重排到未来
        assert recovery_handler.tick(tmp_db, backend, NOW)["processed"] == 0


class TestNoRetryNoNewRequest:
    def test_恢复期不重发写工具_不新建请求(self, tmp_db):
        """§2.5.4 / §2.5.6 第 8 条：原键查证是唯一动作。"""
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()  # unknown 路径

        before = tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM submit_intent").fetchone()["n"]
        recovery_handler.tick(tmp_db, backend, NOW)
        after = tmp_db._conn.execute(
            "SELECT COUNT(*) AS n FROM submit_intent").fetchone()["n"]

        assert before == after == 1          # ⛔ 没有新建 intent/新请求键
        names = [c[0] for c in backend.calls]
        assert names == ["op_query_by_request"]  # ⛔ 没有 checkin_submit 等重发
        assert backend.calls[0][1]["request_id"] == REQUEST_ID  # 原键


class TestDueFilter:
    def test_未到期不处理(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db, due=NOW + timedelta(minutes=10))
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {"status": "success",
                                                  "record_id": 1}
        summary = recovery_handler.tick(tmp_db, backend, NOW)
        assert summary["processed"] == 0
        assert backend.calls == []


class TestBackoff:
    def test_指数退避封顶(self):
        assert recovery_handler._backoff_minutes(1) == 2
        assert recovery_handler._backoff_minutes(2) == 4
        assert recovery_handler._backoff_minutes(3) == 8
        assert recovery_handler._backoff_minutes(5) == 30   # 32 -> 封顶
        assert recovery_handler._backoff_minutes(12) == 30


class TestStartupScan:
    def test_recover_on_startup_补账(self, tmp_db):
        """§4.9 第 5 条：启动扫描（真实时钟，任务设为已到期）。"""
        from app import period as period_mod
        enable_all(tmp_db)
        arm_task(tmp_db, due=period_mod.now() - timedelta(minutes=5))
        backend = FakeBackend()
        backend.results["op_query_by_request"] = {
            "status": "success", "record_id": 9}

        summary = recovery_handler.recover_on_startup(tmp_db, backend)

        assert summary["A"] == 1
        assert tmp_db.has_successful_scope(SCOPE) is True


class TestTickResilience:
    def test_单任务异常不拖死整轮(self, tmp_db):
        enable_all(tmp_db)
        arm_task(tmp_db)
        backend = FakeBackend()

        def boom(**kwargs):
            raise RuntimeError("unexpected fake failure")

        backend.op_query_by_request = boom
        summary = recovery_handler.tick(tmp_db, backend, NOW)
        assert summary["processed"] == 0
        assert len(summary["errors"]) == 1
        assert task_row(tmp_db)["status"] == "pending"
