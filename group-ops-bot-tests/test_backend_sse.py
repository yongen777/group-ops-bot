# -*- coding: utf-8 -*-
"""MCP SSE 适配器测试（脱库，§2.8：替身与故障模拟只存在于测试目录）。

⚠️ 这些测试只验证适配器的协议行为（握手 / 调用 / 错误归类 / 不重试），
⛔ 不构成「与真实 MCP Server + MySQL 联通」的证据。
"""

from __future__ import annotations

import httpx
import pytest

from app.backend.base import BackendError
from app.backend.mcp_adapter import McpSseAdapter

SSE_BODY = (
    "event: endpoint\r\n"
    "data: /messages/?session_id=abc\r\n"
    "\r\n"
    "event: message\r\n"
    'data: {"jsonrpc":"2.0","id":"1","result":{"protocolVersion":'
    '"2025-03-26","capabilities":{},"serverInfo":{"name":"fake"}}}\r\n'
    "\r\n"
    "event: message\r\n"
    'data: {"jsonrpc":"2.0","id":"2","result":{"content":[{"type":"text",'
    '"text":"{\\"status\\": \\"success\\", \\"record_id\\": 42}"}],'
    '"isError":false}}\r\n'
    "\r\n"
)


def make_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler),
                        base_url="http://mcp.test")


def make_adapter(handler, timeout=5.0):
    return McpSseAdapter(timeout_seconds=int(timeout),
                         client=make_client(handler))


def test_handshake_and_tool_call():
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/sse":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                content=SSE_BODY.encode("utf-8"))
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(202, json={})
        return httpx.Response(404)

    adapter = make_adapter(handler)
    try:
        result = adapter.checkin_submit(
            user_id=1, group_id="g1", period_key="2026-10-13",
            session="am", image_ref="img/1.jpg", image_meta=None,
            request_id="req-1")
    finally:
        adapter.close()
    assert result == {"status": "success", "record_id": 42}
    # initialize + notifications/initialized + tools/call = 3 次 POST
    assert len(posts) == 3
    import json
    bodies = [json.loads(p.content) for p in posts]
    assert bodies[0]["method"] == "initialize"
    assert bodies[1]["method"] == "notifications/initialized"
    assert bodies[2]["method"] == "tools/call"
    assert bodies[2]["params"]["name"] == "checkin.submit"
    assert bodies[2]["params"]["arguments"]["request_id"] == "req-1"


def test_jsonrpc_error_raises_backend_error():
    body = (
        "event: endpoint\r\n"
        "data: /messages/?session_id=abc\r\n"
        "\r\n"
        "event: message\r\n"
        'data: {"jsonrpc":"2.0","id":"1","result":{"protocolVersion":'
        '"2025-03-26","capabilities":{},"serverInfo":{"name":"fake"}}}\r\n'
        "\r\n"
        "event: message\r\n"
        'data: {"jsonrpc":"2.0","id":"2","error":{"code":-32602,'
        '"message":"invalid params"}}\r\n'
        "\r\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                content=body.encode("utf-8"))
        return httpx.Response(202, json={})

    adapter = make_adapter(handler)
    try:
        with pytest.raises(BackendError) as exc_info:
            adapter.checkin_submit(
                user_id=1, group_id="g1", period_key="2026-10-13",
                session="am", image_ref="img/1.jpg", image_meta=None,
                request_id="req-1")
    finally:
        adapter.close()
    assert "invalid params" in str(exc_info.value)


def test_post_failure_no_retry():
    """POST 失败 -> BackendError，且 ⛔ 不自动重试（§2.5.4）。"""
    posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"},
                content=SSE_BODY.encode("utf-8"))
        posts.append(request)
        if len(posts) <= 2:
            return httpx.Response(202, json={})
        raise httpx.ConnectError("connection refused")

    adapter = make_adapter(handler)
    try:
        with pytest.raises(BackendError):
            adapter.checkin_submit(
                user_id=1, group_id="g1", period_key="2026-10-13",
                session="am", image_ref="img/1.jpg", image_meta=None,
                request_id="req-1")
    finally:
        adapter.close()
    # initialize + notification + 失败的那一次 tools/call，⛔ 无额外重试
    assert len(posts) == 3


def test_sse_handshake_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    adapter = make_adapter(handler)
    try:
        with pytest.raises(BackendError):
            adapter.checkin_submit(
                user_id=1, group_id="g1", period_key="2026-10-13",
                session="am", image_ref="img/1.jpg", image_meta=None,
                request_id="req-1")
    finally:
        adapter.close()
