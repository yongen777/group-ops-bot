# -*- coding: utf-8 -*-
"""自建应用「事件订阅」回调的验签与解密（D41，2026-10-23）。

算法来源：官方 Demo `callback-demo/internal/encryptor/default.go`
（2026-09-22 交付版），本项目按其规则用 Python 重写：

    签名 = HMAC-SHA256(key=验证 Token, msg = timestamp + nonce + data)
    会话密钥 = SHA256(加密 Key + ":" + timestamp + ":" + nonce)   → 32 字节
    IV      = base64decode(data)[:16]
    密文    = base64decode(data)[16:]
    明文    = AES-256-CBC 解密 + PKCS#7 去填充

⚠️ 两个 Key 都以**后台原样字符串**参与运算，⛔ 不要先做 base64 解码
（解码后反而解不开，2026-10-23 实测）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from typing import Optional

LOG = logging.getLogger("group-ops.app_crypto")

BLOCK = 16


def _aes_mods():
    """惰性导入：缺 pycryptodome 时模块仍可加载，⛔ 不让服务起不来。"""
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    return AES, unpad


def generate_signature(timestamp: str, nonce: str, verification_token: str,
                       data: str) -> str:
    """`HMAC-SHA256(token, timestamp + nonce + data)` 的十六进制小写。"""
    msg = "%s%s%s" % (timestamp, nonce, data)
    return hmac.new(verification_token.encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def verify(timestamp: str, nonce: str, signature: str,
           verification_token: str, data: str) -> bool:
    """常量时间比较，避免计时侧信道。"""
    if not (timestamp and nonce and signature and verification_token):
        return False
    expected = generate_signature(timestamp, nonce, verification_token, data)
    return hmac.compare_digest(expected, signature.lower())


def derive_session_key(encrypt_key: str, timestamp: str, nonce: str) -> bytes:
    """会话密钥随每条报文变化：SHA256(key:timestamp:nonce)。"""
    material = "%s:%s:%s" % (encrypt_key, timestamp, nonce)
    return hashlib.sha256(material.encode("utf-8")).digest()


def decrypt(encrypt_key: str, timestamp: str, nonce: str, data: str) -> str:
    """base64 解码 → 前 16 字节 IV → AES-256-CBC → PKCS#7。"""
    raw = base64.b64decode(data + "=" * (-len(data) % 4))
    if len(raw) < BLOCK * 2:
        raise ValueError("cipher too short")
    iv, ciphertext = raw[:BLOCK], raw[BLOCK:]
    if len(ciphertext) % BLOCK:
        raise ValueError("cipher not block aligned")
    AES, unpad = _aes_mods()
    key = derive_session_key(encrypt_key, timestamp, nonce)
    plain = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext), BLOCK)
    return plain.decode("utf-8")


def open_payload(encrypt_key: str, verification_token: str,
                 timestamp: str, nonce: str, signature: str,
                 data: str) -> Optional[dict]:
    """验签 + 解密 + JSON 解析。任一环节失败返回 None（⛔ 不抛给调用方）。"""
    if not verify(timestamp, nonce, signature, verification_token, data):
        LOG.warning("app event signature mismatch")
        return None
    try:
        text = decrypt(encrypt_key, timestamp, nonce, data)
        return json.loads(text)
    except Exception:
        LOG.exception("app event decrypt failed")
        return None
