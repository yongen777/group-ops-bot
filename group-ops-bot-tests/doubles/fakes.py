# -*- coding: utf-8 -*-
"""MCP Server 测试替身（§2.8 / §2.13：替身只存在于测试目录，⛔ 不进生产包）。

⚠️ 这些替身只验证**逻辑分支**（幂等判定 / 结果归类 / 契约完整性），
⛔ 不构成「MCP Server 与 MySQL 联通」的证据 —— 后者必须真实 MySQL
测试库 + DDL 批准后执行（§4.11.5 / §9.3）。
"""

from __future__ import annotations

from contextlib import contextmanager


class FakeCursor:
    """记录 SQL 调用；fetchone 行为由队列控制。"""

    def __init__(self, fetchone_values=None):
        self.calls = []
        self._fetchone_values = list(fetchone_values or [])
        self.lastrowid = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def executemany(self, sql, seq):
        self.calls.append((sql, list(seq)))

    def fetchone(self):
        return self._fetchone_values.pop(0) if self._fetchone_values else None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass

    def ping(self, reconnect=True):
        return True


class FakePool:
    """最小池替身：始终返回同一个 FakeConn（单线程测试）。"""

    def __init__(self, cursor=None):
        self.cursor = cursor or FakeCursor()
        self.conn = FakeConn(self.cursor)
        self.acquired = 0

    @contextmanager
    def connection(self):
        self.acquired += 1
        yield self.conn


class RecordingSender:
    """回推发送替身：记录全部回推（⛔ 只存在于测试目录，§2.8）。"""

    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail
        # 阶段八：出站三口替身。push / file 与生产适配器同口径 ——
        # 能力未确认前默认失败（⛔ 不伪造已发送/已送达），
        # 测试显式置 *_ok=True 模拟「实测接通后」。
        self.pushed = []
        self.push_ok = False
        self.files_sent = []
        self.file_ok = False

    def reply(self, reply_url: str, text: str, mention_phones=None,
              group_id: str = "") -> bool:
        """回推替身：记录文本 + @ 对象（2026-10-16 用户指令一：群聊回复必 @）。"""
        if self.fail:
            return False
        self.sent.append({"reply_url": reply_url, "text": text,
                           "mention_phones": list(mention_phones or []),
                           "group_id": group_id})
        return True

    def push_text(self, group_id: str, text: str, mention_phones=None):
        """主动推送替身：默认未接通（§5.2），如实返回未发送。

        mention_phones：真实 @ 手机号列表（2026-10-16 起与生产适配器同口径；
        打卡成功通知 / 提醒共用）。⛔ 只记录，不伪造已发送。
        """
        if self.fail or not self.push_ok:
            return False, "recording sender: push unconfirmed"
        self.pushed.append({"group_id": group_id, "text": text,
                            "mention_phones": list(mention_phones or [])})
        return True, ""

    def send_file(self, group_id: str, path, filename=None):
        """群内文件发送替身：默认未接通（D12），如实返回未送达。"""
        if self.fail or not self.file_ok:
            return False, "recording sender: file send unconfirmed"
        self.files_sent.append({"group_id": group_id, "path": str(path),
                                "filename": filename})
        return True, ""


