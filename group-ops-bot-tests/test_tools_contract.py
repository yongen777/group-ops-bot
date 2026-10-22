# -*- coding: utf-8 -*-
"""MCP 工具契约冒烟测试（§4.11.1）。

验证：工具清单齐全、命名符合 `<业务>.<动作>`、register 可重复装配。
⛔ 不验证真实 MySQL 行为（见 test_idem.py 顶部声明）。

⚠️ mcp SDK 1.30：官方 FastMCP 已无 `.tools` 同步属性，工具枚举改走
`await list_tools()`（返回 Tool 对象，取 .name）；未装 SDK 的替身
仍提供同步 `.tools` dict —— 两个形态都在 _tool_names 里归一。
"""

import asyncio

from doubles.fakes import FakePool
from mcp_server.idem import IdempotentWriter
from mcp_server.mcp_server_compat import FastMCP, HAS_OFFICIAL_SDK
from mcp_server.tools import checkin, report, summary, user
from mcp_server.server import build_server

EXPECTED_TOOLS = {
    "checkin.submit", "checkin.query_status", "checkin.query_records",
    "checkin.admin_update",
    "summary.save", "summary.query_status", "summary.query_records",
    "summary.admin_update",
    "report.submit", "report.query_status", "report.query_records",
    "report.admin_update",
    "user.upsert", "user.get", "user.list", "user.update_phone",
    "op.query_by_request", "op.query_log",
}


def _tool_names(mcp) -> set:
    """已注册工具名集合（兼容官方 SDK 异步 list_tools 与测试替身 .tools）。"""
    if HAS_OFFICIAL_SDK:
        tools = asyncio.run(mcp.list_tools())
        return {t.name for t in tools}
    return set(mcp.tools)


def test_tool_contract_complete():
    writer = IdempotentWriter(FakePool())
    mcp = FastMCP("test")
    checkin.register(mcp, writer)
    summary.register(mcp, writer)
    report.register(mcp, writer)
    user.register(mcp, writer)
    assert _tool_names(mcp) == EXPECTED_TOOLS


def test_build_server_with_injected_pool():
    mcp, pool, writer = build_server(pool=FakePool())
    assert _tool_names(mcp) == EXPECTED_TOOLS
    assert writer.pool is pool
