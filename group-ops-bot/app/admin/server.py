# -*- coding: utf-8 -*-
"""管理后台应用工厂（:18003 独立监听，§3.1 / §4.7）。

- 与 :18002 业务服务**同进程**（共享 StateStore 线程局部连接 + 同一 MCP
  后端实例），但独立端口独立应用：控制面与入口面隔离（§4.7）
- 页面：app/admin/templates/ + app/admin/static/（Jinja2 + Tabler，
  ⛔ 无构建步骤；D3-C 起替代原静态 SPA）
- API：app/admin/api.py（服务端会话鉴权 + CSRF + 审计）
- ⛔ 不申请 18003 公网映射（§2.10：仅内网 / VPN / 指定管理来源）
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from ..backend.base import Backend
from ..config import BotConfig
from ..state import StateStore
from ..web.sender import ReplySender
from .api import create_admin_router
from .pages import add_page_routes

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_admin_app(*, state: StateStore, backend: Backend,
                     sender: ReplySender, config: BotConfig,
                     now_fn=None) -> FastAPI:
    app = FastAPI(title="group-ops-admin", docs_url=None, redoc_url=None)
    app.state.store = state
    app.include_router(create_admin_router(
        state=state, backend=backend, sender=sender, config=config,
        now_fn=now_fn))

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)),
                  name="static")

    add_page_routes(app, state=state, backend=backend, config=config)

    return app
