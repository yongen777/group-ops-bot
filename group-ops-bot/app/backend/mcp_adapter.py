# -*- coding: utf-8 -*-
"""MCP SSE 适配器（§2.13 技术栈：官方 MCP SDK，SSE 传输）。

实现要点：
- 首次调用时完成 MCP 握手：GET {base}/sse 建立事件流 -> 从 endpoint 事件
  取 messages 地址 -> POST initialize -> POST notifications/initialized
- tools/call 走 POST {messages_url}（JSON-RPC 2.0），响应经 SSE 事件流回传，
  按 id 配对等待（§4.11.1 契约的方法名 / 参数与 tools/ 注册一致）
- ⛔ 不自动重试：POST 失败 / 响应超时 / JSON-RPC error 一律抛 BackendError，
  由处理器归 C 类走恢复流程（§2.5.1 / §2.5.4）
- 测试可注入自建 httpx.Client（MockTransport），⛔ 不连真实服务

⚠️ 未实测项（§7.2）：协议版本协商（protocolVersion）与 SSE 事件形态以官方
SDK 实际行为为准；联调时如偏差，修订本文件并记入 §8，⛔ 不改契约迁就实现。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

import httpx

from .base import Backend, BackendError

LOG = logging.getLogger("group-ops.backend.mcp")

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "group-ops-bot", "version": "1.0.0"}
_IDLE_TIMEOUT_SECONDS = 65  # SSE keepalive 间隔通常 < 60s，留余量


class _SseSession:
    """一条 MCP SSE 会话（事件流传入 + JSON-RPC 收发）。"""

    def __init__(self, client: httpx.Client, sse_path: str = "/sse") -> None:
        self._client = client
        self._sse_path = sse_path
        self._messages_url: Optional[str] = None
        self._responses: dict = {}
        self._cond = threading.Condition()
        self._next_id = 0
        self._response: Optional[httpx.Response] = None
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self._last_activity_at: Optional[float] = time.monotonic()

    # ---------- 生命周期 ----------

    def start(self, timeout: float) -> None:
        request = self._client.build_request(
            "GET", self._sse_path,
            headers={"Accept": "text/event-stream"})
        self._response = self._client.send(request, stream=True)
        if self._response.status_code != 200:
            self._response.close()
            raise BackendError(
                "MCP SSE handshake failed: GET %s -> HTTP %s"
                % (self._sse_path, self._response.status_code))
        self._reader = threading.Thread(
            target=self._read_loop, args=(self._response,), daemon=True)
        self._reader.start()
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._messages_url is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError(
                        "MCP SSE handshake timeout: no endpoint event")
                self._cond.wait(remaining)
        self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout)
        # notifications/initialized：无 id，无响应
        self._post_notification("notifications/initialized")

    def close(self) -> None:
        self._closed = True
        if self._response is not None:
            try:
                self._response.close()
            except Exception:  # pragma: no cover - 关闭best-effort
                pass

    def is_closed(self) -> bool:
        """事件流是否已断开（读线程退出即置位）。"""
        return self._closed

    def alive(self) -> bool:
        """会话是否仍可用。

        ⛔ 两种失效都判 false：读线程已退出（`_closed`），或空闲超过
        ``_IDLE_TIMEOUT_SECONDS``（服务端 keepalive 通常 < 60s，届时流
        多半已被关闭，⛔ 不去赌它还活着）。
        """
        if self._closed or self._messages_url is None:
            return False
        if self._reader is None or not self._reader.is_alive():
            return False
        last = self._last_activity()
        if last is not None and (
                time.monotonic() - last) > _IDLE_TIMEOUT_SECONDS:
            return False
        return True

    def _last_activity(self) -> Optional[float]:
        """最近一次成功收发的单调时刻（无则 None）。"""
        with self._cond:
            return self._last_activity_at

    # ---------- JSON-RPC ----------

    def _rpc(self, method: str, params: dict, timeout: float) -> dict:
        """发一次请求并等待 SSE 回传的同 id 响应。⛔ 不重试。"""
        with self._cond:
            self._next_id += 1
            req_id = str(self._next_id)
        body = {"jsonrpc": "2.0", "id": req_id, "method": method,
                "params": params}
        with self._cond:
            self._last_activity_at = time.monotonic()
        try:
            resp = self._client.post(self._messages_url, json=body,
                                     timeout=timeout)
        except httpx.HTTPError as exc:
            raise BackendError("MCP POST %s failed: %s" % (method, exc))
        if resp.status_code not in (200, 202):
            raise BackendError("MCP POST %s -> HTTP %s"
                               % (method, resp.status_code))
        deadline = time.monotonic() + timeout
        with self._cond:
            while req_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise BackendError(
                        "MCP call %s timeout waiting for response id=%s"
                        % (method, req_id))
                self._cond.wait(remaining)
            return self._responses.pop(req_id)

    def _post_notification(self, method: str) -> None:
        try:
            self._client.post(self._messages_url,
                              json={"jsonrpc": "2.0", "method": method})
        except httpx.HTTPError as exc:
            raise BackendError("MCP notify %s failed: %s" % (method, exc))

    # ---------- SSE 读取 ----------

    def _read_loop(self, response: httpx.Response) -> None:
        event_name = None
        data_lines: list = []
        try:
            for raw in response.iter_lines():
                line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if line == "":
                    self._dispatch(event_name, data_lines)
                    event_name, data_lines = None, []
                    continue
                if line.startswith(":"):  # keepalive 注释
                    continue
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
        except Exception:  # pragma: no cover - 流中断由等待方超时发现
            pass
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def _dispatch(self, event_name: Optional[str], data_lines: list) -> None:
        data = "\n".join(data_lines).strip()
        if not data:
            return
        if event_name == "endpoint" or data.startswith("/"):
            with self._cond:
                self._messages_url = data
                self._cond.notify_all()
            return
        try:
            message = json.loads(data)
        except ValueError:
            return  # 非 JSON 事件（ping 等）忽略
        if not isinstance(message, dict):
            return
        msg_id = message.get("id")
        if msg_id is not None and ("result" in message or "error" in message):
            with self._cond:
                self._responses[str(msg_id)] = message
                self._last_activity_at = time.monotonic()
                self._cond.notify_all()
        else:
            # 无 id 的事件（notification / ping）也算活动，避免误判空闲
            with self._cond:
                self._last_activity_at = time.monotonic()


class McpSseAdapter(Backend):
    """经 SSE 连自研 MCP Server 的适配器（生产实现）。"""

    def __init__(self, base_url: Optional[str] = None,
                 timeout_seconds: int = 30, *,
                 client: Optional[httpx.Client] = None,
                 sse_path: str = "/sse") -> None:
        self._timeout = float(timeout_seconds)
        self._client = client or httpx.Client(
            base_url=base_url,
            timeout=httpx.Timeout(self._timeout))
        self._sse_path = sse_path
        self._session: Optional[_SseSession] = None
        self._idle_since: Optional[float] = None
        self._lock = threading.Lock()
        # ⭐ 2026-10-20（P0-2）：调用级串行锁。:18002 / :18003 / scheduler
        # 三个线程共享同一个适配器实例：只锁「取会话」不够 —— 两个线程可能
        # 同时拿到同一 session 并发操作同一个 httpx.Client（连接状态互相污染），
        # 或某线程判定会话失活后 close() 掉另一线程正在等响应的 _session
        #（在途 tools/call 半途断流）。故把 _ensure_session() 与 _rpc()
        # 收进同一把可重入锁里串行执行。
        # ⛔ 只串行化，不重试：已发出的 tools/call 失败仍抛 BackendError。
        self._call_lock = threading.RLock()

    # ---------- 会话 ----------

    def _ensure_session(self) -> _SseSession:
        """取可用的 MCP 会话，⛔ 失效时**自动重建一次**。

        2026-10-16（D4-2）：实测复用本身早已生效（首次 11ms / 后续 4ms），
        但**会话被服务端 keepalive 超时关闭后不会重建** —— `_session` 仍是
        非 None 的死对象，后续每次调用都会失败。
        这里补：`_closed` 或空闲超时（_IDLE_TIMEOUT_SECONDS）则重建。

        ⛔ 只重建**一次**：重建后仍失败照常抛 BackendError -> 业务归 C 类
        走恢复流程，⛔ 不无限重连、不静默吞。
        """
        with self._lock:
            if self._session is not None and not self._session.alive():
                LOG.info("MCP session stale (closed=%s), rebuilding",
                         self._session.is_closed())
                try:
                    self._session.close()
                except Exception:  # pragma: no cover - 关闭 best-effort
                    pass
                self._session = None
                self._idle_since = time.monotonic()
            if self._session is None:
                session = _SseSession(self._client, self._sse_path)
                session.start(self._timeout)
                self._session = session
                self._idle_since = None
                LOG.info("MCP session established (reuse enabled)")
            else:
                self._idle_since = None
            return self._session

    def _call_tool(self, name: str, arguments: dict) -> dict:
        # ⭐ 2026-10-20（P0-2）：`_call_lock` 覆盖「取会话 + 收发」整段 ——
        # 杜绝并发线程在 _rpc 等待期间把 _session 关闭或污染 Client 连接。
        # ⛔ 出了 with 才解析结果：解析失败与锁无关，且缩短持锁时间。
        with self._call_lock:
            session = self._ensure_session()
            message = session._rpc("tools/call",
                                   {"name": name, "arguments": arguments},
                                   self._timeout)
        if "error" in message:
            err = message["error"] or {}
            raise BackendError("MCP tool %s JSON-RPC error %s: %s"
                               % (name, err.get("code"), err.get("message")))
        result = message.get("result") or {}
        texts = [part.get("text", "") for part in result.get("content", [])
                 if isinstance(part, dict) and part.get("type") == "text"]
        if result.get("isError"):
            # ⭐ 2026-10-20：把工具侧错误正文带出来。旧实现只抛
            # 「returned isError」，日志里全无原因；线上一次 `queue.Empty`
            # （str 为空串）就这样变成「后台没数据」的黑盒，查了半小时。
            detail = " ".join(t for t in texts if t).strip()[:300]
            raise BackendError("MCP tool %s returned isError: %s"
                               % (name, detail or "(empty detail)"))
        if not texts:
            raise BackendError("MCP tool %s returned no text content" % name)
        try:
            payload = json.loads("\n".join(texts))
        except ValueError as exc:
            raise BackendError("MCP tool %s returned non-JSON: %s"
                               % (name, exc))
        if not isinstance(payload, dict):
            raise BackendError("MCP tool %s returned non-object payload" % name)
        return payload

    # ---------- Backend 接口 ----------

    def user_upsert(self, *, phone, name=None, platform_user_id=None,
                    department=None, source="learned", group_id=None) -> dict:
        return self._call_tool("user.upsert", {
            "phone": phone, "name": name,
            "platform_user_id": platform_user_id,
            "department": department, "source": source,
            "group_id": group_id})

    def user_get(self, *, phone) -> dict:
        return self._call_tool("user.get", {"phone": phone})

    def checkin_submit(self, *, user_id, group_id, period_key, session,
                       image_ref, image_meta=None, request_id,
                       status="NORMAL", entry_source="user",
                       checkin_time=None) -> dict:
        return self._call_tool("checkin.submit", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session,
            "image_ref": image_ref, "image_meta": image_meta,
            "status": status, "entry_source": entry_source,
            "checkin_time": checkin_time,
            "request_id": request_id})

    def checkin_query_status(self, *, user_id, group_id, period_key,
                             session) -> dict:
        return self._call_tool("checkin.query_status", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session})

    def checkin_query_records(self, *, group_id, period_key) -> dict:
        return self._call_tool("checkin.query_records",
                               {"group_id": group_id, "period_key": period_key})

    def summary_save(self, *, user_id, group_id, period_key, period_type,
                     content, request_id, entry_source="user") -> dict:
        return self._call_tool("summary.save", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "period_type": period_type,
            "content": content, "request_id": request_id,
            "entry_source": entry_source})

    def summary_query_status(self, *, user_id, group_id, period_key) -> dict:
        return self._call_tool("summary.query_status", {
            "user_id": user_id, "group_id": group_id, "period_key": period_key})

    def summary_query_records(self, *, group_id, period_key) -> dict:
        return self._call_tool("summary.query_records",
                               {"group_id": group_id, "period_key": period_key})

    def report_submit(self, *, user_id, group_id, period_key, role, items,
                      request_id, entry_source="user") -> dict:
        return self._call_tool("report.submit", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "role": role, "items": items,
            "request_id": request_id, "entry_source": entry_source})

    def report_query_status(self, *, user_id, group_id, period_key) -> dict:
        return self._call_tool("report.query_status", {
            "user_id": user_id, "group_id": group_id, "period_key": period_key})

    def report_query_records(self, *, group_id, period_key) -> dict:
        return self._call_tool("report.query_records",
                               {"group_id": group_id, "period_key": period_key})

    def op_query_by_request(self, *, request_id) -> dict:
        return self._call_tool("op.query_by_request",
                               {"request_id": request_id})

    # ---------- 管理员修改既有记录（D17） ----------

    def checkin_admin_update(self, *, user_id, group_id, period_key, session,
                             checkin_time=None, status=None) -> dict:
        return self._call_tool("checkin.admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session,
            "checkin_time": checkin_time, "status": status})

    def summary_admin_update(self, *, user_id, group_id, period_key,
                             content=None, submit_time=None) -> dict:
        return self._call_tool("summary.admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "content": content,
            "submit_time": submit_time})

    def report_admin_update(self, *, user_id, group_id, period_key,
                            items=None, submit_time=None) -> dict:
        return self._call_tool("report.admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "items": items,
            "submit_time": submit_time})

    def close(self) -> None:
        with self._lock:
            if self._session is not None:
                self._session.close()
                self._session = None
        self._client.close()