class FakeBackend:
    """Backend 端口替身（§4.8 / §4.11.1 契约面）。

    - 每个方法的返回可脚本化；默认返回成功形态
    - calls 记录全部调用（断言契约参数用）
    - raise_on 可注入 BackendError 模拟后端不可达（C 类路径）
    """

    def __init__(self):
        self.calls = []
        self.raise_on = set()          # {"checkin_submit", ...}
        self.results = {}              # 方法名 -> 返回值覆盖
        self.next_user_id = 1000

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))

    def _maybe_raise(self, name):
        if name in self.raise_on:
            from app.backend.base import BackendError
            raise BackendError("fake backend unreachable: %s" % name)

    def user_upsert(self, *, phone, name=None, platform_user_id=None,
                    department=None, source="learned", group_id=None):
        self._record("user_upsert", {"phone": phone, "name": name,
                                     "group_id": group_id, "source": source})
        self._maybe_raise("user_upsert")
        self.next_user_id += 1
        return {"user_id": self.next_user_id, "created": True}

    def user_get(self, *, phone):
        self._record("user_get", {"phone": phone})
        self._maybe_raise("user_get")
        return {"user": {"id": 7, "phone": phone, "name": "测试用户"}}

    def checkin_submit(self, *, user_id, group_id, period_key, session,
                       image_ref, image_meta=None, request_id,
                       status="NORMAL", entry_source="user"):
        self._record("checkin_submit", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session,
            "image_ref": image_ref, "image_meta": image_meta,
            "request_id": request_id,
            "status": status, "entry_source": entry_source})
        self._maybe_raise("checkin_submit")
        return self.results.get("checkin_submit", {
            "status": "success", "record_id": 42,
            "duplicate": False, "message": "打卡成功"})

    def checkin_query_status(self, *, user_id, group_id, period_key, session):
        self._record("checkin_query_status", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session})
        self._maybe_raise("checkin_query_status")
        return self.results.get("checkin_query_status", {
            "completed": False, "record_id": None, "checkin_time": None})

    def checkin_query_records(self, *, group_id, period_key):
        self._record("checkin_query_records",
                     {"group_id": group_id, "period_key": period_key})
        self._maybe_raise("checkin_query_records")
        return self.results.get("checkin_query_records", {"records": []})

    def summary_save(self, *, user_id, group_id, period_key, period_type,
                     content, request_id, entry_source="user"):
        self._record("summary_save", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "period_type": period_type,
            "content": content, "request_id": request_id,
            "entry_source": entry_source})
        self._maybe_raise("summary_save")
        return self.results.get("summary_save", {
            "status": "success", "record_id": 9,
            "duplicate": False, "message": "保存成功"})

    # ---------- 管理员修改既有记录（D17） ----------

    def checkin_admin_update(self, *, user_id, group_id, period_key, session,
                             checkin_time=None, status=None):
        self._record("checkin_admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "session": session,
            "checkin_time": checkin_time, "status": status})
        self._maybe_raise("checkin_admin_update")
        return self.results.get("checkin_admin_update", {
            "status": "success", "record_id": None, "message": "ok"})

    def summary_admin_update(self, *, user_id, group_id, period_key,
                             content=None, submit_time=None):
        self._record("summary_admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "content": content,
            "submit_time": submit_time})
        self._maybe_raise("summary_admin_update")
        return self.results.get("summary_admin_update", {
            "status": "success", "record_id": None, "message": "ok"})

    def report_admin_update(self, *, user_id, group_id, period_key,
                            items=None, submit_time=None):
        self._record("report_admin_update", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "items": items,
            "submit_time": submit_time})
        self._maybe_raise("report_admin_update")
        return self.results.get("report_admin_update", {
            "status": "success", "record_id": None, "message": "ok"})

    def summary_query_status(self, *, user_id, group_id, period_key):
        self._record("summary_query_status", {
            "user_id": user_id, "group_id": group_id, "period_key": period_key})
        self._maybe_raise("summary_query_status")
        return self.results.get("summary_query_status",
                                {"completed": False, "record_id": None})

    def summary_query_records(self, *, group_id, period_key):
        self._record("summary_query_records",
                     {"group_id": group_id, "period_key": period_key})
        self._maybe_raise("summary_query_records")
        return self.results.get("summary_query_records", {"records": []})

    def report_submit(self, *, user_id, group_id, period_key, role, items,
                      request_id, entry_source="user"):
        self._record("report_submit", {
            "user_id": user_id, "group_id": group_id,
            "period_key": period_key, "role": role, "items": items,
            "request_id": request_id, "entry_source": entry_source})
        self._maybe_raise("report_submit")
        return self.results.get("report_submit", {
            "status": "success", "record_id": 5,
            "duplicate": False, "message": "报数成功"})

    def report_query_status(self, *, user_id, group_id, period_key):
        self._record("report_query_status", {
            "user_id": user_id, "group_id": group_id, "period_key": period_key})
        self._maybe_raise("report_query_status")
        return self.results.get("report_query_status",
                                {"completed": False, "record_id": None})

    def report_query_records(self, *, group_id, period_key):
        self._record("report_query_records",
                     {"group_id": group_id, "period_key": period_key})
        self._maybe_raise("report_query_records")
        return self.results.get("report_query_records", {"records": []})

    def op_query_by_request(self, *, request_id):
        self._record("op_query_by_request", {"request_id": request_id})
        self._maybe_raise("op_query_by_request")
        return self.results.get("op_query_by_request",
                                {"found": False})
