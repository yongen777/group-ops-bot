# -*- coding: utf-8 -*-
"""令牌签发与校验（§2.4）。

硬约束：
- 服务端生成高强度随机不透明值（secrets，⛔ 不用可预测序列）
- 令牌绑定：群 / 用户手机号 / 业务类型 / 周期键 / 时段（打卡）/ 创建时间 /
  有效期 / 提交状态 —— 缺一不可
- 最小授权：仅相应业务/用户/群/周期的一次提交凭证，不得跨群跨业务
- 令牌位置：⛔ 不放 URL query，用 URL fragment 由浏览器读取，HTTPS POST 回传
- 有效期（用户指令第七阶段收紧）：**签发后 30 分钟**为硬上限，
  ⛔ 任何调用方传更长的 expires_at 都不生效（在 new_record 内统一封顶）；
  原 §2.4「到本业务周期截止后 24 小时为止」的口径由本条覆盖，
  冲突已记入待确认项（Q-H5），⛔ 不得两边各说一套
- 首次正式提交成功后立即标记完成，禁止再建第二条记录
- 提交发起即置 pending_verify：结论未定期间同一链接 ⛔ 不能再次独立提交
  （明确失败 / 恢复判 B 才回 active 供修正重试；用户指令 P1 验收项 5）
- 安全重试：⛔ 不得因令牌已用就把重试判为全新提交；须返回原结果或查到已完成
- 重开已完成页：可显示「已完成」，但不可再次独立提交
- 拒绝规则：旧/过期/越权/篡改令牌一律拒绝
- 日志：令牌 ⛔ 不写入普通访问日志

存储：令牌记录落机器人侧 SQLite（state.py 的 tokens 表）。
链接本身即访问凭证 —— 身份风险声明见 §2.4 末段，不得声称可完全防止转发。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

from . import period as period_mod

# 令牌有效期硬上限（用户指令第七阶段）：签发后 30 分钟。
# ⛔ 这是天花板而非默认值：new_record 内对 expires_at 统一取 min，
#    调用方无法绕过（fail-closed）。
TOKEN_TTL_MINUTES = 30


class TokenState(str, Enum):
    ACTIVE = "active"          # 可提交
    PENDING_VERIFY = "pending_verify"  # 提交已发起、结论未定（⛔ 禁重复提交）
    CONSUMED = "consumed"      # 已成功提交（一次提交凭证已用）
    EXPIRED = "expired"        # 已过期
    REVOKED = "revoked"        # 管理员作废（预留）


class RejectReason(str, Enum):
    NOT_FOUND = "not_found"        # 不存在（含被篡改：查不到即拒绝）
    EXPIRED = "expired"
    WRONG_USER = "wrong_user"      # 越权：非令牌绑定人
    WRONG_GROUP = "wrong_group"    # 越权：非令牌绑定群
    WRONG_BIZ = "wrong_biz"        # 越权：非令牌绑定业务
    WRONG_PERIOD = "wrong_period"  # 越权：非令牌绑定周期
    ALREADY_CONSUMED = "already_consumed"  # 已成功提交过（安全重试见下）
    SUBMITTING = "submitting"      # 提交处理中（pending_verify，⛔ 禁重复提交）


# 拒绝话术单一真源（用户指令第七阶段：规范话术）。
# ⛔ 各业务处理器不得自造一套措辞 —— 一律走 reject_message()，
#    否则「同一个拒绝原因在打卡页和报数页说法不同」会直接暴露给用户。
BIZ_COMMAND_TEXT = {
    "checkin": "打卡",
    "summary": "总结",
    "report": "报数",
}

REJECT_MESSAGE = {
    RejectReason.NOT_FOUND:
        "链接无效或已被作废，请返回群聊重新发送「{cmd}」获取新链接",
    RejectReason.EXPIRED:
        "链接已超过 {ttl} 分钟有效期，请返回群聊重新发送「{cmd}」获取新链接",
    RejectReason.WRONG_USER:
        "该链接不属于当前用户，请使用本人链接",
    RejectReason.WRONG_GROUP:
        "该链接不适用于当前群，请在本群重新发送「{cmd}」",
    RejectReason.WRONG_BIZ:
        "该链接不适用于当前业务，请重新发送「{cmd}」",
    RejectReason.WRONG_PERIOD:
        "该链接不适用于当前周期，请重新发送「{cmd}」获取新链接",
    RejectReason.ALREADY_CONSUMED:
        "该链接已提交完成，无需重复提交",
    RejectReason.SUBMITTING:
        "该链接的提交正在处理中，请稍候再试（请勿重复提交）",
}

# 越权 / 篡改类：除拒绝外还要记安全事件，运维可查（§2.4 拒绝规则留痕）
REJECT_SECURITY_EVENT = {
    RejectReason.WRONG_USER: "token_wrong_user",
    RejectReason.WRONG_GROUP: "token_wrong_group",
    RejectReason.WRONG_BIZ: "token_wrong_biz",
    RejectReason.WRONG_PERIOD: "token_wrong_period",
    RejectReason.NOT_FOUND: "token_not_found",
}


def reject_message(reason: RejectReason, biz: str) -> str:
    """按拒绝原因取规范话术（{cmd} 填本业务入口命令名，{ttl} 填有效期上限）。"""
    template = REJECT_MESSAGE.get(reason)
    if template is None:
        return "链接无效，请返回群聊重新发送命令获取新链接"
    return template.format(cmd=BIZ_COMMAND_TEXT.get(biz, "命令"),
                           ttl=TOKEN_TTL_MINUTES)


@dataclass
class TokenRecord:
    """SQLite tokens 表的一行（不含明文令牌本体之外的敏感信息）。"""

    token_id: str            # 令牌内部 id（哈希值，⛔ 不存明文）
    phone: str               # 绑定用户手机号
    group_id: str            # 绑定群
    biz: str                 # checkin / summary / report
    period_key: str          # 绑定周期键
    session: Optional[str]   # 打卡时段（am/pm），其他业务为 None
    created_at: datetime
    expires_at: datetime
    state: str               # TokenState 值
    consumed_request_id: Optional[str] = None  # 成功提交时的请求键（安全重试回原结果用）


def generate_token_value() -> str:
    """高强度随机不透明值（URL-safe，32 字节熵）。"""
    return secrets.token_urlsafe(32)


def token_id_of(value: str) -> str:
    """令牌存储 id = SHA-256(value)。

    ⛔ 数据库不存令牌明文：库泄露不等于提交凭证泄露。
    fragment 里传给浏览器的是明文 value，服务端只认其哈希。
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_eq(a: str, b: str) -> bool:
    """恒定时间比较，防时序侧信道（篡改令牌爆破）。"""
    return hmac.compare_digest(a, b)


