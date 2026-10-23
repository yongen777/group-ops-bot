# -*- coding: utf-8 -*-
"""D40（2026-10-23）：自建应用「事件订阅 HTTP 模式」回调探针。

平台文档只写「按配置校验签名」，**未给算法**，因此先上「只观测不拦截」的探针：
- GET  /api/app-events?echostr=...  → 原样返回（平台 URL 验证常见形态）
- POST /api/app-events              → 200，并把 header/query/body 落 JSONL
"""

from __future__ import annotations

import json
import os
import pathlib

from test_d34_removed_and_rename import _bot


def _probe_file(tmp_db) -> pathlib.Path:
    return pathlib.Path(os.path.dirname(tmp_db.db_path)) / "app_events_probe.jsonl"


def test_verify_echo_returns_plain(tmp_db):
    client, _b, _s = _bot(tmp_db)
    r = client.get("/api/app-events?echostr=hello123")
    assert r.status_code == 200
    assert r.text == "hello123"


def test_verify_without_echo_returns_ok(tmp_db):
    client, _b, _s = _bot(tmp_db)
    r = client.get("/api/app-events")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_post_records_headers_and_body(tmp_db):
    client, _b, _s = _bot(tmp_db)
    r = client.post("/api/app-events",
                    json={"data": "encrypted-blob"},
                    headers={"X-CTQ-Timestamp": "1792700000000",
                             "X-CTQ-Nonce": "abc123",
                             "X-CTQ-Signature": "sig-value"})
    assert r.status_code == 200
    assert r.json() == {"success": True, "code": 200, "message": "OK"}

    f = _probe_file(tmp_db)
    assert f.exists(), "探针应把请求落到 app_events_probe.jsonl"
    rec = json.loads(f.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["method"] == "POST"
    assert rec["headers"]["x-ctq-signature"] == "sig-value"
    assert rec["headers"]["x-ctq-nonce"] == "abc123"
    assert "encrypted-blob" in rec["body"]


def test_probe_does_not_touch_group_callback(tmp_db):
    """探针独立：既有群回调路由仍然可用（不存在相互覆盖）。"""
    client, _b, _s = _bot(tmp_db)
    r = client.post("/api/app-events", json={"data": "x"})
    assert r.status_code == 200
    r2 = client.get("/healthz")
    assert r2.status_code == 200
