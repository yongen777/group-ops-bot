# -*- coding: utf-8 -*-
"""本地冒烟：种子数据（群配置 / 字段池 / 白名单 / 应交名单）。

⛔ 只写本地冒烟库（smoke/state.db），⛔ 不碰任何共享库。
用法：python seed_state.py <db_path>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "group-ops-bot"))

from app.state import StateStore  # noqa: E402

GROUP = "G100"
PHONE = "13800138009"


def main(db_path: str) -> None:
    store = StateStore(db_path)
    store.create_group(GROUP, "测试群")
    store.upsert_group_config(GROUP, period_type="daily")
    store.upsert_field(GROUP, 1, "bug修复数", None, ["开发"], True, 1)
    store.upsert_field(GROUP, 2, "代码行数", "行", ["开发"], True, 2)
    store.upsert_member_task(GROUP, PHONE, role="开发")
    # 2026-10-19 单权化：白名单只有 ADMIN_ALL 一种（对象限应交成员）
    store.upsert_whitelist(GROUP, PHONE)
    store.close()
    print("seeded:", db_path)


if __name__ == "__main__":
    main(sys.argv[1])
