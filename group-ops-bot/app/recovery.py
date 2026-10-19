# -*- coding: utf-8 -*-
"""C 类恢复处理器（§2.5.4 第①种重试 / §4.8 第 7-8 条 / §4.11.3 第 6 条）。

run_submission 归 C 时会挂恢复任务（recovery_task, pending，§2.5.6 第 1 条）。
本模块按**原请求键**经 op.query_by_request 查证，把结论补账：

- verdict success -> intent 置 confirmed + 落 receipt + 补消费绑定令牌
  （额度自此成立），任务 outcome=A
- verdict rejected -> intent 置 rejected，任务 outcome=B（不占额度，
  用户可修正重试 —— 换新请求键，⛔ 不经本模块；绑定令牌回 active）
- verdict unknown（含 found=False）-> attempts+1 + 指数退避重排；
  超 MAX_ATTEMPTS 或超 MAX_AGE_HOURS -> 记 security_event 告警管理员
  （⛔ 任务仍 pending，继续恢复，§2.5.5 第 5 条）
- BackendError（查证本身失败）-> ⛔ 不计 attempts（没得到结论），仅重排

硬约束（§2.5.6 第 8 条 / §2.5.4）：
- ⛔ 不自动重试（不重发写工具）、⛔ 不新建请求（不换 request_id）
- ⛔ 不当成功、不当失败、不当未完成；超期无法核实先告警并继续恢复
- 告警落 security_event(event_type=recovery_escalated) + 日志；
  管理员**触达**通道（:18003 后台 / 平台推送）未接通前，告警以落库 +
  日志为准，⛔ 不伪造已通知
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from . import period as period_mod
from .backend.base import Backend, BackendError
from .handlers.base import RECOVERY_DELAY_MINUTES
from .state import StateStore

LOG = logging.getLogger("group-ops.recovery")

# 查证退避：首次 unknown 后 BASE 分钟，之后指数增长，封顶 CAP
BACKOFF_CAP_MINUTES = 30
# 超限即告警（仍继续恢复）： attempts 上限 / 任务年龄上限
MAX_ATTEMPTS = 12
MAX_AGE_HOURS = 24

OUTCOME_A = "A"
OUTCOME_B = "B"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_QUERY_FAILED = "query_failed"


def _backoff_minutes(attempts: int) -> int:
    """第 attempts 次 unknown 后的等待分钟数（指数退避，封顶）。"""
    return min(RECOVERY_DELAY_MINUTES * (2 ** max(attempts - 1, 0)),
               BACKOFF_CAP_MINUTES)


def _parse_aware(value: str) -> Optional[datetime]:
    """ISO8601 -> TZ 感知时间（库内存 naive 也按服务端时区解释）。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=period_mod.TZ)


def _confirm(state: StateStore, row, record_id: str) -> None:
    """A 补账（§2.5.6 第 2 条）：intent confirmed + receipt + 令牌补消费。"""
    request_id = row["request_id"]
    state.update_intent_status(request_id, "confirmed")
    state.put_receipt(request_id, row["scope_key"], row["biz"], record_id)
    intent = state.get_intent(request_id)
    token_id = intent["token_id"] if intent else None
    if token_id:
        # C 发生在消费令牌之前；恢复判 A 后补消费（§2.4：成功即作废）
        state.consume_token(token_id, request_id)
    state.resolve_recovery_task(request_id, OUTCOME_A)


def _reject(state: StateStore, row) -> None:
    """B 补账：intent rejected，任务 resolved(B)。⛔ 不占额度、不启动再恢复。

    绑定令牌从 pending_verify 回 active：结论是明确拒绝，用户可修正重试
    （⛔ 不让一次被拒的提交把链接永久锁死）。
    """
    request_id = row["request_id"]
    state.update_intent_status(request_id, "rejected")
    intent = state.get_intent(request_id)
    token_id = intent["token_id"] if intent else None
    if token_id:
        state.reactivate_token(token_id)
    state.resolve_recovery_task(request_id, OUTCOME_B)


