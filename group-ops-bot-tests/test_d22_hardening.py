# -*- coding: utf-8 -*-
"""D22 生产加固回归（2026-10-20，接入多真实群前的六项修复）。

每一项都对应一个**线上会真出事**的现象，⛔ 不是防御性自嗨：

1. P0-1 自定义时段 id 9 字符 -> `checkin_record.session VARCHAR(8)` 写入崩
2. P0-2 多线程共享 McpSseAdapter -> 并发 _rpc / 会话被别的线程关掉
3. P0-3 一键催办全部业务被「打卡时段已过」整体覆盖 -> 总结/报数一条不发
4. P1-1 单群异常让所有群断更提醒
5. P0-4 关闭的业务 / 暂停考核的成员仍被算「未完成」-> 待补录名单误报
6. P1-2 自定义时段在导出里一律显示「下午」
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import commands
from app.admin import board as board_mod
from app.admin import stats_export
from app.backend.base import BackendError
from app.backend.mcp_adapter import McpSseAdapter
from app.handlers import checkin as checkin_handler
from app.handlers import reminder as reminder_handler
from app.handlers import stats as stats_handler
from app.period import TZ
from doubles.fakes import FakeBackend, FakeCursor, FakePool, RecordingSender
from helpers import GROUP, OTHER_PHONE, PHONE, enable_all, make_config, make_ctx
from mcp_server.db.dao import Dao
from test_reminder_stats import ReminderBackend, setup_group_with_required

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "group-ops-bot"
DDL = ROOT / "ddl" / "mysql_qsm_v1.sql"

SINGLE_SLOT = {"slots": [
    {"id": "am", "name": "上午", "start": "08:00", "end": "09:30"},
]}
T_0803 = datetime(2026, 10, 13, 8, 3, tzinfo=TZ)
T_1010 = datetime(2026, 10, 13, 10, 10, tzinfo=TZ)   # 上午场已结束


def _push_ok(group_id, text):
    return True, ""


def _planned_biz(tmp_db):
    rows = tmp_db._conn.execute(
        "SELECT DISTINCT biz FROM reminder_plan").fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------- 修复项 1 ----

class TestSlotIdLength:
    """P0-1：时段 id 必须装得进 checkin_record.session。"""

    def test_custom_slot_id_fits_column(self):
        """slot_8 之类的自定义 id 能通过校验（<= 16 字符）。"""
        assert checkin_handler._SLOT_ID_RE.match("slot_8")
        assert checkin_handler._SLOT_ID_RE.match("slot_12")
        assert not checkin_handler._SLOT_ID_RE.match("x" * 17)

    def test_ddl_session_column_is_16(self):
        """DDL 必须是 VARCHAR(16)，且附已建库环境的在线扩容语句。"""
        text = DDL.read_text(encoding="utf-8")
        assert re.search(r"`session`\s+VARCHAR\(16\)\s+NOT NULL", text)
        assert "ALTER TABLE checkin_record MODIFY COLUMN session VARCHAR(16)" \
            in text

    def test_frontend_adds_short_slot_ids(self):
        """前端新增时段 ⛔ 不再用 s + Date.now().toString(36)（9 字符）。

        这是本项目唯一能在 pytest 里锁住的 UI 约束：一旦改回时间戳 id，
        MySQL 严格模式会在群员打卡时直接 500，而单测/联调都不会触发。
        """
        tsx = (BOT / "admin-ui" / "src" / "components" / "system"
               / "GroupManagement.tsx").read_text(encoding="utf-8")
        assert "Date.now().toString(36)" not in tsx
        assert "`slot_${idx}`" in tsx

    def test_configured_slots_keeps_custom_name(self, tmp_db):
        """群配置里的自定义时段名要原样读出来（导出/提醒都依赖它）。"""
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"slots": [
            {"id": "slot_3", "name": "晚班", "start": "18:00", "end": "19:00"},
        ]})
        ctx = make_ctx(tmp_db, FakeBackend(), make_config(
            Path(str(tmp_db.db_path)).parent))
        slots = checkin_handler.configured_slots(ctx)
        assert [(s["id"], s["name"]) for s in slots] == [("slot_3", "晚班")]


# ---------------------------------------------------------------- 修复项 2 ----

class _SlowSession:
    """假 MCP 会话：_rpc 阻塞 50ms，记录同时在跑的线程数。"""

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def alive(self) -> bool:
        return True

    def is_closed(self) -> bool:
        return False

    def close(self) -> None:      # pragma: no cover - 不应被调用
        raise AssertionError("session must not be closed while in use")

    def _rpc(self, method, params, timeout):
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.05)
        with self._guard:
            self.active -= 1
        return {"jsonrpc": "2.0", "id": 1,
                "result": {"content": [{"type": "text",
                                        "text": '{"ok": true}'}],
                           "isError": False}}


class TestMcpAdapterSerialization:
    """P0-2：多线程共享适配器时，调用必须串行且会话不被误关。"""

    @staticmethod
    def _adapter():
        # base_url 必填（旧 httpx 不接受 None）；用例只走注入的假会话，
        # ⛔ 不会真的连网。
        return McpSseAdapter(base_url="http://mcp.test", timeout_seconds=5)

    def test_call_lock_exists_and_is_reentrant(self):
        adapter = self._adapter()
        assert isinstance(adapter._call_lock, type(threading.RLock()))
        # RLock 允许同线程重入（_ensure_session 内部若再次取锁不会自锁死）
        assert adapter._call_lock.acquire(timeout=0.5)
        adapter._call_lock.acquire(timeout=0.5)
        adapter._call_lock.release()
        adapter._call_lock.release()

    def test_concurrent_calls_are_serialized(self):
        """:18002 / :18003 / scheduler 三线程同时调用 ⛔ 不得交叉进 _rpc。"""
        adapter = self._adapter()
        session = _SlowSession()
        adapter._session = session

        errors = []

        def worker():
            try:
                adapter._call_tool("user.get", {"phone": PHONE})
            except Exception as exc:   # pragma: no cover - 失败要显形
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, errors
        assert session.max_active == 1, "并发进入了 _rpc（锁没覆盖住）"

    def test_no_retry_on_tool_error(self):
        """⛔ 保持架构铁律：JSON-RPC error 直接抛 BackendError，不重试。"""

        class _ErrSession(_SlowSession):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def _rpc(self, method, params, timeout):
                self.calls += 1
                return {"jsonrpc": "2.0", "id": 1,
                        "error": {"code": -32000, "message": "boom"}}

        adapter = self._adapter()
        session = _ErrSession()
        adapter._session = session
        with pytest.raises(BackendError):
            adapter._call_tool("user.get", {"phone": PHONE})
        assert session.calls == 1


# ---------------------------------------------------------------- 修复项 3 ----

class TestManualReminderAllBiz:
    """P0-3：一键催办全部业务 ⛔ 不该被「打卡时段已过」整体吞掉。"""

    def _setup(self, tmp_db):
        setup_group_with_required(tmp_db)
        tmp_db.upsert_group_config(
            GROUP,
            checkin_deadlines=SINGLE_SLOT,
            reminder_rules={"checkin": {"enabled": True, "times": ["08:00"]},
                            "summary": {"enabled": True, "times": ["00:00"]},
                            "report": {"enabled": True, "times": ["00:00"]}})

    def test_all_biz_still_sends_summary_and_report(self, tmp_db):
        """10:10 打卡已全过 -> 打卡跳过，但总结/报数照发，且不标 skipped。"""
        self._setup(tmp_db)
        pushed = []

        def push(group_id, text):
            pushed.append(text)
            return True, ""

        summary = reminder_handler.run_group(
            tmp_db, ReminderBackend(), push, tmp_db.get_group_config(GROUP),
            T_1010, biz=None, manual=True)
        assert summary.get("skipped") is None      # ⛔ 旧实现这里是 checkin_all_done
        assert "checkin" not in _planned_biz(tmp_db)
        assert {"summary", "report"} <= _planned_biz(tmp_db)
        assert summary["sent"] > 0 and pushed

    def test_single_checkin_still_reports_all_done(self, tmp_db):
        """显式单业务催打卡、时段已过 -> 仍要明确告知「已过」。"""
        self._setup(tmp_db)
        summary = reminder_handler.run_group(
            tmp_db, ReminderBackend(), _push_ok,
            tmp_db.get_group_config(GROUP), T_1010,
            biz="checkin", manual=True)
        assert summary["skipped"] == "checkin_all_done"
        assert summary["sent"] == 0


class _BadGroupBackend(ReminderBackend):
    """只对指定群抛 BackendError，模拟单群链路故障。"""

    def __init__(self, bad_group: str):
        super().__init__()
        self.bad_group = bad_group

    def summary_query_status(self, *, user_id, group_id, period_key):
        if group_id == self.bad_group:
            raise BackendError("mcp timeout for %s" % group_id)
        return super().summary_query_status(user_id=user_id,
                                           group_id=group_id,
                                           period_key=period_key)


TICK_NOW = datetime(2026, 10, 13, 20, 0, tzinfo=TZ)   # summary 19:00 已过


class TestReminderTickIsolation:
    """P1-1：一个群炸了 ⛔ 不能让所有群断更。"""

    BAD = "G200"

    def _two_groups(self, tmp_db):
        setup_group_with_required(tmp_db)
        enable_all(tmp_db, group_id=self.BAD)
        tmp_db.upsert_member_task(self.BAD, PHONE, "乙", "开发")
        rules = {"checkin": {"enabled": False, "times": []},
                 "summary": {"enabled": True, "times": ["19:00"]},
                 "report": {"enabled": False, "times": []}}
        for gid in (GROUP, self.BAD):
            tmp_db.upsert_group_config(gid, reminder_rules=rules)

    def test_one_bad_group_does_not_stop_others(self, tmp_db):
        self._two_groups(tmp_db)
        summary = reminder_handler.tick(
            tmp_db, _BadGroupBackend(self.BAD), _push_ok, TICK_NOW)
        assert summary["groups"] == 2
        assert len(summary["errors"]) == 1
        assert summary["errors"][0].startswith(self.BAD + ":")
        assert summary["sent"] > 0        # 好群照发

    def test_all_groups_backend_down_raises(self, tmp_db):
        """全群后端不可用 -> 上抛（保持「基础设施宕机」既有语义）。"""
        self._two_groups(tmp_db)

        class _AllBad(_BadGroupBackend):
            def summary_query_status(self, *, user_id, group_id, period_key):
                raise BackendError("mcp down")

        with pytest.raises(BackendError):
            reminder_handler.tick(tmp_db, _AllBad(GROUP), _push_ok, TICK_NOW)


# ---------------------------------------------------------------- 修复项 4 ----

class TestBoardCountsEnabledOnly:
    """P0-4：分子只数在岗考核成员（⛔ 否则会「3/2 已完成」）。"""

    def _setup(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "甲", "开发", True)
        tmp_db.upsert_member_task(GROUP, OTHER_PHONE, "乙", "测试", False)
        backend = FakeBackend()
        cells = []
        for phone in (PHONE, OTHER_PHONE):
            for session in ("am", "pm"):
                cells.append({"phone": phone, "name": phone[-2:],
                              "session": session,
                              "checkin_time": "2026-10-20T09:00:00",
                              "status": "NORMAL", "entry_source": "user"})
        backend.results["checkin_query_records"] = {"records": cells}
        backend.results["summary_query_records"] = {"records": [
            {"phone": p, "name": p[-2:], "content": "x",
             "entry_source": "user", "updated_time": "2026-10-20T20:00:00"}
            for p in (PHONE, OTHER_PHONE)]}
        backend.results["report_query_records"] = {"records": [
            {"phone": p, "name": p[-2:], "role": "开发",
             "entry_source": "user", "created_time": "2026-10-20T20:00:00",
             "items": [{"field_id": 1, "field_name": "数", "quantity": 1}]}
            for p in (PHONE, OTHER_PHONE)]}
        return backend

    def _board(self, tmp_db, backend):
        return board_mod.build_board(
            tmp_db, backend, GROUP, "2026-10-20",
            now=datetime(2026, 10, 20, 21, 0, tzinfo=TZ))

    def test_paused_member_not_counted_in_numerator(self, tmp_db):
        board = self._board(tmp_db, self._setup(tmp_db))
        counts = board["counts"]
        assert counts["total"] == 1               # 分母只算在岗
        assert counts["checkin_done"] == 1
        assert counts["summary_done"] == 1
        assert counts["report_done"] == 1
        assert counts["checkin_done"] <= counts["total"]

    def test_disabled_member_row_still_present(self, tmp_db):
        """名单行仍保留（历史记录要能对上号），只是不计完成。"""
        board = self._board(tmp_db, self._setup(tmp_db))
        paused = [m for m in board["members"]
                  if m["user_id"] == OTHER_PHONE][0]
        assert paused["enabled"] is False
        assert paused["checkin_status"] == "completed"   # 行内状态照实渲染

    def test_all_disabled_keeps_legacy_fallback(self, tmp_db):
        """全员暂停（enabled_total == 0）时保留旧口径，⛔ 不显示 0/0。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "甲", "开发", False)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [
            {"phone": PHONE, "session": "am", "checkin_time": "09:00",
             "status": "NORMAL", "entry_source": "user"}]}
        board = self._board(tmp_db, backend)
        # 只打完 am、缺 pm -> 整体未完成；这里只锁「分子不被 enabled 守卫吃成 0」
        assert board["counts"]["total"] == 1


