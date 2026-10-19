# -*- coding: utf-8 -*-
"""量子密信真实载荷 → 内部字段名的**别名适配层**（实测依据：2026-10-15 16:54:49）。

为什么需要这一层
------------------
`normalize.py` 的候选键名表是按**假想协议**写的（其 docstring 亦自述「平台真实
载荷尚未实测」）。2026-10-15 线上首条真实回调（观测探针 `payload_probe` 实测，
只记结构不记值）暴露两处不命中：

===============  ======================  ==========================  ======
语义              平台真实字段（实测）      normalize.py 候选键         结果
===============  ======================  ==========================  ======
群标识groupId  str(len=19)   groupId                    命中
手机号            phone         str(len=11)   phone                      命中
消息正文          textMsg.content str(len)      text/content/msg/body    未命中（顶层无，需下钻）
回推地址          callBackUrl   str(len=74)   callbackUrl（⛔ 只差一个大写 B）  未命中
显示名            userName      str(len=2)    name/nickname              未命中
===============  ======================  ==========================  ======

⛔ **红线**：本项目⛔ 不修改 `normalize.py`（用户硬约束：核心业务逻辑不动）。
因此平台与假想协议的差异全部隔离在本层，`normalize_payload` 收到的是
「它已经认识的字段名」。

设计纪律
--------
- ⛔ **不做语义猜测**：只做「实测字段 → normalize 已认字段」的一对一重命名；
  取不到就照旧交给 `normalize_payload` 如实报缺失（fail-closed 不变）。
- ⛔ **不覆盖**：仅在该语义键缺失时才补，normalize 既有的候选优先级不受影响。
- ⚠️ **退役条件**：若日后决定按 `normalize.py` docstring 的指引直接修订其候选键表，
  本层连同 `web/server.py` 的调用一并删除，不要两层并存（否则两处真相）。
"""

from __future__ import annotations

from typing import Any

# 实测：平台回推地址用 callBackUrl（大写 B），normalize 候选写的是 callbackUrl
_REPLY_ALIASES = ("callBackUrl",)

# 实测：消息正文包在 textMsg 对象里，取其content
_TEXT_BOXES = ("textMsg", "text_msg", "textMessage")
_TEXT_INNER_KEYS = ("content", "text")

# 实测：显示名为 userName
_NAME_ALIASES = ("userName", "user_name")


def adapt(payload: Any) -> Any:
    """把真实平台载荷补成 normalize.py 已认识的字段名（⛔ ��改变入参）。

    非 dict 原样返回（由 normalize_payload 如实报「must be a JSON object」）。
    """
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)

    # 1) 回推地址：callBackUrl -> reply_url
    if not out.get("reply_url"):
        for key in _REPLY_ALIASES:
            value = out.get(key)
            if isinstance(value, str) and value.strip():
                out["reply_url"] = value
                break

    # 2) 消息正文：textMsg.content -> text（⛔ 只认字符串；非文本消息照旧缺失）
    if not out.get("text"):
        for box in _TEXT_BOXES:
            node = out.get(box)
            if isinstance(node, dict):
                for inner in _TEXT_INNER_KEYS:
                    value = node.get(inner)
                    if isinstance(value, str) and value.strip():
                        out["text"] = value
                        break
            if out.get("text"):
                break

    # 3) 显示名：userName -> name
    if not out.get("name"):
        for key in _NAME_ALIASES:
            value = out.get(key)
            if isinstance(value, str) and value.strip():
                out["name"] = value
                break

    return out