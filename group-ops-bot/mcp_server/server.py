# -*- coding: utf-8 -*-
"""自研 MCP Server 入口（§4.11）。

启动纪律（fail-closed，§4.8）：
- MySQL 连接参数缺失 / 无效 -> 启动失败，⛔ 不静默选择任何替代路径
- 启动后提供工具清单（§4.11.1）：checkin.* / summary.* / report.* /
  user.* / op.*
- 传输：开发期同机部署（§2.9），默认 SSE（机器人侧经 HTTP 连接）

⛔ 本服务不判断业务资格（名单 / 白名单 / 周期合法性归机器人侧，§4.11.0）。
⛔ 凭据只走环境变量（GOPS_MYSQL_*），不日志、不落盘。

运行：GOPS_MYSQL_HOST=... python -m mcp_server.server
（目录名 mcp_server/ 为合法 Python 包名；§2.13 的「mcp-server/」表述以本节回填为准）

SSE 监听：FASTMCP_HOST / FASTMCP_PORT（缺省 127.0.0.1:8000）。
⚠️ mcp 1.x 的 FastMCP.__init__ 把 host/port 作显式参数传给 Settings
（pydantic init > env 优先级，2026-10-14 在 mcp 1.30.0 实测），仅设 env 不传参
将永远落在 SDK 默认 8000 —— 故本入口显式读取 env 再传入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 保证 `app` 与 `mcp-server` 均可导入（与本文件位置无关）
_BOT_ROOT = Path(__file__).resolve().parent.parent
if str(_BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOT_ROOT))

from app.config import ConfigError, load_mysql_config  # noqa: E402
from .mcp_server_compat import FastMCP, HAS_OFFICIAL_SDK  # noqa: E402  (见该文件说明)

from .db.pool import MysqlPool  # noqa: E402
from .idem import IdempotentWriter  # noqa: E402
from .tools import checkin, report, summary, user  # noqa: E402  (user.py 含 user.* 与 op.*)


# SSE 监听参数：显式读取 env 传给 FastMCP（mcp 1.x init 参数优先于 env，
# 见文件头说明）。缺省与 SDK 默认值一致：127.0.0.1:8000。
_SSE_HOST_DEFAULT = "127.0.0.1"
_SSE_PORT_DEFAULT = 8000


def _sse_host() -> str:
    """SSE 监听地址（FASTMCP_HOST，缺省 127.0.0.1；⛔ 只听回环）。"""
    return os.environ.get("FASTMCP_HOST") or _SSE_HOST_DEFAULT


def _sse_port() -> int:
    """SSE 监听端口（FASTMCP_PORT，缺省 8000）。

    fail-closed：端口配置为非法值 / 越界时直接拒启，
    ⛔ 不静默回落默认值（配错必须当场炸，不能带着错配置跑起来）。
    """
    raw = os.environ.get("FASTMCP_PORT")
    if raw is None or not str(raw).strip():
        return _SSE_PORT_DEFAULT
    try:
        port = int(str(raw).strip())
    except ValueError:
        raise ConfigError(
            f"FASTMCP_PORT must be an integer, got {raw!r}; "
            "refusing to start (fail-closed)")
    if not 1 <= port <= 65535:
        raise ConfigError(
            f"FASTMCP_PORT out of range (1-65535): {port}; "
            "refusing to start (fail-closed)")
    return port


def build_server(pool: MysqlPool | None = None):
    """组装 MCP Server（便于测试注入替身 pool）。"""
    if pool is None:
        config = load_mysql_config()  # fail-closed：缺配置直接抛
        pool = MysqlPool(config)
    writer = IdempotentWriter(pool)

    if HAS_OFFICIAL_SDK:
        # 官方 SDK：显式传 host/port（env 读取见 _sse_host/_sse_port）
        mcp = FastMCP("group-ops-mcp", host=_sse_host(), port=_sse_port())
    else:
        # 测试替身（_FakeMCP）不接受 host/port；其 run() 本就抛错禁跑
        mcp = FastMCP("group-ops-mcp")
    checkin.register(mcp, writer)
    summary.register(mcp, writer)
    report.register(mcp, writer)
    user.register(mcp, writer)   # 注册 user.* 与 op.*（同模块）
    return mcp, pool, writer


def main() -> None:
    mcp, pool, _ = build_server()
    try:
        mcp.run(transport="sse")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