# ---------------------------------------------------------------- 修复项 5 ----

class TestSlotLabelExport:
    """P1-2：自定义时段在导出里要显示真名，⛔ 不再一律「下午」。"""

    SLOTS = {"slots": [
        {"id": "am", "name": "上午", "start": "08:00", "end": "12:00"},
        {"id": "slot_3", "name": "晚班", "start": "18:00", "end": "22:00"},
    ]}

    def _records(self):
        return {"records": [
            {"phone": PHONE, "name": "甲", "session": "slot_3",
             "checkin_time": "2026-10-20T19:05:00", "status": "NORMAL",
             "entry_source": "user", "image_name": "a.jpg"},
            {"phone": PHONE, "name": "甲", "session": "am",
             "checkin_time": "2026-10-20T09:05:00", "status": "NORMAL",
             "entry_source": "user", "image_name": "b.jpg"},
        ]}

    def test_csv_uses_configured_slot_name(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines=self.SLOTS)
        tmp_db.upsert_whitelist(GROUP, PHONE)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = self._records()
        ctx = make_ctx(tmp_db, backend, make_config(
            Path(str(tmp_db.db_path)).parent),
            now=datetime(2026, 10, 20, 10, 0, tzinfo=TZ))
        stats_handler.handle_command(commands.parse("导出"), ctx)
        files = sorted((Path(str(tmp_db.db_path)).parent / "exports")
                       .glob("*.csv"))
        assert files
        text = files[-1].read_text(encoding="utf-8-sig")
        assert "晚班" in text
        assert "上午" in text
        # slot_3 那行 ⛔ 不能再被写成「下午」
        for line in text.splitlines():
            if "slot_3" in line or "19:05" in line:
                assert "下午" not in line

    def test_xlsx_fallback_sheet_uses_configured_name(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines=self.SLOTS)
        data = stats_handler.build_export_workbook(
            "checkin", "daily", "2026-10-20", self._records()["records"],
            {"am": "上午", "slot_3": "晚班"})
        from openpyxl import load_workbook
        import io
        ws = load_workbook(io.BytesIO(data)).active
        labels = [row[2] for row in ws.iter_rows(min_row=2, values_only=True)]
        assert labels == ["晚班", "上午"]

    def test_admin_export_uses_configured_name(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines=self.SLOTS)
        labels = stats_export.slot_labels_for(tmp_db, GROUP)
        assert labels["slot_3"] == "晚班"
        headers, rows = stats_export.records_to_rows(
            "checkin", self._records()["records"], "", labels)
        assert rows[0][headers.index("时段")] == "晚班"

    def test_unknown_session_falls_back_to_raw_id(self):
        """既不在配置也不在默认表里 -> 原样输出标识（⛔ 不猜成「下午」）。"""
        headers, rows = stats_export.records_to_rows(
            "checkin", self._records()["records"], "", {"am": "上午"})
        assert rows[0][headers.index("时段")] == "slot_3"