def new_record(
    value: str,
    phone: str,
    group_id: str,
    biz: str,
    period_key: str,
    session: Optional[str],
    now: Optional[datetime] = None,
    expires_at: Optional[datetime] = None,
) -> TokenRecord:
    """签发一条令牌记录（落库前调用）。

    过期时刻 = min(调用方期望值 / 周期窗口值, now + TOKEN_TTL_MINUTES)。
    ⛔ 30 分钟是硬上限：调用方（如打卡「时段截止 + 24h」）传更长时间一律被封顶。
    """
    now = now or period_mod.now()
    requested = expires_at or period_mod.token_expiry(biz, period_key, session)
    expires = min(requested, now + timedelta(minutes=TOKEN_TTL_MINUTES))
    return TokenRecord(
        token_id=token_id_of(value),
        phone=phone,
        group_id=group_id,
        biz=biz,
        period_key=period_key,
        session=session,
        created_at=now,
        expires_at=expires,
        state=TokenState.ACTIVE.value,
    )


def check_authorization(
    record: Optional[TokenRecord],
    value: str,
    phone: str,
    group_id: str,
    biz: str,
    period_key: str,
    session: Optional[str],
    now: Optional[datetime] = None,
) -> Optional[RejectReason]:
    """令牌校验（§2.4 拒绝规则 + 最小授权）。

    返回 None 表示通过；返回 RejectReason 表示拒绝。
    注意顺序：先存在性（含篡改 -> not_found），再越权，再过期，再状态。
    """
    now = now or period_mod.now()
    if record is None:
        return RejectReason.NOT_FOUND
    if not constant_time_eq(record.token_id, token_id_of(value)):
        return RejectReason.NOT_FOUND
    if record.phone != phone:
        return RejectReason.WRONG_USER
    if record.group_id != group_id:
        return RejectReason.WRONG_GROUP
    if record.biz != biz:
        return RejectReason.WRONG_BIZ
    if record.period_key != period_key:
        return RejectReason.WRONG_PERIOD
    if record.biz == "checkin" and session is not None \
            and record.session != session:
        # 时段绑定：仅在调用方给出当前时段时校验（session=None 表示调用方
        # 尚未解析到开放区间，此时不得用时段不匹配抢先拒绝——越权/过期等
        # 硬拒绝要先于业务时段话术，否则跨群令牌在非开放时段会静默漏审）
        return RejectReason.WRONG_PERIOD
    if record.state == TokenState.CONSUMED.value:
        return RejectReason.ALREADY_CONSUMED
    if record.state == TokenState.REVOKED.value:
        return RejectReason.NOT_FOUND
    if now >= record.expires_at:
        return RejectReason.EXPIRED
    if record.state == TokenState.PENDING_VERIFY.value:
        # 提交已发起、结论未定：⛔ 禁止同链接再次独立提交（用户指令 P1 验收项 5）
        return RejectReason.SUBMITTING
    if record.state != TokenState.ACTIVE.value:
        return RejectReason.NOT_FOUND
    return None
