# -*- coding: utf-8 -*-
"""量子密信真实载荷字段别名适配层（实测契约锁定）。

背景：2026-10-15 16:54:49 线上首条真实回调经观测探针抓到字段结构，
发现平台用`callBackUrl`（大写 B）/ `textMsg.content` / `userName`，
而 `normalize.py` 的假想协议候选键未覆盖 → 回调被 400 静默拒收。

本文件锁三件事：
1. **实测结构能走通**：`normalize_payload(adapt(真实结构))` 得到完整 InboundContext
2. ⛔ **不伪造**：非字符串 content / 缺字段，仍如实报缺失（fail-closed 不变）
3. ⛔ **不改入参、不覆盖既有键**：适配层是纯函数、无副作用

⚠️ 若日后按 `normalize.py` docstring 指引直接修订其候选键表，
   本文件与适配层一并删除，⛔ 不两层并存。
"""

from __future__ import annotations

from app import normalize as nm
from app.web.payload_probe import shape_line
from app.web.platform_adapter import adapt


def _real_payload() -> dict:
    """严格按16:54:49 探针实测结构重建（字段名与类型 1:1 对齐）。"""
    return {
        "callBackMethod": "post",
        "callBackUrl": "http://219.151.177.33:12334/api/reply/abcdef0123456789",
        "groupId": "gid_1737999999999",
        "groupName": "量子密信群机器人开发沟通群",
        "phone": "17313771297",
        "robotId": "robot_8888888888888",
        "tenantId": 10001,
        "textMsg": {"content": "绑定", "tenantId": 10001},
        "type": "text",
        "userName": "郑佳",
    }


class TestRealPayloadAdaptation:
    def test_real_shape_normalizes_end_to_end(self):
        inbound = nm.normalize_payload(adapt(_real_payload()))
        assert inbound.group_id == "gid_1737999999999"
        assert inbound.phone == "17313771297"
        assert inbound.text == "绑定"
        assert inbound.reply_url.startswith("http://219.151.177.33:12334/")
        assert inbound.name == "郑佳"

    def test_platform_platform_user_id_absent_stays_none(self):
        # 实测载荷⛔ 没有平台用户 ID 字段：如实为 None，⛔ 不拿手机号冒充
        inbound = nm.normalize_payload(adapt(_real_payload()))
        assert inbound.platform_user_id is None

    def test_raw_payload_preserved_for_audit(self):
        raw = _real_payload()
        inbound = nm.normalize_payload(adapt(raw))
        assert inbound.raw["groupId"] == raw["groupId"]
        assert "textMsg" in inbound.raw


class TestAdapterIsPure:
    def test_input_not_mutated(self):
        raw = _real_payload()
        adapt(raw)
        assert "text" not in raw
        assert "reply_url" not in raw
        assert "name" not in raw
        assert raw["textMsg"] == {"content": "绑定", "tenantId": 10001}

    def test_existing_reply_url_not_overwritten(self):
        raw = _real_payload()
        raw["reply_url"] = "http://existing/keep-me"
        assert adapt(raw)["reply_url"] == "http://existing/keep-me"

    def test_existing_text_not_overwritten(self):
        raw = _real_payload()
        raw["text"] = "统计"
        assert adapt(raw)["text"] == "统计"

    def test_non_dict_passthrough(self):
        assert adapt(["not", "a", "dict"]) == ["not", "a", "dict"]


class TestNoFabrication:
    def test_missing_text_still_raises(self):
        raw = _real_payload()
        raw["textMsg"] = {"tenantId": 10001}
        try:
            nm.normalize_payload(adapt(raw))
            raise AssertionError("缺 text 时应当如实报缺失")
        except nm.PayloadError as exc:
            assert "text" in str(exc)

    def test_non_text_message_not_fabricated(self):
        # 图片/文件消息的 content ⛔ 不是字符串：⛔ 不把整个对象塞进 text
        raw = _real_payload()
        raw["textMsg"] = {"content": {"url": "http://x/f.png"}}
        try:
            nm.normalize_payload(adapt(raw))
            raise AssertionError("非文本 content 时应当如实报缺失")
        except nm.PayloadError as exc:
            assert "text" in str(exc)

    def test_missing_reply_url_still_raises(self):
        raw = _real_payload()
        del raw["callBackUrl"]
        try:
            nm.normalize_payload(adapt(raw))
            raise AssertionError("缺回推地址时应当如实报缺失")
        except nm.PayloadError as exc:
            assert "reply_url" in str(exc)

    def test_blank_reply_url_not_accepted(self):
        raw = _real_payload()
        raw["callBackUrl"] = "   "
        try:
            nm.normalize_payload(adapt(raw))
            raise AssertionError("空白回推地址不得被当作有效值")
        except nm.PayloadError as exc:
            assert "reply_url" in str(exc)


class TestProbeLeaksNoValue:
    def test_shape_contains_names_not_values(self):
        raw = _real_payload()
        line = shape_line(raw)
        for secret in ("17313771297", "郑佳", "绑定",
                       "gid_1737999999999", "abcdef0123456789"):
            assert secret not in line, "探针⛔ 不得记录任何值"
        for field in ("callBackUrl", "groupId", "textMsg", "userName"):
            assert field in line, "探针必须记录字段名"

    def test_shape_survives_odd_input(self):
        assert "payload_shape:" in shape_line({"a": [1, 2, {"b": None}]})
        assert "payload_shape:" in shape_line({})