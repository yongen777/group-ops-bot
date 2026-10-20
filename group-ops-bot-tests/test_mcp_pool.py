# -*- coding: utf-8 -*-
"""MCP 连接池自愈 + 工具错误透出（2026-10-20 线上事故回归）。

事故现场（2026-10-19 18:00 起）：
- MySQL 抖动期间 `_created += 1; return self._connect()` 抛错后计数不归还，
  连续 5 次失败即把 `_created` 打满、空闲队列仍为空 →
  之后**每次**工具调用都卡满 10 秒并抛 `queue.Empty`（`str()` 是空串），
  MySQL 早就恢复了，进程却永远取不到数据，只能重启。
- 机器人侧只抛「MCP tool user.get returned isError」，日志里没有原因，
  排查全靠猜。

本组测试只验证这两处行为，⛔ 不连真实 MySQL。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest

from app.backend.base import BackendError
from app.backend.mcp_adapter import McpSseAdapter
from mcp_server.db.pool import MysqlPool, PoolError

CFG = SimpleNamespace(host="127.0.0.1", port=3306, user="u",
                      password="p", database="qsm", charset="utf8mb4")


class FakeConn:
    """最小连接替身：只实现池子会用到的方法。"""

    def __init__(self, name: str = "c") -> None:
        self.name = name
        self.closed = False

    def ping(self, reconnect: bool = True) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    def cursor(self):
        raise AssertionError("本测试不应执行 SQL")


def test_connect_failure_refunds_slot(monkeypatch):
    """建连失败必须归还计数：失败 size 次后仍能连上（旧实现会永久卡死）。"""
    pool = MysqlPool(CFG, size=2)

    def boom():
        raise PoolError("mysql connect failed: simulated outage")

    monkeypatch.setattr(pool, "_connect", boom)
    for _ in range(2):
        with pytest.raises(PoolError):
            with pool.connection():
                pass
    assert pool._created == 0
    assert pool._borrowed == 0

    conn = FakeConn()
    monkeypatch.setattr(pool, "_connect", lambda: conn)
    started = time.monotonic()
    with pool.connection() as got:
        assert got is conn
    assert time.monotonic() - started < 1.0  # ⛔ 不能再等满 10 秒


def test_pool_counter_drift_self_heals(monkeypatch):
    """计数打满但无人借出、队列为空 -> 立即自愈（不等 10 秒、不抛 Empty）。"""
    pool = MysqlPool(CFG, size=2)
    conn = FakeConn()
    monkeypatch.setattr(pool, "_connect", lambda: conn)
    pool._created = 2          # 模拟历史泄漏（正常不可能出现）
    pool._borrowed = 0

    started = time.monotonic()
    with pool.connection() as got:
        assert got is conn
        assert pool._borrowed == 1
    assert time.monotonic() - started < 1.0
    assert pool._created == 1
    assert pool._borrowed == 0


def test_pool_reuse_keeps_invariant():
    """正常借还后：_created 不减、_borrowed 归零、连接回到空闲队列。"""
    pool = MysqlPool(CFG, size=2)
    conn = FakeConn()
    pool._connect = lambda: conn
    with pool.connection() as got:
        assert got is conn
        assert pool._borrowed == 1
    assert pool._borrowed == 0
    assert pool._created == 1
    assert pool._idle.qsize() == 1


SSE_ISERROR = (
    "event: endpoint\r\n"
    "data: /messages/?session_id=abc\r\n"
    "\r\n"
    "event: message\r\n"
    'data: {"jsonrpc":"2.0","id":"1","result":{"protocolVersion":'
    '"2025-03-26","capabilities":{},"serverInfo":{"name":"fake"}}}\r\n'
    "\r\n"
    "event: message\r\n"
    'data: {"jsonrpc":"2.0","id":"2","result":{"content":[{"type":"text",'
    '"text":"Error executing tool user.get: mysql pool exhausted"}],'
    '"isError":true}}\r\n'
    "\r\n"
)


def test_tool_iserror_exposes_detail():
    """isError 时把工具侧错误正文带进 BackendError（旧实现只有一个光秃秃的
    「returned isError」，线上排查无据可依）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/sse":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                content=SSE_ISERROR.encode("utf-8"))
        return httpx.Response(202, json={})

    adapter = McpSseAdapter(
        timeout_seconds=5,
        client=httpx.Client(transport=httpx.MockTransport(handler),
                            base_url="http://mcp.test"))
    with pytest.raises(BackendError) as exc:
        adapter.user_get(phone="13800000001")
    assert "isError" in str(exc.value)
    assert "mysql pool exhausted" in str(exc.value)