def _escalate(state: StateStore, row, attempts: int, age_hours: float) -> None:
    """超限告警（§2.5.5 第 5 条）：记 security_event + 日志；⛔ 任务仍 pending。"""
    state.mark_recovery_escalated(row["request_id"])
    intent = state.get_intent(row["request_id"])
    group_id = intent["group_id"] if intent else None
    phone = intent["phone"] if intent else None
    detail = ("C 类恢复超限仍未核实：biz=%s attempts=%s age_hours=%.1f "
              "request_id=%s（⛔ 不自动重试、不新建请求，继续按原键查证）"
              % (row["biz"], attempts, age_hours, row["request_id"]))
    state.record_security_event("recovery_escalated", group_id, phone, detail)
    LOG.warning("recovery escalated: %s", detail)


def process_task(state: StateStore, backend: Backend, row, now: datetime) -> dict:
    """处理单个到期恢复任务。返回 {request_id, outcome, ...} 摘要。

    ⛔ 不向调用方抛 BackendError（查证失败的结论就是「继续等」）；
    其他异常上抛由 tick 统一兜（⛔ 单任务异常不拖死整轮）。
    """
    request_id = row["request_id"]
    try:
        verdict = backend.op_query_by_request(request_id=request_id)
    except BackendError as exc:
        # 查证本身失败：没得到任何结论 -> ⛔ 不计 attempts，短退避后重试
        state.reschedule_recovery(
            request_id,
            now + timedelta(minutes=RECOVERY_DELAY_MINUTES),
            _short_error(exc))
        return {"request_id": request_id, "outcome": OUTCOME_QUERY_FAILED}

    status = (verdict or {}).get("status")
    if status == "success":
        record_id = verdict.get("record_id")
        _confirm(state, row, "" if record_id is None else str(record_id))
        LOG.info("recovery resolved A: request_id=%s record_id=%s",
                 request_id, record_id)
        return {"request_id": request_id, "outcome": OUTCOME_A,
                "record_id": record_id}
    if status == "rejected":
        _reject(state, row)
        LOG.info("recovery resolved B: request_id=%s", request_id)
        return {"request_id": request_id, "outcome": OUTCOME_B}

    # unknown（含 found=False：请求可能从未到达 MCP，⛔ 不当失败）
    # 重申「待核验」标记（幂等；run_submission 归 C 时已置过，§2.5.5 第 3 条）
    state.update_intent_status(request_id, "unknown")
    attempts_after = int(row["attempts"] or 0) + 1
    due = now + timedelta(minutes=_backoff_minutes(attempts_after))
    attempts = state.bump_recovery_attempt(request_id, due, None)
    result = {"request_id": request_id, "outcome": OUTCOME_UNKNOWN,
              "attempts": attempts, "escalated": False}
    created = _parse_aware(row["created_at"])
    age_hours = ((now - created).total_seconds() / 3600.0
                 if created else 0.0)
    if (attempts >= MAX_ATTEMPTS or age_hours >= MAX_AGE_HOURS) \
            and not row["escalated"]:
        _escalate(state, row, attempts, age_hours)
        result["escalated"] = True
    return result


def tick(state: StateStore, backend: Backend,
         now: Optional[datetime] = None) -> dict:
    """一轮恢复查证（调度线程 / 启动扫描共用）。

    ⛔ 不静默：每类的处理数都进摘要；单任务异常记 errors 继续。
    """
    now = now or period_mod.now()
    summary = {"processed": 0, OUTCOME_A: 0, OUTCOME_B: 0,
               OUTCOME_UNKNOWN: 0, OUTCOME_QUERY_FAILED: 0,
               "escalated": 0, "errors": []}
    for row in state.due_recovery_tasks(now):
        try:
            result = process_task(state, backend, row, now)
        except Exception as exc:  # ⛔ 单任务异常不拖死整轮
            LOG.exception("recovery task failed: request_id=%s",
                          row["request_id"])
            summary["errors"].append({"request_id": row["request_id"],
                                      "error": _short_error(exc)})
            continue
        summary["processed"] += 1
        summary[result["outcome"]] += 1
        if result.get("escalated"):
            summary["escalated"] += 1
    return summary


def recover_on_startup(state: StateStore, backend: Backend) -> dict:
    """§4.9 第 5 条：启动时先跑恢复扫描，再开对外服务。best-effort。"""
    summary = tick(state, backend)
    if summary["processed"]:
        LOG.info("startup recovery scan: %s", summary)
    return summary


def _short_error(exc: Exception) -> str:
    """异常摘要（脱敏：只取类型 + 简短消息，⛔ 不整段落库）。"""
    text = str(exc).replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "..."
    return "%s: %s" % (type(exc).__name__, text)
