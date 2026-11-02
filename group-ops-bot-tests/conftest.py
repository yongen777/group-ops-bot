# -*- coding: utf-8 -*-
"""pytest 共享夹具。

测试目录纪律（§2.8 / §2.13）：
- 测试替身与故障模拟只存在于本目录，⛔ 不进生产包
- 本地对话测试与真实平台回调测试分别记录（§9.1）
- 单元测试只验证机器人侧逻辑，⛔ 不构成接口可用性证据
"""

import sys
from pathlib import Path

import pytest

BOT_ROOT = Path(__file__).resolve().parent.parent / "group-ops-bot"
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))


@pytest.fixture()
def tmp_db(tmp_path):
    """临时 SQLite 库（每个测试独立，⛔ 不碰任何共享库）。"""
    from app.state import StateStore
    store = StateStore(tmp_path / "test_state.db")
    yield store
    store.close()


@pytest.fixture(autouse=True)
def _reset_reply_rate_window():
    """D51：回调入口有「每群每分钟 20 条回复」的限流（挡刷屏）。

    ⚠️ 它的窗口是**模块级内存**，同一进程里所有测试共用一个窗口 ——
    不清理的话，跑到第 21 个用例就会被误拦（实测打挂 77 个）。
    这里每个测试前后各清一次，让限流逻辑只在专门用例里被测。
    """
    try:
        from app.web import server as _web

        _web._REPLY_WINDOW.clear()
        yield
        _web._REPLY_WINDOW.clear()
    except Exception:  # pragma: no cover - 导入失败不影响其它测试
        yield
