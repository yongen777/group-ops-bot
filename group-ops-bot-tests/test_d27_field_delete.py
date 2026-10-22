# -*- coding: utf-8 -*-
"""D27（2026-10-20 用户指令）：报数字段池「删除」= 物理删除，⛔ 不再降级成停用。

用户原话：「群管理的报数字段池，列表的删除操作修改成直接删除该字段而不是
停用功能」。

为什么不能简单 DELETE 完事 —— 两个必须同时守住的口径：

1. 历史报数 ⛔ 不能丢：MySQL ``report_item`` 存 ``field_name`` / ``unit``
   快照，删字段池行不影响历史记录与导出（导出按快照出列）。
2. 字段 id ⛔ 不能复用：``stats`` 导出按 ``field_id`` 归并历史列，删掉最大号
   后若新字段又拿到同一个 id，已删字段的历史数量会被并进新字段的列里。
   故引入只增不减的 ``report_field_seq``（schema v10）。
"""

from __future__ import annotations

from app.handlers import report as report_handler

GROUP = "G100"


def _seed(store) -> None:
    store.create_group(GROUP, "测试群")


def _add(store, name, unit="个"):
    fid = store.next_field_id(GROUP)
    store.upsert_field(GROUP, fid, name, unit, [], True, 0)
    return fid


class TestHardDelete:
    def test_delete_removes_row(self, tmp_db):
        _seed(tmp_db)
        fid = _add(tmp_db, "客户拜访量")
        assert [r["field_id"] for r in
                tmp_db.list_fields(GROUP, enabled_only=False)] == [fid]
        assert tmp_db.delete_field(GROUP, fid) == 1
        assert list(tmp_db.list_fields(GROUP, enabled_only=False)) == []

    def test_delete_missing_returns_zero(self, tmp_db):
        """不存在 -> 0 行（API 层据此 404，⛔ 不伪装成功）。"""
        _seed(tmp_db)
        assert tmp_db.delete_field(GROUP, 999) == 0

    def test_delete_only_touches_one_group(self, tmp_db):
        _seed(tmp_db)
        tmp_db.create_group("G200", "另一个群")
        a = _add(tmp_db, "字段A")
        tmp_db.upsert_field("G200", 1, "字段A", "个", [], True, 0)
        tmp_db.delete_field(GROUP, a)
        assert len(tmp_db.list_fields("G200", enabled_only=False)) == 1

    def test_other_fields_survive(self, tmp_db):
        _seed(tmp_db)
        a = _add(tmp_db, "字段A")
        b = _add(tmp_db, "字段B")
        tmp_db.delete_field(GROUP, a)
        rows = tmp_db.list_fields(GROUP, enabled_only=False)
        assert [r["field_id"] for r in rows] == [b]

    def test_deleted_field_not_fillable(self, tmp_db, tmp_path):
        """群内报数只列 enabled 字段 —— 删掉后立刻不可填报。"""
        _seed(tmp_db)
        a = _add(tmp_db, "字段A")
        b = _add(tmp_db, "字段B")
        tmp_db.delete_field(GROUP, a)
        assert [f["field_id"] for f in
                report_handler.field_pool(_ctx(tmp_db))] == [b]


class TestFieldIdNotReused:
    """⭐ 核心：删掉最大号后新字段必须拿**新号**（⛔ 不回收）。"""

    def test_id_monotonic_after_delete(self, tmp_db):
        _seed(tmp_db)
        a = _add(tmp_db, "字段A")
        b = _add(tmp_db, "字段B")
        tmp_db.delete_field(GROUP, b)          # 删掉最大号 2
        c = _add(tmp_db, "字段C")
        assert (a, b) == (1, 2)
        assert c == 3                          # ⛔ 不是 2

    def test_id_keeps_growing_after_delete_all(self, tmp_db):
        _seed(tmp_db)
        a = _add(tmp_db, "字段A")
        tmp_db.delete_field(GROUP, a)
        assert tmp_db.next_field_id(GROUP) == 2
        assert tmp_db.next_field_id(GROUP) == 3

    def test_seq_survives_group_cascade(self, tmp_db):
        """移除群聊（全删字段池）后重新接入，⛔ 也不能回收旧号。"""
        _seed(tmp_db)
        _add(tmp_db, "字段A")
        _add(tmp_db, "字段B")
        tmp_db.delete_group_cascade(GROUP)
        assert list(tmp_db.list_fields(GROUP, enabled_only=False)) == []
        tmp_db.create_group(GROUP, "测试群")
        assert _add(tmp_db, "新群新字段") == 3

    def test_legacy_group_without_seq_seeds_from_max(self, tmp_db):
        """旧库 / 手工造数据没有发号行：按现有最大号起种，⛔ 不重复发号。"""
        _seed(tmp_db)
        tmp_db.upsert_field(GROUP, 7, "历史字段", "个", [], True, 0)
        tmp_db._conn.execute(
            "DELETE FROM report_field_seq WHERE group_id=?", (GROUP,))
        tmp_db._conn.commit()
        assert tmp_db.next_field_id(GROUP) == 8

    def test_seq_seeded_by_migration(self, tmp_path):
        """v9 库升 v10：按现有最大 field_id 补种发号行。"""
        from app.state import StateStore
        db = tmp_path / "v9.db"
        store = StateStore(db)
        try:
            store.create_group(GROUP, "测试群")
            store.upsert_field(GROUP, 3, "旧字段", "个", [], True, 0)
            store._conn.execute(
                "DELETE FROM report_field_seq WHERE group_id=?", (GROUP,))
            store._conn.execute(
                "UPDATE meta SET value='9' WHERE key='schema_version'")
            store._conn.commit()
        finally:
            store.close()

        store = StateStore(db)
        try:
            version = store._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()["value"]
            assert version == "12"
            row = store._conn.execute(
                "SELECT last_id FROM report_field_seq WHERE group_id=?",
                (GROUP,)).fetchone()
            assert row["last_id"] == 3
            assert store.next_field_id(GROUP) == 4
        finally:
            store.close()

    def test_migration_idempotent(self, tmp_path):
        """重复打开同一库 ⛔ 不会把发号推高。"""
        from app.state import StateStore
        db = tmp_path / "v10.db"
        store = StateStore(db)
        try:
            store.create_group(GROUP, "测试群")
            _add(store, "字段A")               # field_id=1，last_id=1
        finally:
            store.close()
        store = StateStore(db)
        try:
            row = store._conn.execute(
                "SELECT last_id FROM report_field_seq WHERE group_id=?",
                (GROUP,)).fetchone()
            assert row["last_id"] == 1
            assert store.next_field_id(GROUP) == 2
        finally:
            store.close()


def _ctx(store):
    """最小 ctx：``field_pool`` 只用 ctx.state + ctx.group_id。

    ⛔ 不构造完整 HandlerContext（会牵出 backend / now 等无关依赖）。
    """
    from types import SimpleNamespace
    return SimpleNamespace(state=store, group_id=GROUP)


def test_schema_has_seq_table(tmp_db):
    cols = {r["name"] for r in
            tmp_db._conn.execute("PRAGMA table_info(report_field_seq)")}
    assert cols == {"group_id", "last_id"}
