# -*- coding: utf-8 -*-
"""管理后台鉴权（:18003，§4.7）。

安全底线（§4.7 关键约束，全部落地）：
- 密码单向哈希（PBKDF2-SHA256，26 万次迭代 + 每盐随机）；⛔ 不存明文
- 初始凭据只从环境变量引导（GOPS_ADMIN_USERNAME / GOPS_ADMIN_PASSWORD），
  首次登录**强制改密**；改密后初始凭据不可复用（哈希已换）
- 服务端会话：令牌随机高强度，库内只存 SHA-256 哈希；
  Cookie HttpOnly + SameSite=Lax + Secure（按部署 scheme 自动/可配）
- 登录限流：同 用户名+IP 失败 5 次锁 15 分钟（DB 持久，重启不失效）
- CSRF：会话绑定令牌 + X-CSRF-Token 头校验 + Origin 校验（双保险）
- ⛔ 凭据不进前端代码、不进日志、不进审计 detail

端口边界：:18003 默认仅内网 / VPN（⛔ 不申请公网映射，§2.10）；
本模块的鉴权是第二道防线，⛔ 不因「已在内网」而省略任何一项。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

from ..config import ConfigError
from ..state import (LOGIN_LOCK_SECONDS, LOGIN_MAX_FAILURES, StateStore,
                     utcnow_iso)

SESSION_COOKIE_NAME = "gops_admin_session"
CSRF_HEADER_NAME = "X-CSRF-Token"


def _env_int(name: str, default: int) -> int:
    """环境变量取整数（缺省 / 非法 -> default；⛔ 不抛异常打断启动）。"""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ⭐ 2026-10-19 用户指令：**凭证过期前一直保持登录**（⛔ 不再半小时无操作就踢）
# - 绝对有效期默认 168 小时（7 天），env `GOPS_ADMIN_SESSION_HOURS` 可调
# - 空闲超时默认 0 = **关闭**（0 表示不因空闲失效），
#   env `GOPS_ADMIN_SESSION_IDLE_MINUTES` 需要时可重新开启（如共享终端场景）
SESSION_ABSOLUTE_HOURS = max(1, _env_int("GOPS_ADMIN_SESSION_HOURS", 168))
SESSION_IDLE_MINUTES = max(0, _env_int("GOPS_ADMIN_SESSION_IDLE_MINUTES", 0))

PBKDF2_ITERATIONS = 260_000
MIN_PASSWORD_LEN = 8
# 管理员账号名长度（⭐ 2026-10-20 用户指令：后台允许改登录账号名）
USERNAME_MIN_LEN = 2
USERNAME_MAX_LEN = 32


# ---------- 密码哈希 ----------

def hash_password(password: str) -> str:
    """PBKDF2-SHA256；存储格式 pbkdf2_sha256$iterations$salt_hex$hash_hex。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, salt.hex(),
                                       digest.hex())


def verify_password(password: str, stored: str) -> bool:
    """恒定时间校验；形态不符一律 False（⛔ 不抛异常泄露细节）。"""
    try:
        algorithm, iterations, salt_hex, hash_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------- 身份 / 会话 ----------

@dataclass
class AdminIdentity:
    """一次已鉴权管理请求的身份（⛔ 不含密码材料）。"""
    user_id: int
    username: str
    must_change_password: bool
    csrf_token: str
    session_token_hash: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_session_token() -> str:
    """会话令牌明文（只出现在 Cookie 响应里；库内存哈希）。"""
    return secrets.token_urlsafe(32)


def client_ip(request) -> str:
    """取客户端 IP（⛔ 可信代理未确认前只认直连地址，不读 X-Forwarded-For）。"""
    return request.client.host if request.client else "unknown"


def cookie_secure(request, configured: str = "auto") -> bool:
    """Secure Cookie 开关。

    auto：按请求 scheme 判定（https -> Secure）。内网 http 部署下若强制
    Secure，浏览器将丢弃 Cookie 导致登录不可用 —— 这是已知的部署形态
    权衡（§4.7 要求 Secure，但确认形态为内网 http，见 §2.10），
    生产 HTTPS 就绪后置 GOPS_ADMIN_COOKIE_SECURE=always 强制。
    """
    if configured == "always":
        return True
    if configured == "never":
        return False
    try:
        return request.url.scheme == "https"
    except Exception:  # pragma: no cover - 无请求上下文时保守
        return False


