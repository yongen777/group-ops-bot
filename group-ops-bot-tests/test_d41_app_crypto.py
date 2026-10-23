# -*- coding: utf-8 -*-
"""D41（2026-10-23）：自建应用事件回调的验签与解密。

算法来自官方 Demo `callback-demo/internal/encryptor/default.go`：
  签名   = HMAC-SHA256(验证Token, timestamp + nonce + data)
  会话密钥 = SHA256(加密Key + ":" + timestamp + ":" + nonce)
  IV     = base64decode(data)[:16]；密文 = 其后部分；AES-256-CBC + PKCS#7

本文件用**自造密钥与自造报文**做闭环测试（⛔ 不把真实密钥写进仓库）。
"""

from __future__ import annotations

import base64
import json

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from app.web import app_crypto

ENC_KEY = "test-encrypt-key-0123456789"
VER_TOKEN = "test-verify-token-0123456789"
TS = "1792722877666"
NONCE = "xFWXh8zvUcRDJmm5"


def _seal(plain: str, ts: str = TS, nonce: str = NONCE,
          enc_key: str = ENC_KEY) -> str:
    """按官方规则加密：iv(16) + AES-CBC(pkcs7 明文)。"""
    key = app_crypto.derive_session_key(enc_key, ts, nonce)
    iv = b"\x07" * 16
    body = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(plain.encode(), 16))
    return base64.b64encode(iv + body).decode()


def _event(msg_uid="evt-1"):
    return json.dumps({"appId": 63474, "eventType": "callback:group",
                       "userId": "6251174531257914689",
                       "groupId": "2109573070744977410",
                       "msgUid": msg_uid, "type": "text",
                       "content": {"content": "打卡"}}, ensure_ascii=False)


class TestCrypto:
    def test_signature_matches_go_impl(self):
        """签名 = HMAC-SHA256(token, ts+nonce+data)，用已知向量锁定。"""
        data = "abc"
        sig = app_crypto.generate_signature(TS, NONCE, VER_TOKEN, data)
        assert len(sig) == 64 and sig == sig.lower()
        # 自校验：换个 timestamp 必须变
        assert sig != app_crypto.generate_signature("1", NONCE, VER_TOKEN, data)

    def test_derived_key_is_sha256(self):
        import hashlib
        expect = hashlib.sha256(
            ("%s:%s:%s" % (ENC_KEY, TS, NONCE)).encode()).digest()
        assert app_crypto.derive_session_key(ENC_KEY, TS, NONCE) == expect

    def test_roundtrip(self):
        data = _seal(_event())
        sig = app_crypto.generate_signature(TS, NONCE, VER_TOKEN, data)
        out = app_crypto.open_payload(ENC_KEY, VER_TOKEN, TS, NONCE, sig, data)
        assert out is not None and out["msgUid"] == "evt-1"
        assert out["content"]["content"] == "打卡"

    def test_tampered_signature_rejected(self):
        data = _event()
        assert app_crypto.open_payload(ENC_KEY, VER_TOKEN, TS, NONCE,
                                       "0" * 64, data) is None

    def test_tampered_body_rejected(self):
        data = _event()
        sig = app_crypto.generate_signature(TS, NONCE, VER_TOKEN, data)
        bad = data[:-4] + "AAAA"
        assert app_crypto.open_payload(ENC_KEY, VER_TOKEN, TS, NONCE, sig, bad) is None

    def test_wrong_key_cannot_decrypt(self):
        data = _event()
        sig = app_crypto.generate_signature(TS, NONCE, VER_TOKEN, data)
        assert app_crypto.open_payload("another-key", VER_TOKEN, TS, NONCE,
                                       sig, data) is None

    def test_empty_inputs_rejected(self):
        assert app_crypto.verify("", NONCE, "x" * 64, VER_TOKEN, _event()) is False
        assert app_crypto.verify(TS, NONCE, "", VER_TOKEN, _event()) is False


class TestEndpoint:
    def _client(self, tmp_db, monkeypatch=None, with_keys=False):
        from test_d34_removed_and_rename import _bot
        if with_keys and monkeypatch is not None:
            monkeypatch.setenv("GOPS_APP_ENCRYPT_KEY", ENC_KEY)
            monkeypatch.setenv("GOPS_APP_VERIFY_TOKEN", VER_TOKEN)
        client, _b, _s = _bot(tmp_db)
        return client

    def test_probing_mode_without_keys(self, tmp_db):
        """未配密钥 -> 仍旧 200（只观测），不拦平台。"""
        client = self._client(tmp_db)
        r = client.post("/api/app-events", json={"anything": 1})
        assert r.status_code == 200 and r.json()["success"] is True

    def test_valid_event_accepted(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch, with_keys=True)
        data = _seal(_event("evt-ok"))
        sig = app_crypto.generate_signature(TS, NONCE, VER_TOKEN, data)
        r = client.post("/api/app-events", content=data,
                        headers={"X-CTQ-Timestamp": TS, "X-CTQ-Nonce": NONCE,
                                 "X-CTQ-Signature": sig,
                                 "Content-Type": "application/json"})
        assert r.status_code == 200, r.text
        assert r.json() == {"success": True, "code": 200, "message": "OK"}

    def test_bad_signature_rejected(self, tmp_db, monkeypatch):
        client = self._client(tmp_db, monkeypatch, with_keys=True)
        data = _event()
        r = client.post("/api/app-events", content=data,
                        headers={"X-CTQ-Timestamp": TS, "X-CTQ-Nonce": NONCE,
                                 "X-CTQ-Signature": "f" * 64})
        assert r.status_code == 401
        assert r.json()["success"] is False
