# -*- coding: utf-8 -*-
"""FastMCP 兼容垫层。

- 生产：直接 `from mcp.server.fastmcp import FastMCP`（官方 SDK，requirements-mcp.txt）
- 测试 / 未装 SDK 环境：提供最小替身，仅够 register() 注册与 handle_* 直调，
  ⛔ 不是生产传输实现 —— 真实 MCP 联调必须装官方 SDK 跑（§9.3：不得把
  单元测试或替身描述为生产链路跑通）。
"""

from __future__ import annotations

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore
    HAS_OFFICIAL_SDK = True
except Exception:  # pragma: no cover - 取决于环境是否安装 mcp 包
    HAS_OFFICIAL_SDK = False

    class _FakeMCP:
        """最小替身：记录工具名，不提供传输。⛔ 仅测试用。"""

        def __init__(self, name: str = "") -> None:
            self.name = name
            self.tools: dict = {}

        def tool(self, name: str | None = None):
            def decorator(fn):
                self.tools[name or fn.__name__] = fn
                return fn
            return decorator

        def run(self, transport: str = "stdio") -> None:  # pragma: no cover
            raise RuntimeError(
                "fake MCP transport: install official mcp package to run")

    FastMCP = _FakeMCP  # type: ignore
