# -*- coding: utf-8 -*-
"""幂等机制测试（§4.11.3）。

⚠️ 脱库测试：只覆盖判定逻辑与分支归类；真实 MySQL 行为（唯一索引冲突 /
DATETIME(3) /  charset）必须 DDL 批准后用测试库验证（§4.11.5）。
"""

import pymysql.err
import pytest

from doubles.fakes import FakeCursor, FakePool
from mcp_server.idem import (
    STATUS_REJECTED, STATUS_SUCCESS, STATUS_UNKNOWN,
    ScopeRejected, _sanitize_input, _verdict_from_log,
    IdempotentWriter,
)


# dao.get_op_log 的列序（与 PyMySQL 默认 tuple 行一致；替身必须同形态）
OPLOG_ROW = ("req-1", "checkin.submit", "checkin", "g1", 1, "2026-10-13")


def oplog_row(status="success", record_id=5, result_json="{}",
              error_code=None, error_msg=None):
    return OPLOG_ROW + (status, record_id, result_json, error_code,
                        error_msg, "2026-10-13T10:00:00+08:00",
                        "2026-10-13T10:00:00+08:00")


def make_writer(fetchone_values=None):
    pool = FakePool(FakeCursor(fetchone_values))
    return IdempotentWriter(pool), pool


def integrity_error(code, message):
    return pymysql.err.IntegrityError(code, message)


class TestVerdictFromLog:
    def test_success_returns_original_result(self):
        verdict = _verdict_from_log({
            "status": "success", "record_id": 42,
            "result_json": '{"record_id": 42, "message": "ok"}',
            "error_code": None, "error_msg": None})
        assert verdict["status"] == STATUS_SUCCESS
        assert verdict["duplicate"] is True
        assert verdict["record_id"] == 42

    def test_rejected_returns_original_rejection(self):
        verdict = _verdict_from_log({
            "status": "rejected", "record_id": None,
            "result_json": None, "error_code": "scope_conflict",
            "error_msg": "本时段已打卡"})
        assert verdict["status"] == STATUS_REJECTED
        assert verdict["duplicate"] is True
        assert verdict["message"] == "本时段已打卡"

    def test_pending_returns_unknown(self):
        # §4.11.3 第 3 条：pending -> unknown（机器人归 C，不当成功也不当失败）
        verdict = _verdict_from_log({
            "status": "pending", "record_id": None, "result_json": None,
            "error_code": None, "error_msg": None})
        assert verdict["status"] == STATUS_UNKNOWN
        assert verdict["duplicate"] is False


class TestIntegrityErrorClassification:
    """§4.11.3 第 4 条：1062 按冲突在 request_id 还是范围键归类。"""

    def test_scope_index_conflict_is_rejected(self, monkeypatch):
        writer, _ = make_writer()
        exc = integrity_error(
            1062, "Duplicate entry 'g1-1-2026-10-13-am' for key 'uq_checkin_scope'")
        result = writer._handle_integrity_error(
            exc, "req-1", "checkin.submit", "checkin", "g1", 1,
            "2026-10-13", "{}")
        assert result["status"] == STATUS_REJECTED
        assert result["error_code"] == "scope_conflict"

    def test_request_id_conflict_is_duplicate_lookup(self, monkeypatch):
        # request_id 唯一索引冲突 -> 回查原结果（duplicate）
        pool = FakePool(FakeCursor([oplog_row(status="success", record_id=7)]))
        writer = IdempotentWriter(pool)
        exc = integrity_error(
            1062, "Duplicate entry 'req-1' for key 'request_id'")
        result = writer._handle_integrity_error(
            exc, "req-1", "checkin.submit", "checkin", "g1", 1,
            "2026-10-13", "{}")
        assert result["status"] == STATUS_SUCCESS
        assert result["duplicate"] is True
        assert result["record_id"] == 7

    def test_non_1062_is_unknown(self):
        writer, _ = make_writer()
        exc = integrity_error(1146, "Table 'qsm.checkin_record' doesn't exist")
        result = writer._handle_integrity_error(
            exc, "req-1", "checkin.submit", "checkin", "g1", 1,
            "2026-10-13", "{}")
        assert result["status"] == STATUS_UNKNOWN


class TestSanitizeInput:
    def test_forbidden_keys_stripped(self):
        payload = {"phone": "13800138009", "token": "abc",
                   "password": "p", "image_ref": "img-1"}
        cleaned = _sanitize_input(payload)
        assert "abc" not in cleaned and '"p"' not in cleaned
        assert "13800138009" in cleaned and "img-1" in cleaned


class TestExecutePaths:
    def test_success_single_transaction(self):
        writer, pool = make_writer()  # 快路径无记录

        def biz(cur, dao):
            cur.execute("INSERT ...")
            return 99, {"session": "am"}

        result = writer.execute(
            tool_name="checkin.submit", biz_type="checkin", group_id="g1",
            user_id=1, period_key="2026-10-13", request_id="req-1",
            payload={"session": "am"}, biz_fn=biz)
        assert result["status"] == STATUS_SUCCESS
        assert result["duplicate"] is False
        assert result["record_id"] == 99
        assert pool.conn.committed == 1
        # 单事务：pending 插入 -> finish 更新，中间无 commit
        sqls = [c[0] for c in pool.cursor.calls]
        assert any("INSERT INTO operation_log" in s for s in sqls)
        assert any("UPDATE operation_log" in s for s in sqls)

    def test_duplicate_replay_returns_original(self):
        writer, _ = make_writer(fetchone_values=[oplog_row(record_id=5)])

        def biz(cur, dao):  # 不应被调用
            raise AssertionError("biz_fn must not run on replay")

        result = writer.execute(
            tool_name="checkin.submit", biz_type="checkin", group_id="g1",
            user_id=1, period_key="2026-10-13", request_id="req-1",
            payload={}, biz_fn=biz)
        assert result["status"] == STATUS_SUCCESS
        assert result["duplicate"] is True
        assert result["record_id"] == 5

    def test_pending_replay_returns_unknown(self):
        writer, _ = make_writer(fetchone_values=[oplog_row(status="pending",
                                                           record_id=None,
                                                           result_json=None)])
        result = writer.execute(
            tool_name="checkin.submit", biz_type="checkin", group_id="g1",
            user_id=1, period_key="2026-10-13", request_id="req-1",
            payload={}, biz_fn=lambda cur, dao: (1, {}))
        assert result["status"] == STATUS_UNKNOWN

    def test_scope_rejected_path(self):
        writer, pool = make_writer()

        def biz(cur, dao):
            raise ScopeRejected("本时段已打卡")

        result = writer.execute(
            tool_name="checkin.submit", biz_type="checkin", group_id="g1",
            user_id=1, period_key="2026-10-13", request_id="req-2",
            payload={}, biz_fn=biz)
        assert result["status"] == STATUS_REJECTED
        assert result["duplicate"] is False
        assert result["message"] == "本时段已打卡"
        # 拒绝结论落库（重放返回原拒绝）
        sqls = [c[0] for c in pool.cursor.calls]
        assert sum("INSERT INTO operation_log" in s for s in sqls) == 2

    def test_query_by_request(self):
        writer, _ = make_writer(fetchone_values=[
            oplog_row(status="success", record_id=3)])
        result = writer.query_by_request("req-1")
        assert result["found"] is True
        assert result["status"] == STATUS_SUCCESS
        assert result["record_id"] == 3
