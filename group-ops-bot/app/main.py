# -*- coding: utf-8 -*-
"""群运维机器人服务入口（:18002 群回调 + H5 同源）。

启动（fail-closed，缺配置即拒启）：
    GOPS_MCP_BASE_URL=http://127.0.0.1:18001 \
    GOPS_EXTERNAL_BASE_URL=https://<公网入口> \
    GOPS_DATA_DIR=./data \
    python -m app.main

配置纪律见 app/config.py：凭据只走环境变量，⛔ 不落盘、⛔ 不进日志。
⛔ 本服务不设任何「模拟运行模式」；联调证据以 §7.1 记录为准。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn

from . import period as period_mod
from . import recovery as recovery_handler
from .admin.server import create_admin_app
from .backend.mcp_adapter import McpSseAdapter
from .config import BotConfig, load_bot_config
from .handlers import reminder as reminder_handler
from .platform.qmx import QmxAdapter
from .state import StateStore
from .web.server import create_app

LOG = logging.getLogger("group-ops.main")

SCHEDULER_INTERVAL_SECONDS = 60


def _build(config: BotConfig, state: StateStore):
    backend = McpSseAdapter(
        base_url=config.mcp.base_url,
        timeout_seconds=config.mcp.timeout_seconds)
    # 阶段八：出站统一走平台适配器（reply/push/file 三口收敛，实测只改一处）
    # 2026-10-16：push 已实测接通（载荷 type=text + textMsg.content）；
    # 群级 webhook 地址由 state 提供 —— ⛔ 未绑定/未启用的群如实返回未发送
    sender = QmxAdapter(url_resolver=lambda gid: _instance_push_url(state, gid))
    app = create_app(state=state, backend=backend, sender=sender,
                     config=config)
    return app, backend, sender


def _instance_push_url(state: StateStore, group_id: str) -> Optional[str]:
    """该群可用于推送的 webhook 地址（⛔ 仅 ACTIVE 实例；未就绪返回 None）。

    fail-closed：实例缺失 / 非 ACTIVE / 地址为空 -> None，push 端口据此
    如实返回「未发送 + 原因」，⛔ 不回退到任何全局默认地址（无兜底凭据）。
    """
    try:
        row = state.get_robot_instance(group_id)
    except Exception:  # pragma: no cover - 查库异常按未就绪处理
        LOG.exception("get_robot_instance failed")
        return None
    if row is None or row["status"] != "ACTIVE":
        return None
    url = row["webhook_base_url"]
    return url if url and str(url).strip() else None


def _start_scheduler(state_path: str, backend, push, interval: int
                    ) -> threading.Thread:
    """定时提醒轮询（§4.5）+ C 类恢复查证（§4.8 第 7 条 / app/recovery.py）。

    独立线程 + 独立 SQLite 连接（WAL 多连接）。

    ⛔ 推送口径（2026-10-16 已实测接通）：每轮如实计算 + 落 reminder_plan；
    push 返回未发送时记 unsent + 原因，⛔ 不伪造已发送（平台把业务失败
    包在 HTTP 200 里，故 push 端口内部已按 JSON ok/code 判定）。
    ⛔ 恢复查证只读原请求键：不自动重试、不新建请求、不猜结论。
    """
    def loop() -> None:
        while True:
            try:
                time.sleep(interval)
                state = StateStore(state_path)
                try:
                    summary = reminder_handler.tick(
                        state, backend, push, period_mod.now())
                    if summary["targets"]:
                        LOG.info("reminder tick: %s", summary)
                    recovery = recovery_handler.tick(state, backend)
                    if recovery["processed"]:
                        LOG.info("recovery tick: %s", recovery)
                    # H5 上传文件保留期限清理（§4.11.4 D4 定夺；best-effort，
                    # ⛔ 失败只记日志不上抛 —— 清理是卫生行为，不是业务行为）
                    from .web import uploads as uploads_mod
                    cleaned = uploads_mod.cleanup_old_uploads(
                        data_dir=Path(state_path).parent, state=state,
                        now=period_mod.now())
                    if cleaned["deleted_rows"]:
                        LOG.info("upload cleanup: %s", cleaned)
                finally:
                    state.close()
            except Exception:  # pragma: no cover - 调度线程⛔ 不静默死掉
                LOG.exception("scheduler tick failed")

    thread = threading.Thread(target=loop, name="reminder-scheduler",
                              daemon=True)
    thread.start()
    return thread


def _start_admin(state: StateStore, backend, sender, config: BotConfig
                 ) -> threading.Thread:
    """:18003 管理后台独立监听（§3.1 / §4.7）。

    与 :18002 同进程（共享 StateStore 线程局部连接与同一 MCP 后端），
    独立端口独立应用 —— 控制面与入口面隔离。
    ⛔ 不申请 18003 公网映射（§2.10：仅内网 / VPN）。
    """
    app = create_admin_app(state=state, backend=backend, sender=sender,
                           config=config)
    server = uvicorn.Server(uvicorn.Config(
        app, host=config.admin_host, port=config.admin_port,
        log_config=None))

    def loop() -> None:
        try:
            server.run()
        except Exception:  # pragma: no cover - 管理端线程⛔ 不静默死掉
            LOG.exception("admin server failed")

    thread = threading.Thread(target=loop, name="admin-server", daemon=True)
    thread.start()
    return thread


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("GOPS_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # 第三方 HTTP 库的 INFO 日志会把**完整请求 URL** 打出来，而量子密信的
    # 回复地址形如 `.../webhook/send?key=<平台密钥>` —— 凭据进日志违反 §4.7。
    # 只保留 WARNING 以上（异常与失败仍可见），⛔ 不改我们自己的日志级别。
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    config = load_bot_config()
    data_dir = Path(config.data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_path = data_dir / "bot_state.db"

    state = StateStore(state_path)

    # 管理后台账号（⭐ 2026-10-20 用户指令：⛔ 不再读环境变量建号）。
    # 库里没有管理员时**不阻断启动** —— 业务链路（:12334 回调）照常，
    # 只打 WARN + 明确建号命令。一次误清空 admin_user 不应把整个机器人停掉。
    if state.count_admin_users() == 0:
        LOG.warning("admin: no admin account in %s — :18003 无法登录；"
                    "请在服务器执行 `python -m app.admin.create_admin` 建号",
                    state_path)

    app, backend, sender = _build(config, state)

    if os.environ.get("GOPS_ENABLE_SCHEDULER", "1") == "1":
        _start_scheduler(str(state_path), backend, sender.push_text,
                         SCHEDULER_INTERVAL_SECONDS)

    # §4.9 第 5 条：启动时先跑恢复扫描（崩溃窗口内的 C 类任务补账），再开服务
    try:
        recovery_handler.recover_on_startup(state, backend)
    except Exception:  # pragma: no cover - 启动扫描 best-effort，不挡服务
        LOG.exception("startup recovery scan failed")

    # :18003 管理后台（与 :18002 同进程双监听；共用同一平台适配器）
    _start_admin(state, backend, sender, config)

    LOG.info("group-ops-bot starting on %s:%s (mcp=%s, admin=%s:%s)",
             config.callback_host, config.callback_port, config.mcp.base_url,
             config.admin_host, config.admin_port)
    try:
        uvicorn.run(app, host=config.callback_host, port=config.callback_port,
                    log_config=None)
    finally:
        backend.close()
        state.close()


if __name__ == "__main__":
    main()
