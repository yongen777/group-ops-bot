# -*- coding: utf-8 -*-
"""D31 生产收官六项加固回归（2026-10-21）。

覆盖（与主文档 §8.33 一一对应）：
1. 群内「导出」进程内互斥锁 —— 多群同分钟并发不串表
2. `MysqlPool._acquire()` 重连失败必须归还 `_created` / `_borrowed`
3. 6~8 个打卡时段时「保存提醒规则」不再 400（前端截断对齐后端上限 5）
4. 统计中心 / 全业务导出对齐「暂停考核」与「未完成 vs 待补录」
5. `rename_member_task` 事务化 + 授权冲突清理 + `realname` 同步 `learned_user`
6. 手动催办一条都没送达时，回执必须明说「未送达」

⛔ 本文件只测这六条；既有 806 项行为（导出文件名、提醒口径、权限边界）
由各自测试文件锁住，这里⛔ 不重复断言。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

import pymysql
import pymysql.err
import pytest

from app import commands
from app.admin import board as board_mod
from app.handlers import realname as realname_handler
from app.handlers import stats as stats_handler
from doubles.fakes import FakeBackend, RecordingSender
from helpers import (BOT_ROOT, GROUP, PHONE, TZ, enable_all, make_config,
                     make_ctx)
from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap, login,
                        mutate)

OTHER = "13800138010"
REAL_PHONE = "13800000001"
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)   # 落在 am 时段（00:00-12:00）


# =====================================================================
# 修复 1：导出互斥锁（多群同分钟并发不串表）
# =====================================================================

def _export_ctx(tmp_db, group_id, phone, data_dir):
    """建群 + 授权导出 + 名单，返回可执行「导出」的 ctx（文件发送替身可用）。"""
    enable_all(tmp_db, group_id)
    tmp_db.upsert_whitelist(group_id, phone)
    tmp_db.upsert_member_task(group_id, phone, "成员")
    sender = RecordingSender()
    sender.file_ok = True          # 模拟「file 通道已接通」（D12 实测后）
    return make_ctx(tmp_db, FakeBackend(), make_config(data_dir),
                    now=NOW, group_id=group_id, phone=phone, sender=sender)


class TestExportLock:
    def test_module_lock_exists(self):
        """锁必须是模块级真锁（⛔ 不是每请求新建的假锁）。"""
        assert isinstance(stats_handler._EXPORT_LOCK,
                          type(threading.Lock()))

    def test_two_groups_do_not_build_files_concurrently(self, tmp_db,
                                                        monkeypatch):
        """A/B 两群并发导出：生成阶段 ⛔ 不许交叠（交叠 = 可能串表）。"""
        data_dir = Path(str(tmp_db.db_path)).parent
        ctx_a = _export_ctx(tmp_db, "G-A", PHONE, data_dir)
        ctx_b = _export_ctx(tmp_db, "G-B", OTHER, data_dir)

        real = stats_handler.build_export_workbook
        guard = threading.Lock()
        state = {"active": 0, "peak": 0, "calls": 0}

        def slow_build(*args, **kwargs):
            with guard:
                state["active"] += 1
                state["calls"] += 1
                state["peak"] = max(state["peak"], state["active"])
            try:
                time.sleep(0.05)          # 放大交叠窗口
                return real(*args, **kwargs)
            finally:
                with guard:
                    state["active"] -= 1

        monkeypatch.setattr(stats_handler, "build_export_workbook", slow_build)

        results = {}

        def run(name, ctx):
            results[name] = stats_handler.handle_command(
                commands.parse("导出"), ctx)

        threads = [threading.Thread(target=run, args=("a", ctx_a)),
                   threading.Thread(target=run, args=("b", ctx_b))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 3 个业务 × 2 个群 = 6 次构建，且任意时刻只有 1 个在跑
        assert state["calls"] == 6
        assert state["peak"] == 1
        # 两群都拿到了「发送成功」回执（锁没把功能锁坏）
        assert results["a"] == stats_handler.EXPORT_SENT_TEXT
        assert results["b"] == stats_handler.EXPORT_SENT_TEXT

    def test_file_names_stay_group_agnostic(self, tmp_db):
        """⛔ 文件名口径不动（`{biz}_{YYYYMMDD_HHMM}.xlsx`，不含群号）。"""
        data_dir = Path(str(tmp_db.db_path)).parent
        ctx = _export_ctx(tmp_db, GROUP, PHONE, data_dir)
        stats_handler.handle_command(commands.parse("导出"), ctx)
        names = sorted(p.name for p in (data_dir / "exports").glob("*"))
        assert names == ["checkin_20261013_1000.xlsx",
                         "report_20261013_1000.xlsx",
                         "summary_20261013_1000.xlsx"]


# =====================================================================
# 修复 2：连接池重连失败归还计数
# =====================================================================

class _DeadConn:
    """ping 必失败的假连接（模拟 MySQL 掉线）。"""

    def __init__(self):
        self.closed = False

    def ping(self, reconnect=True):  # noqa: ARG002 - 与 pymysql 同签名
        raise pymysql.err.OperationalError(2006, "MySQL server has gone away")

    def close(self):
        self.closed = True

    def rollback(self):
        pass


class _OkConn:
    def __init__(self):
        self.closed = False

    def ping(self, reconnect=True):  # noqa: ARG002
        return None

    def close(self):
        self.closed = True

    def rollback(self):
        pass


class TestPoolCounterReturn:
    def _pool(self, size=1):
        from mcp_server.db.pool import MysqlPool
        return MysqlPool(config=object(), size=size)

    def test_reconnect_failure_returns_both_counters(self):
        """ping 失败 + 重连也抛错 -> `_created`/`_borrowed` 必须都回零。"""
        from mcp_server.db.pool import PoolError

        pool = self._pool()
        dead = _DeadConn()
        pool._created = 1
        pool._idle.put(dead)

        def boom():
            raise PoolError("mysql connect failed: refused")

        pool._connect = boom
        with pytest.raises(PoolError):
            pool._acquire()

        assert dead.closed is True          # 坏连接先关掉，⛔ 不回队列
        assert pool._created == 0
        assert pool._borrowed == 0

    def test_pool_recovers_after_failure(self):
        """归还计数后，下一轮建连成功即恢复（⛔ 不会 5 次后死锁）。"""
        from mcp_server.db.pool import MysqlPool, PoolError

        pool = MysqlPool(config=object(), size=1)
        pool._created = 1
        pool._idle.put(_DeadConn())

        def boom():
            raise PoolError("refused")

        pool._connect = boom
        for _ in range(6):                 # 旧代码第 5 次后永久卡死
            with pytest.raises(PoolError):
                pool._acquire()

        pool._connect = lambda: _OkConn()
        conn = pool._acquire()             # 还能借到新连接
        assert isinstance(conn, _OkConn)
        assert pool._created == 1 and pool._borrowed == 1
        pool._release(conn)
        assert pool._borrowed == 0


# =====================================================================
# 修复 3：提醒规则 times 截断对齐后端上限
# =====================================================================

class TestReminderTimesLimit:
    def test_frontend_slices_to_five(self):
        """6~8 个时段 -> ⛔ 不能再截成 6 个（后端上限是 5，必 400）。"""
        src = (BOT_ROOT / "admin-ui/src/components/system/"
               "GroupManagement.tsx").read_text(encoding="utf-8")
        block = src.split("const saveReminder", 1)[1].split("const patch", 1)[0]
        assert ".slice(0, 5);" in block
        # ⚠️ 注释里会提到「原来写 .slice(0, 6)」，所以只锚代码语句（带分号）
        assert ".slice(0, 6);" not in block

    def test_backend_accepts_five_and_rejects_six(self, tmp_db):
        """同一口径的后端证据：5 个 times 可存，6 个必须 400。"""
        enable_all(tmp_db)
        client, _sender = _ready_admin(tmp_db)
        five = ["08:25", "09:25", "10:25", "11:25", "12:25"]
        resp = mutate(client, "PUT", "/api/admin/groups/%s/config" % GROUP,
                      {"reminder_rules": {"checkin": {"enabled": True,
                                                      "times": five}}})
        assert resp.status_code == 200
        resp = mutate(client, "PUT", "/api/admin/groups/%s/config" % GROUP,
                      {"reminder_rules": {
                          "checkin": {"enabled": True,
                                      "times": five + ["13:25"]}}})
        assert resp.status_code == 400


# =====================================================================
# 修复 4：暂停考核 / 未完成 vs 待补录 口径
# =====================================================================

def _member(enabled=True, checkin_time="", summary=None, report=None,
            checkin_status="pending", summary_status="pending",
            report_status="pending"):
    return {
        "user_id": PHONE, "nickname": "成员1", "role": "", "enabled": enabled,
        "in_roster": True,
        "checkin": {"am": {"time": checkin_time, "status": "normal",
                           "raw_status": "", "method": "",
                           "window": "08:00-08:30", "reminded": False}},
        "checkin_status": checkin_status,
        "summary": summary,
        "summary_status": summary_status,
        "report": report,
        "report_status": report_status,
    }


SWITCHES = {"checkin": True, "summary": True, "report": True}
SLOTS = [{"id": "am", "name": "上午", "start": "08:00", "end": "08:30"}]


class TestPausedMemberVerdict:
    def test_paused_member_with_all_done_still_achieved(self):
        """暂停考核的人照常提交了 -> 仍判「今日达标」（记录真实完成情况）。"""
        member = _member(enabled=False, checkin_time="08:10",
                         summary={"content": "ok"}, report={"items": []})
        assert board_mod._achieve(member, SWITCHES, SLOTS) == "今日达标"

    def test_paused_member_with_gaps_is_not_counted_as_absent(self):
        """⭐ 核心：暂停考核 + 缺项 -> 「已暂停考核」，⛔ 不写「上午未打卡…」。"""
        verdict = board_mod._achieve(_member(enabled=False), SWITCHES, SLOTS)
        assert verdict == "已暂停考核"
        assert "未打卡" not in verdict and "未提交" not in verdict

    def test_active_member_still_lists_missing_items(self):
        """在岗成员口径不变（⛔ 不能为了暂停考核把正常人的缺项也吞掉）。"""
        verdict = board_mod._achieve(_member(), SWITCHES, SLOTS)
        assert "上午未打卡" in verdict and "总结未提交" in verdict

    def test_export_row_uses_paused_label(self):
        """全业务导出（board_rows）同口径 —— 表里⛔ 不能把人算成缺勤。"""
        board = {
            "group": {"group_name": "测试群", "switches": SWITCHES},
            "slots": SLOTS,
            "members": [_member(enabled=False)],
        }
        _headers, rows = board_mod.board_rows(board)
        assert rows[0][-1] == "已暂停考核"

    def test_statistics_page_uses_active_members(self):
        """统计中心：分母/分子/两个筛选都排除暂停考核的成员。"""
        src = (BOT_ROOT / "admin-ui/src/components/system/"
               "Statistics.tsx").read_text(encoding="utf-8")
        assert "const activeMembers = useMemo" in src
        assert "const totalCount = activeMembers.length" in src
        assert "activeMembers.filter((m) => isAchieved(m)).length" in src
        assert "m.enabled !== false && isAchieved(m)" in src
        assert "m.enabled !== false && !isAchieved(m)" in src
        assert "已暂停考核" in src


# =====================================================================
# 修复 5：改号事务化 + 实名同步 learned_user
# =====================================================================

class TestRenameMemberTransaction:
    def test_roster_and_whitelist_follow_new_phone(self, tmp_db):
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "成员1")
        tmp_db.upsert_whitelist(GROUP, PHONE)
        tmp_db.rename_member_task(GROUP, PHONE, OTHER)

        assert tmp_db.get_member_task(GROUP, PHONE) is None
        assert tmp_db.get_member_task(GROUP, OTHER)["nickname"] == "成员1"
        grants = list(tmp_db.list_whitelist(GROUP))
        assert [(r["user_id"], r["permission_type"]) for r in grants] == \
            [(OTHER, "ADMIN_ALL")]

    def test_conflicting_grant_is_merged_not_crashing(self, tmp_db):
        """新号已有同权限行 -> UPDATE OR IGNORE + 清旧行，⛔ 不抛 IntegrityError。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "成员1")
        tmp_db.upsert_whitelist(GROUP, PHONE)
        tmp_db.upsert_whitelist(GROUP, OTHER)          # 新号本来就有授权

        tmp_db.rename_member_task(GROUP, PHONE, OTHER)  # 旧代码在这里 500

        rows = list(tmp_db.list_whitelist(GROUP))
        assert [(r["user_id"], r["permission_type"]) for r in rows] == \
            [(OTHER, "ADMIN_ALL")]
        assert tmp_db.has_whitelist_permission(GROUP, OTHER) is True
        assert tmp_db.has_whitelist_permission(GROUP, PHONE) is False

    def test_old_phone_pending_session_cleared(self, tmp_db):
        """旧号的「等群名 / 等实名」会话必须清掉，⛔ 否则旧号下条文本被吞。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "成员1")
        tmp_db.put_pending_session(GROUP, PHONE, realname_handler.BIZ, None,
                                   NOW, ttl_minutes=10)
        assert tmp_db.get_pending_session(GROUP, PHONE,
                                          realname_handler.BIZ, NOW) is not None

        tmp_db.rename_member_task(GROUP, PHONE, OTHER)

        assert tmp_db.peek_pending_session(GROUP, PHONE,
                                           realname_handler.BIZ) is None


class TestRealnameSyncsLearnedUser:
    def test_name_only_updates_learned_name(self, tmp_db):
        """⭐ D45：只发姓名 -> 号码取平台身份，本地展示名同步（提醒里⛔ 不用旧昵称）。

        原用例名 `test_same_phone_updates_learned_name`，发的是「张三 <本人号>」。
        D45 之后号码由平台决定，且 `_phone_conflict` 不再按「号 == 自己」短路
        （否则撞号检测永远不触发），所以这里必须让目录里**还没有他**，
        才能走到「首次登记」这条正路。
        """
        enable_all(tmp_db)
        backend = FakeBackend()
        backend.results["user_get"] = {"user": None}   # 目录里还没有这个人
        ctx = make_ctx(tmp_db, backend,
                       make_config(Path(str(tmp_db.db_path)).parent),
                       now=NOW, phone=REAL_PHONE)
        tmp_db.put_pending_session(GROUP, REAL_PHONE, realname_handler.BIZ,
                                   None, NOW, ttl_minutes=10)
        ctx.text = "张三"
        reply = realname_handler.handle_pending_text(ctx)
        assert "已成功录入" in reply
        assert tmp_db.user_display_name(REAL_PHONE) == "张三"

    def test_mismatched_phone_is_rejected(self, tmp_db):
        """⭐ D45（2026-10-26 用户指令）：手输号码 ≠ 平台身份 -> ⛔ 直接拒。

        旧口径（D31）是「照样录入、只记安全事件、不改展示名」—— 那等于允许
        把别人的号配到自己名下。现在改为**先校验号码**：不一致就拒绝，
        ⛔ 不写 sys_user、⛔ 不改展示名，会话保留让用户重发（只需发姓名）。
        """
        enable_all(tmp_db)
        backend = FakeBackend()
        backend.results["user_get"] = {"user": None}
        ctx = make_ctx(tmp_db, backend,
                       make_config(Path(str(tmp_db.db_path)).parent),
                       now=NOW, phone=REAL_PHONE)
        tmp_db.put_pending_session(GROUP, REAL_PHONE, realname_handler.BIZ,
                                   None, NOW, ttl_minutes=10)
        ctx.text = "张三 13800000002"
        reply = realname_handler.handle_pending_text(ctx)
        assert reply == realname_handler.PHONE_MISMATCH_TEXT
        assert not [c for c in backend.calls if c[0] == "user_upsert"]
        assert tmp_db.user_display_name(REAL_PHONE) is None
        # 会话保留：用户可以只发姓名重来
        assert tmp_db.get_pending_session(
            GROUP, REAL_PHONE, realname_handler.BIZ, NOW) is not None


# =====================================================================
# 修复 6：手动催办「一条都没送达」必须在回执里说清
# =====================================================================

def _ready_admin(tmp_db, push_ok=False):
    """建号 -> 登录 -> 过强制改密门（时钟注入到 NOW）。

    push_ok：是否模拟「推送通道已接通」（⛔ 默认未接通，与生产 fail-closed 同口径）。
    返回 (client, sender) —— 需要看 sender 的用例直接用。
    """
    from fastapi.testclient import TestClient

    from app.admin.server import create_admin_app

    bootstrap(tmp_db, None)
    sender = RecordingSender()
    sender.push_ok = push_ok
    app = create_admin_app(
        state=tmp_db, backend=FakeBackend(), sender=sender,
        config=make_config(Path(str(tmp_db.db_path)).parent),
        now_fn=lambda: NOW)
    client = TestClient(app)
    assert login(client).status_code == 200
    resp = mutate(client, "POST", "/api/admin/change-password",
                  {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert resp.status_code == 200
    assert login(client, password=NEW_PASS).status_code == 200
    return client, sender


class TestManualReminderUnsentWarning:
    def test_all_unsent_warns_about_webhook(self, tmp_db):
        """实例未绑定（无推送地址）-> sent=0/unsent>0 -> 回执必须提「未送达」。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "郑佳")
        tmp_db.ensure_robot_instance(GROUP)      # WAIT_BIND，无 webhook
        client, _sender = _ready_admin(tmp_db)

        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP,
                      {"biz": "checkin"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["summary"]["sent"] == 0
        assert body["summary"]["unsent"] >= 1
        assert "未送达" in body["message"]
        assert "推送地址" in body["message"]

    def test_sent_case_has_no_false_alarm(self, tmp_db):
        """送达正常时 ⛔ 不出现「未送达」字样（⛔ 不制造假告警）。"""
        enable_all(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, "郑佳")
        tmp_db.ensure_robot_instance(GROUP)
        tmp_db.bind_robot_instance(GROUP, "https://imtwo.zdxlz.com/x?key=k")
        client, _sender = _ready_admin(tmp_db, push_ok=True)

        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/reminders/run" % GROUP,
                      {"biz": "checkin"})
        body = resp.json()
        assert body["summary"]["sent"] >= 1
        assert "未送达" not in body["message"]
