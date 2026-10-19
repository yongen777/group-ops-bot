# -*- coding: utf-8 -*-
"""本地冒烟：C 类恢复全流程（§4.8 第 7-8 条）。

流程（全部走真实代码路径，替身仅替代 MCP 端口）：
1. 打开既有冒烟库 -> 触发 schema v4 迁移 -> 校验新列与旧数据
2. run_submission 遇 BackendError（模拟 MCP 不可达）-> 归 C
   （intent=unknown + recovery_task pending + ⛔ 不自动重试）
3. recovery tick 以「后端已写成功」查证 -> 补账 A
   （intent=confirmed + receipt + 额度成立 + 任务 resolved）

⛔ 只写本地冒烟库；替身结果不构成真实 MySQL 链路证据（§2.8 / §9.3）。
用法：python recovery_flow.py <db_path>
"""

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))       # group-ops-bot-tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "group-ops-bot"))

from app import recovery as recovery_handler          # noqa: E402
from app.backend.base import BackendError             # noqa: E402
from app.handlers import report as report_handler     # noqa: E402
from app.handlers.base import KIND_C, run_submission  # noqa: E402
from app.state import StateStore                       # noqa: E402
from doubles.fakes import FakeBackend                  # noqa: E402
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx  # noqa: E402

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
PERIOD = "2026-10-13"


def check_migration(db_path: str) -> None:
    """schema v4：新列/新表就位、旧数据（种子配置）不受影响。

    v2 -> v3 -> v4 逐级迁移在 StateStore 构造时完成；本函数校验终态。
    """
    raw = sqlite3.connect(db_path)
    raw.row_factory = sqlite3.Row
    version = raw.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()["value"]
    intent_cols = [r["name"] for r in raw.execute("PRAGMA table_info(submit_intent)")]
    task_cols = [r["name"] for r in raw.execute("PRAGMA table_info(recovery_task)")]
    tables = {r["name"] for r in raw.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    wl_cols = [r["name"] for r in raw.execute("PRAGMA table_info(export_whitelist)")]
    g_cols = [r["name"] for r in raw.execute("PRAGMA table_info(group_config)")]
    seeded = raw.execute(
        "SELECT COUNT(*) AS n FROM group_config WHERE group_id=?",
        (GROUP,)).fetchone()["n"]
    raw.close()
    # 迁移链叠加：v3 -> v4（成员表/白名单分权）-> v5/v6/v7 -> v8
    # （2026-10-19 白名单单权化）。StateStore.__init__ 一次跑到最新版。
    assert version == "8", "schema_version 应为 8，实际 %s" % version
    assert "token_id" in intent_cols, "submit_intent 缺 token_id"
    for col in ("attempts", "escalated", "last_error"):
        assert col in task_cols, "recovery_task 缺 %s" % col
    assert "group_member_task_config" in tables, "缺应交人员表（v4）"
    assert "checkin_required" not in tables, "旧表未改名（v4）"
    assert "permission_type" in wl_cols, "export_whitelist 缺 permission_type"
    for col in ("group_name", "status", "checkin_deadlines",
                "checkin_require_image"):
        assert col in g_cols, "group_config 缺 %s（v4）" % col
    assert seeded == 1, "迁移弄丢了种子群配置"
    print("[1] 迁移 OK：schema v8（v3->v4->v8 全链），新列/新表就位，种子群配置保留")


def simulate_c_then_recover(store: StateStore) -> None:
    """真实 run_submission 归 C -> recovery tick 补账 A。

    ⚠️ 持久冒烟库：每轮用独立手机号（独立范围键），与往轮残留隔离。
    """
    config = make_config(Path(store.db_path).parent)
    run_phone = "139%08d" % (int(datetime.now().timestamp()) % 100000000)
    scope = "%s|%s|report|%s" % (GROUP, run_phone, PERIOD)

    # --- 第一段：MCP 不可达 -> C ---
    down = FakeBackend()
    down.raise_on.add("report_submit")
    ctx = make_ctx(store, down, config, now=NOW, phone=run_phone)
    result = run_submission(
        ctx, biz="report",
        call=lambda rid: down.report_submit(
            user_id=1, group_id=GROUP, period_key=PERIOD, role="开发",
            items=[{"field_id": 1, "field_name": "bug修复数", "quantity": 3}],
            request_id=rid),
        period_key=PERIOD)
    assert result.kind == KIND_C, "MCP 不可达应归 C，实际 %s" % result.kind
    request_id = result.request_id
    assert store.get_intent(request_id)["status"] == "unknown"
    assert store.has_successful_scope(scope) is False, "C ⛔ 不占额度"
    task = store._conn.execute(
        "SELECT * FROM recovery_task WHERE request_id=?",
        (request_id,)).fetchone()
    assert task is not None and task["status"] == "pending"
    assert [c[0] for c in down.calls].count("report_submit") == 1, \
        "C ⛔ 不自动重试（只应调用一次）"
    print("[2] C 路径 OK：intent=unknown、任务 pending、额度未占、⛔ 未重试")

    # --- 第二段：后端其实已写成功 -> 查证补账 A ---
    up = FakeBackend()
    up.results["op_query_by_request"] = {
        "status": "success", "record_id": 555, "duplicate": True}
    # 恢复延迟 2 分钟（RECOVERY_DELAY_MINUTES）：3 分钟后调度器来查证
    # ⚠️ 持久冒烟库：往轮残留任务也会被查证，断言按本次 request_id 定向
    summary = recovery_handler.tick(
        store, up, NOW + timedelta(minutes=3))
    assert summary["A"] >= 1, "恢复应补账 A：%s" % summary
    assert store.get_intent(request_id)["status"] == "confirmed"
    receipt = store._conn.execute(
        "SELECT * FROM receipt WHERE request_id=?",
        (request_id,)).fetchone()
    assert receipt is not None and receipt["record_id"] == "555"
    assert store.has_successful_scope(scope) is True, "A 补账后额度应成立"
    task = store._conn.execute(
        "SELECT * FROM recovery_task WHERE request_id=?",
        (request_id,)).fetchone()
    assert task["status"] == "resolved" and task["outcome"] == "A"
    assert [c[0] for c in up.calls].count("op_query_by_request") >= 1, \
        "恢复只应按原键查证"
    print("[3] 恢复 A OK：回执落库、额度成立、任务 resolved(A)、原键查证")

    # --- 第三段：恢复后同范围再提交 -> B（已完成） ---
    again = FakeBackend()
    ctx2 = make_ctx(store, again, config, now=NOW, phone=run_phone)
    result2 = run_submission(
        ctx2, biz="report",
        call=lambda rid: again.report_submit(
            user_id=1, group_id=GROUP, period_key=PERIOD, role="开发",
            items=[], request_id=rid),
        period_key=PERIOD)
    assert result2.kind == "B", "额度已占应归 B，实际 %s" % result2.kind
    print("[4] 补账后重复提交 OK：归 B（本周期已完成）")


def main(db_path: str) -> None:
    store = StateStore(db_path)   # 先建库（触发 v4 迁移）再校验
    try:
        check_migration(db_path)
        simulate_c_then_recover(store)
    finally:
        store.close()
    print("RESULT: PASS —— C 类恢复全流程（迁移 -> C -> 查证补账 A -> 重复提交归 B）")


if __name__ == "__main__":
    main(sys.argv[1])
