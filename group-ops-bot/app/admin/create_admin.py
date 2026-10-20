# -*- coding: utf-8 -*-
"""离线创建后台管理员账号（⭐ 2026-10-20 用户指令：env 引导整体移除后的唯一建号入口）。

用法（在服务器 repo 目录、以服务同一套 env 执行）::

    python -m app.admin.create_admin --username 郑佳
    python -m app.admin.create_admin --username 郑佳 --db /opt/group-ops/data/bot_state.db
    python -m app.admin.create_admin --username 郑佳 --password 'xxxx'   # 自动化用，慎用

设计纪律：
- 密码默认走 ``getpass``（⛔ 不进 env、⛔ 不进 shell 历史、⛔ 不进日志）
- 只写 ``admin_user`` 表，⛔ 不碰任何业务表；账号已存在时明确报错退出（不覆盖）
- ``--must-change`` 让新号首次登录强制改密（给交接场景用）
- 退出码：0 成功 / 2 参数或校验失败 / 3 库不存在（⛔ 不静默新建空库）
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from . import auth as admin_auth
from ..state import StateStore


def _db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    data_dir = os.environ.get("GOPS_DATA_DIR", "").strip() or "data"
    return Path(data_dir) / "bot_state.db"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.admin.create_admin",
        description="离线创建后台管理员账号（不依赖任何环境变量凭据）")
    parser.add_argument("--username", required=True, help="登录账号名（2~32 位）")
    parser.add_argument("--password", default=None,
                        help="密码（省略则交互输入，推荐省略）")
    parser.add_argument("--db", default=None,
                        help="bot_state.db 路径（默认 $GOPS_DATA_DIR/bot_state.db）")
    parser.add_argument("--must-change", action="store_true",
                        help="首次登录强制改密（交接场景建议开启）")
    args = parser.parse_args(argv)

    db_path = _db_path(args.db)
    if not db_path.exists():
        print("错误：数据库不存在：%s（⛔ 不会自动新建；请确认 --db 或 "
              "GOPS_DATA_DIR）" % db_path, file=sys.stderr)
        return 3

    password = args.password
    if password is None:
        password = getpass.getpass("请输入新账号密码（≥8 位）：")
        confirm = getpass.getpass("请再次输入确认：")
        if password != confirm:
            print("错误：两次输入的密码不一致", file=sys.stderr)
            return 2

    state = StateStore(db_path)
    try:
        user_id = admin_auth.create_admin(
            state, args.username, password,
            must_change_password=bool(args.must_change))
    except ValueError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    finally:
        state.close()

    print("已创建后台管理员：%s（user_id=%s，必须改密=%s）"
          % (args.username.strip(), user_id, "是" if args.must_change else "否"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
