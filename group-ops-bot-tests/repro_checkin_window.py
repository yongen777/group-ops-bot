# -*- coding: utf-8 -*-
"""打卡链路「时段内 / 时段外」对比复现（2026-10-16 用户指令 4）。

⛔ 本脚本**绝不发往真实平台**：载荷里的 ``callBackUrl`` 一律指向本地
探针（127.0.0.1 随机端口），只在本地跑完整入站 -> 适配层 -> normalize
-> 派发链路，验三件事：

  A. 时段内 -> 签发 H5 令牌、返回带链接的话术、令牌落库
  B. 时段外 -> **返回含开放时间的明确话术**、不签发令牌、不调后端
  C. 出站是否真的投递（探针收到 = delivered），把「有话要说」与
     「群里真的出现消息」区分开 —— 2026-10-15 线上现象即
     平台回 2xx 但群里无消息，⛔ 不再靠 replied 字段自欺

复用既有夹具（helpers / doubles），⛔ 不另造状态库与后端替身。

用法::

    python repro_checkin_window.py            # 打印两轮对比 + 断言汇总
    python repro_checkin_window.py --json     # 机器可读输出
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(os.path.dirname(_ROOT), "group-ops-bot"))

from app import commands  # noqa: E402
from app import normalize as normalize_mod  # noqa: E402
from app.handlers import checkin as checkin_handler  # noqa: E402
from app.platform.qmx import QmxAdapter  # noqa: E402
from app.state import StateStore  # noqa: E402
from app.web.platform_adapter import adapt as adapt_platform_payload  # noqa: E402
from doubles.fakes import FakeBackend  # noqa: E402
from helpers import GROUP, PHONE, TZ, make_config, make_ctx  # noqa: E402

# 与线上群配置一致的时段（2026-10-15 实测 group_config.checkin_deadlines）
REAL_SLOTS = {"slots": [
    {"id": "am", "name": "上午", "start": "08:00", "end": "09:00"},
    {"id": "pm", "name": "下午", "start": "14:00", "end": "14:50"},
]}
REAL_NAME = "量子密信群机器人开发沟通群"

# 三个对照时刻：上午时段内 / 上午与下午之间 / 下午时段已过（= 18:05 现场）
T_IN_AM = datetime(2026, 10, 15, 8, 30, tzinfo=TZ)
T_BETWEEN = datetime(2026, 10, 15, 10, 0, tzinfo=TZ)
T_AFTER_ALL = datetime(2026, 10, 15, 18, 5, tzinfo=TZ)   # ⭐ 昨日现场时刻


def platform_payload(text: str, reply_url: str) -> dict:
    """平台真实载荷形态（2026-10-15 16:54:49 实测字段名，⛔ 值已脱敏）。"""
    return {
        "callBackMethod": "POST",
        "callBackUrl": reply_url,
        "groupId": GROUP,
        "groupName": REAL_NAME,
        "phone": PHONE,
        "robotId": GROUP,
        "tenantId": 1,
        "textMsg": {"content": text, "tenantId": 1},
        "type": "text",
        "userName": "郑佳",
    }


def run_case(store, backend, now, label, expect):
    """跑一轮：真实载荷 -> 适配层 -> normalize -> 派发 -> 出站投递。"""
    hits = []

    class _Probe:
        """出站探针（⛔ 只记录，⛔ 不做任何平台语义判断）。"""

    def _fake_post(url, json=None, timeout=None, **_kw):
        hits.append(json)
        try:
            import httpx
            return httpx.Response(200, text='{"code":0}')
        except Exception:  # pragma: no cover
            class _R:
                status_code = 200
                text = '{"code":0}'
            return _R()

    import app.platform.qmx as qmx_mod
    real_post = None
    import httpx as _httpx
    real_post = _httpx.post
    _httpx.post = _fake_post
    try:
        adapter = QmxAdapter()
        inbound = normalize_mod.normalize_payload(
            adapt_platform_payload(platform_payload("打卡", "http://probe.local/r")))
        # ⛔ 入站断言：适配层必须把真实字段还原成内部字段名
        assert inbound.group_id == GROUP, inbound
        assert inbound.text == "打卡", inbound
        assert inbound.name == "郑佳", inbound
        assert inbound.reply_url == "http://probe.local/r", inbound

        ctx = make_ctx(store, backend,
                       make_config(Path(store.db_path).parent),
                       now=now, group_id=GROUP, phone=PHONE)
        reply = checkin_handler.handle_command(commands.parse("打卡"), ctx)
        delivered = adapter.reply_text(inbound.reply_url, reply) if reply else None
    finally:
        _httpx.post = real_post

    tokens = store._conn.execute(
        "SELECT COUNT(*) AS n FROM tokens").fetchone()["n"]
    got = {"label": label, "now": now.strftime("%H:%M"), "reply": reply,
           "tokens_issued": tokens, "backend_calls": len(backend.calls),
           "outbound_payloads": hits, "delivered": delivered,
           "checks": _check(expect, {"reply": reply, "tokens_issued": tokens,
                                     "backend_calls": len(backend.calls),
                                     "delivered": delivered})}
    return got


def _check(expect, got):
    """按契约逐条核对，返回 {断言名: 是否通过}。"""
    reply = got["reply"] or ""
    checks = {"有明确话术": bool(reply.strip()),
              "话术含当前时段判断": "当前不在打卡时段" in reply
                                       or "请点击链接" in reply}
    if expect == "in_window":
        checks.update({
            "签发令牌": got["tokens_issued"] == 1,
            "话术含 H5 链接": "#t=" in reply,
        })
    elif expect == "not_open":
        checks.update({
            "话术含开放时间": "14:00" in reply,
            "未签发令牌": got["tokens_issued"] == 0,
            "未调后端": got["backend_calls"] == 0,
        })
    elif expect == "all_done":
        checks.update({
            "话术列出今日全部时段": "08:00-09:00" in reply
                                     and "14:00-14:50" in reply,
            "未签发令牌": got["tokens_issued"] == 0,
            "未调后端": got["backend_calls"] == 0,
        })
    checks["出站已投递"] = got["delivered"] is True
    return checks


def main():
    as_json = "--json" in sys.argv
    results = []
    cases = [("A 时段内 08:30（上午 08:00-09:00）", T_IN_AM, "in_window"),
             ("B 时段间 10:00（未到下午 14:00）", T_BETWEEN, "not_open"),
             ("C 昨日现场 18:05（下午已过）", T_AFTER_ALL, "all_done")]
    with tempfile.TemporaryDirectory() as td:
        for label, now, expect in cases:
            store = StateStore(Path(td) / ("s_%s.db" % now.strftime("%H%M")))
            store.upsert_group_config(
                GROUP, biz_switches={"checkin": True},
                checkin_deadlines=REAL_SLOTS, checkin_require_image=1)
            backend = FakeBackend()
            results.append(run_case(store, backend, now, label, expect))
            store.close()

    ok = all(all(r["checks"].values()) for r in results)
    if as_json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False,
                         indent=2))
        return 0 if ok else 1

    print("=" * 72)
    print("打卡链路 时段内 / 时段外 对比复现（本地，⛔ 未发往平台）")
    print("=" * 72)
    for r in results:
        print("\n【%s】  时刻 %s" % (r["label"], r["now"]))
        print("  机器人回复：%s" % r["reply"])
        print("  令牌签发=%d  后端调用=%d  出站投递=%s  出站载荷=%s"
              % (r["tokens_issued"], r["backend_calls"], r["delivered"],
                 json.dumps(r["outbound_payloads"], ensure_ascii=False)))
        for name, passed in r["checks"].items():
            print("    [%s] %s" % ("PASS" if passed else "FAIL", name))
    print("\n" + "=" * 72)
    print("总判定：%s" % ("全部通过 ✅" if ok else "存在未通过项 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
