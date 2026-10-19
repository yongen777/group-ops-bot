# -*- coding: utf-8 -*-
"""量子密信 webhook/send 载荷**字段名扫描**（2026-10-16，一次性工具）。

背景与已知事实（均为本轮实测，⛔ 非推断）
------------------------------------------
目标：`https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=<平台密钥>`
（key 即平台凭据，全程不进日志）。

  空载荷            -> code 5005「参数[type]消息类型不可为空」
  type=1(数字)      -> code 500「系统异常」（类型枚举不接受数字）
  type="text"(字符串) -> 通过类型校验，进入内容校验
  type="text" + 各种内容字段 -> code 7101「消息内容不可为空」

⚠️ ⭐ **HTTP 200 不等于成功**：平台把业务失败也包在 200 里，判定必须读
JSON 的 `success`/`code`。2026-10-15 打卡回推就是 HTTP 200 + 业务失败 →
误判"发送成功"，实则群里无消息（根因）。

扫描思路（⛔ 不猜协议，让平台自己说）
-----------------------------------
固定 `type="text"`，逐个替换内容字段名并传**空值**（⛔ 空内容绝不会被发出
消息），看哪个键能让 7101 消失 —— 消失即该字段名被识别。

安全约束
--------
1. ⛔ URL 只从 SQLite 读；打印只取 host + path，⛔ 不打 query。
2. ⛔ 扫描阶段一律空值，⛔ 不会向群里发出任何消息。
3. ⛔ 真实发送是独立轮次，必须显式 --send。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

import httpx

DB = "/opt/group-ops/data/bot_state.db"
TIMEOUT = 15.0
TYPE_TEXT = "text"

# 候选内容字段名（覆盖常见 IM webhook 形态 + 与平台入站对称的写法）
CANDIDATES = [
    ("content", {}),
    ("text", {}),
    ("msgContent", {}),
    ("message", {}),
    ("body", {}),
    ("msg", {}),
    ("textContent", {}),
    ("msgText", {}),
    ("contentObj", {}),
    ("contentObject", {}),
    ("data", {}),
    ("payload", {}),
    ("msgBody", {}),
    ("markdown", {}),
]

# 复合形态：外层对象包一层（对齐入站 textMsg.content）
NESTED = [
    ("textMsg.content", {"textMsg": {"content": ""}}),
    ("text.content", {"text": {"content": ""}}),
    ("content.content", {"content": {"content": ""}}),
    ("msg.content", {"msg": {"content": ""}}),
    ("data.content", {"data": {"content": ""}}),
    ("content.text", {"content": {"text": ""}}),
]


def load_url() -> str:
    """从实例表读 webhook 地址（⛔ 不经命令行、不打日志）。"""
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    try:
        row = conn.execute(
            "SELECT webhook_base_url FROM qmx_robot_instance "
            "WHERE status='ACTIVE' LIMIT 1").fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        print("!! 未找到 ACTIVE 实例的 webhook 地址，终止")
        sys.exit(2)
    return row[0]


def safe_label(url: str) -> str:
    """只显示 host + path，⛔ 去掉 query（凭据在 query 里）。"""
    u = httpx.URL(url)
    return "%s%s" % (u.host, u.path)


def post(client: httpx.Client, url: str, payload: dict) -> dict:
    """发一次探测并解析业务结果（⛔ 只看 JSON 的 success/code）。"""
    try:
        resp = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        return {"http": None, "success": False, "code": None,
                "message": "transport: %s" % exc}
    body = (resp.text or "").strip()
    parsed = {}
    if body:
        try:
            parsed = json.loads(body)
        except ValueError:
            return {"http": resp.status_code, "success": False, "code": None,
                    "message": "<non-json> %s" % body[:200]}
    return {"http": resp.status_code, "success": bool(parsed.get("success")),
            "code": parsed.get("code"), "message": parsed.get("message")}


def scan_flat(client: httpx.Client, url: str) -> list:
    print("\n### 平铺字段名扫描（值全为空串）")
    out = []
    for name, _ in CANDIDATES:
        payload = {"type": TYPE_TEXT, name: ""}
        r = post(client, url, payload)
        flag = "  ← 有反应!" if r["code"] != 7101 else ""
        print("  %-16s code=%-6s %s%s"
              % (name, r["code"], r["message"], flag))
        out.append((name, r))
    return out


def scan_nested(client: httpx.Client, url: str) -> list:
    print("\n### 复合形态扫描（外层对象包 content）")
    out = []
    for name, extra in NESTED:
        payload = dict(extra)
        payload["type"] = TYPE_TEXT
        r = post(client, url, payload)
        flag = "  ← 有反应!" if r["code"] != 7101 else ""
        print("  %-16s code=%-6s %s%s"
              % (name, r["code"], r["message"], flag))
        out.append((name, r))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true",
                    help="执行真实发送（会往群里发消息）")
    args = ap.parse_args()
    url = load_url()
    print("目标: %s  （⛔ query 已隐去，凭据不显示）" % safe_label(url))
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as client:
        results = scan_flat(client, url) + scan_nested(client, url)
        hits = [n for n, r in results if r["success"] or r["code"] != 7101]
    print("\n" + "=" * 68)
    if hits:
        print("出现非 7101 的候选（需人工判读）：%s" % ", ".join(hits))
    else:
        print("所有候选均回 7101「消息内容不可为空」")
    if args.send:
        print("⛔ --send 已开，但真实发送轮次需先确认字段名再补。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())