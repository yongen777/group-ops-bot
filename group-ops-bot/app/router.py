# -*- coding: utf-8 -*-
"""命令路由（§4.1 第 4-5 步 / §3.2 通用骨架第 3-5 步）。

职责边界：本模块只做「授权检查 + 分派」，不含任何业务逻辑。
业务处理器经 register() 注入（解耦：路由器不 import 具体处理器，
便于测试替身注入与后续处理器逐个落地）。

§3.2 骨架中本模块覆盖：
3. 授权   群是否在授权名单？否 -> 记安全事件，结束（不在该群回复）
4. 命令   已由 commands.parse 完成
5. 分派   进入对应业务处理器
"""

from __future__ import annotations

from typing import Callable, Optional

from . import commands
from .commands import ParseResult

# 业务处理器签名：fn(parsed, ctx) -> str（回复文本或空串表示不回复）
# ctx 为 InboundContext 派生出的 HandlerContext（handlers/base.py），
# 避免循环 import 用 TYPE_CHECKING
Handler = Callable[..., Optional[str]]


class GroupNotAuthorized(Exception):
    """群不在授权名单（§3.2 第 3 步：记安全事件，不在该群回复）。"""


class Router:
    """固定路由表：四个入口命令各一个处理器，无动态路由。"""

    def __init__(self) -> None:
        self._handlers: dict = {}

    def register(self, command: str, handler: Handler) -> None:
        if command not in commands.COMMANDS:
            raise ValueError("unknown command: %r" % (command,))
        if command in self._handlers:
            raise ValueError("duplicate handler for %r" % (command,))
        self._handlers[command] = handler

    def dispatch(self, parsed: ParseResult, ctx=None) -> Optional[str]:
        """按解析结果分派。返回回复文本；None 表示本消息不回复。"""
        if parsed.result == commands.RESULT_CONFLICT:
            return commands.reply_text(parsed)
        if parsed.result == commands.RESULT_UNKNOWN:
            return commands.reply_text(parsed)
        if parsed.result == commands.RESULT_BAD_FORMAT:
            return commands.reply_text(parsed)
        if parsed.result != commands.RESULT_DISPATCH:
            return None
        handler = self._handlers.get(parsed.command)
        if handler is None:
            # 处理器尚未落地：如实上报，⛔ 不得伪造成功或静默丢弃
            raise RuntimeError(
                "no handler registered for command %r" % (parsed.command,))
        return handler(parsed, ctx)

    @property
    def registered(self) -> tuple:
        return tuple(sorted(self._handlers))


def route_message(router: Router, raw_text: str, ctx=None) -> Optional[str]:
    """解析 + 分派一步完成（群授权检查在外层骨架做，见 web/server.py）。"""
    return router.dispatch(commands.parse(raw_text), ctx)