# ---------------------------------------------------------------- 修复项 6 ----

class TestUpsertUserPlatformCleanup:
    """P0-4 防御：换号 / 跨群重绑前先解绑同一 platform_user_id 的残留。"""

    def test_cleanup_update_runs_before_insert(self):
        cur = FakeCursor(fetchone_values=[(7,)])
        dao = Dao(FakePool(cursor=cur))
        uid = dao.upsert_user(cur, PHONE, "甲", "puid-9", None, "learned")
        assert uid == 7
        sql, params = cur.calls[0]
        assert sql.startswith("UPDATE sys_user SET platform_user_id = NULL")
        assert params == ("puid-9", PHONE)
        assert "INSERT INTO sys_user" in cur.calls[1][0]

    def test_no_cleanup_without_platform_id(self):
        cur = FakeCursor(fetchone_values=[(7,)])
        dao = Dao(FakePool(cursor=cur))
        dao.upsert_user(cur, PHONE, "甲", None, None, "learned")
        assert "INSERT INTO sys_user" in cur.calls[0][0]

    def test_cleanup_does_not_touch_same_phone(self):
        """解绑条件带 phone <> %s：⛔ 不会把自己这行清掉。"""
        cur = FakeCursor(fetchone_values=[(7,)])
        Dao(FakePool(cursor=cur)).upsert_user(cur, PHONE, None, "puid-9",
                                             None, "learned")
        sql, params = cur.calls[0]
        assert "phone <> %s" in sql
        assert params[1] == PHONE
