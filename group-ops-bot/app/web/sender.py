# -*- coding: utf-8 -*-
"""出站发送端口（§3.2 骨架出站段 / §4.5 提醒推送）。

两类发送，能力状态不同（⛔ 不混淆）：
- reply：入站消息的回推地址（载荷内带回，normalize 提取）—— 链路假设成立，
  ⚠️ 载荷形态未实测（D11）
- push：主动推送（定时提醒 @ 人）—— 平台能力**未确认**（§5.2），
  未接通时返回 (False, reason)，由调用方记 unsent，⛔ 不伪造已发送

回推载荷形态同样是未实测项：按 {"text": ...} 假设，实测后必须修订并记 §8。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Optional

LOG = logging.getLogger("group-ops.sender")


class ReplySender(ABC):
    """入站回推端口。"""

    @abstractmethod
    def reply(self, reply_url: str, text: str,
              mention_phones=None) -> bool:
        """向回推地址发回复。返回是否送达；⛔ 不抛异常吞掉调用方。

        mention_phones（2026-10-16 用户指令一）：群聊回复**必须 @ 发送者**。
        传手机号列表（真实手机号，⛔ 不靠正文写「@姓名」假装 @）；
        为空/None 表示不带 @。⚠️ 平台是否在回推通道渲染 @ 需实测确认，
        载荷字段见 platform/qmx.py（与已实测接通的 push 同构）。
        """


class HttpReplySender(ReplySender):
    """经 HTTP POST 回推（旧通用实现；生产走 QmxAdapter）。"""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        self._timeout = timeout_seconds

    def reply(self, reply_url: str, text: str, mention_phones=None) -> bool:
        import httpx
        try:
            resp = httpx.post(reply_url, json={"text": text},
                              timeout=self._timeout)
        except httpx.HTTPError as exc:
            LOG.warning("reply POST failed: %s", exc)
            return False
        if resp.status_code >= 300:
            LOG.warning("reply POST -> HTTP %s", resp.status_code)
            return False
        return True


def platform_push(group_id: str, text: str):
    """平台主动推送（定时提醒用）。

    ⛔ 当前版本未接通：平台 push / @ 能力未确认（§5.2）。
    接通前任何调用都如实返回未发送 + 原因，⛔ 不伪造成功。
    """
    return False, "platform push capability unconfirmed (see 5.2)"
