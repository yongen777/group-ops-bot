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