def check_origin(request) -> bool:
    """Origin 校验：带头则其 host 必须与本站一致（防跨站 POST）。

    ⚠️ 仅在内网 http 下 SameSite=Lax + 本校验 + CSRF 令牌三重防护；
    HTTPS 就绪后 Secure Cookie 生效，防护面完整。
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # 非浏览器客户端（curl/测试）不带 Origin；CSRF 令牌仍强制
    try:
        parts = urlsplit(origin)
        origin_host = parts.netloc
    except ValueError:
        return False
    if not origin_host:
        return False
    return origin_host == request.url.netloc


# ---------- 登录 / 限流 ----------

def login(state: StateStore, username: str, password: str,
          ip: str) -> tuple:
    """校验凭据。返回 (ok, error)。

    - 锁定中 -> ("locked", None)
    - 用户不存在 / 密码错 -> ("invalid_credentials", None)（⛔ 同一话术，防枚举）
    - 成功 -> ("ok", AdminIdentity)（调用方负责签发会话 Cookie）
    ⛔ 不在返回值里携带任何密码材料。
    """
    now_iso = utcnow_iso()
    locked_until = state.login_locked_until(username, ip)
    if locked_until:
        try:
            if datetime.fromisoformat(locked_until) > datetime.now(
                    _state_tz()):
                return "locked", None
        except ValueError:
            pass
    user = state.get_admin_user(username)
    if user is None or not verify_password(password, user["password_hash"]):
        state.login_failure(username, ip, now_iso, LOGIN_LOCK_SECONDS)
        return "invalid_credentials", None
    state.reset_login_failures(username, ip)
    state.touch_admin_login(int(user["id"]))
    return "ok", AdminIdentity(
        user_id=int(user["id"]), username=user["username"],
        must_change_password=bool(user["must_change_password"]),
        csrf_token=secrets.token_urlsafe(24),
        session_token_hash="")


def _state_tz():
    from ..period import TZ
    return TZ


def create_session(state: StateStore, identity: AdminIdentity,
                   ip: str) -> str:
    """签发会话（identity 来自 login 的中间态，这里补 session_token_hash）。
    返回会话令牌明文（⛔ 只经 Cookie 下发，不进任何日志/响应体）。"""
    token = new_session_token()
    token_hash = _sha256(token)
    state.create_admin_session(
        token_hash, identity.user_id, identity.csrf_token,
        datetime.now(_state_tz()) + timedelta(hours=SESSION_ABSOLUTE_HOURS),
        ip, utcnow_iso())
    return token


def load_session(state: StateStore, token: str) -> Optional[AdminIdentity]:
    """Cookie -> 身份。过期（绝对/空闲）即删并返回 None；滑动续期。"""
    if not token:
        return None
    row = state.get_admin_session(_sha256(token))
    if row is None:
        return None
    from ..period import TZ
    now = datetime.now(TZ)
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
        last_seen = datetime.fromisoformat(row["last_seen_at"])
    except ValueError:
        state.delete_admin_session(_sha256(token))
        return None
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=TZ)
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=TZ)
    if now >= expires_at:
        # 绝对有效期到点 -> 失效（⛔ 这是唯一会让「凭证」过期的地方）
        state.delete_admin_session(_sha256(token))
        return None
    if SESSION_IDLE_MINUTES > 0 and (now - last_seen) > timedelta(
            minutes=SESSION_IDLE_MINUTES):
        # ⭐ 2026-10-19：空闲超时默认关闭（0）—— 用户要求凭证过期前保持登录；
        # 只有显式配置 GOPS_ADMIN_SESSION_IDLE_MINUTES>0 才启用该判定
        state.delete_admin_session(_sha256(token))
        return None
    state.touch_admin_session(_sha256(token), utcnow_iso())
    user = state.get_admin_user_by_id(int(row["user_id"]))
    if user is None:  # 用户被删（预留）：会话同步失效
        state.delete_admin_session(_sha256(token))
        return None
    return AdminIdentity(
        user_id=int(user["id"]), username=user["username"],
        must_change_password=bool(user["must_change_password"]),
        csrf_token=row["csrf_token"],
        session_token_hash=_sha256(token))


def destroy_session(state: StateStore, token: str) -> None:
    if token:
        state.delete_admin_session(_sha256(token))


def verify_csrf(identity: AdminIdentity, request) -> bool:
    """CSRF 令牌校验（恒定时间）+ Origin 校验。"""
    if not check_origin(request):
        return False
    provided = request.headers.get(CSRF_HEADER_NAME, "")
    if not provided:
        return False
    return hmac.compare_digest(provided, identity.csrf_token)


# ---------- 初始凭据引导（fail-closed） ----------

def bootstrap_admin(state: StateStore) -> str:
    """首次启动：无任何管理员时按环境变量建初始账号（强制改密）。

    - 已有管理员 -> 忽略环境变量（⛔ 初始凭据不可复用）
    - 缺失 / 过短 -> ConfigError（fail-closed：⛔ 不生成弱默认账号）
    返回 "created" / "existing"。
    """
    if state.count_admin_users() > 0:
        return "existing"
    username = os.environ.get("GOPS_ADMIN_USERNAME", "").strip()
    password = os.environ.get("GOPS_ADMIN_PASSWORD", "")
    if not username or not password:
        raise ConfigError(
            "GOPS_ADMIN_USERNAME / GOPS_ADMIN_PASSWORD required for "
            "first-time admin bootstrap; refusing to start (fail-closed)")
    if len(password) < MIN_PASSWORD_LEN:
        raise ConfigError(
            "GOPS_ADMIN_PASSWORD too short (min %d); refusing to start"
            % MIN_PASSWORD_LEN)
    state.create_admin_user(username, hash_password(password),
                            must_change_password=True)
    return "created"


def change_password(state: StateStore, identity: AdminIdentity,
                    old_password: str, new_password: str) -> tuple:
    """改密：校验旧密码 -> 换新哈希 -> 吊销该用户全部会话。

    返回 (ok, error)。新密码 ⛔ 不与旧密码相同、⛔ 短于下限。
    """
    user = state.get_admin_user_by_id(identity.user_id)
    if user is None:
        return False, "用户不存在"
    if not verify_password(old_password, user["password_hash"]):
        return False, "原密码不正确"
    if new_password == old_password:
        return False, "新密码不能与原密码相同"
    if len(new_password) < MIN_PASSWORD_LEN:
        return False, "新密码长度至少 %d 位" % MIN_PASSWORD_LEN
    state.update_admin_password(identity.user_id, hash_password(new_password),
                                must_change_password=False)
    state.delete_admin_sessions_for_user(identity.user_id)
    return True, None


def change_username(state: StateStore, identity: AdminIdentity,
                    new_username: str, password: str) -> tuple:
    """改登录账号名（⭐ 2026-10-20 用户指令）。

    返回 (ok, error)。规则：
    - 必须先校验**当前密码**（账号名 = 登录凭据，⛔ 不能仅凭会话就改）
    - 2~32 位、⛔ 不含空白；⛔ 与当前同名；⛔ 与他人重名（UNIQUE 兜底）
    - 改完**不吊销会话**：load_session 每次按 user_id 回读用户名，立即生效
    """
    user = state.get_admin_user_by_id(identity.user_id)
    if user is None:
        return False, "用户不存在"
    if not verify_password(password, user["password_hash"]):
        return False, "密码不正确"
    name = (new_username or "").strip()
    if not (USERNAME_MIN_LEN <= len(name) <= USERNAME_MAX_LEN):
        return False, "账号名长度需在 %d~%d 位之间" % (USERNAME_MIN_LEN,
                                                      USERNAME_MAX_LEN)
    if any(ch.isspace() for ch in name):
        return False, "账号名不能包含空格"
    if name == identity.username:
        return False, "新账号名与当前账号名相同"
    if state.get_admin_user(name) is not None:
        return False, "该账号名已被占用"
    state.update_admin_username(identity.user_id, name)
    return True, None


__all__ = [
    "AdminIdentity", "SESSION_COOKIE_NAME", "CSRF_HEADER_NAME",
    "SESSION_ABSOLUTE_HOURS", "SESSION_IDLE_MINUTES", "MIN_PASSWORD_LEN",
    "LOGIN_MAX_FAILURES", "LOGIN_LOCK_SECONDS",
    "hash_password", "verify_password", "login", "create_session",
    "load_session", "destroy_session", "verify_csrf", "check_origin",
    "client_ip", "cookie_secure", "bootstrap_admin", "change_password",
    "change_username", "USERNAME_MIN_LEN", "USERNAME_MAX_LEN",
]
