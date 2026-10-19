# -*- coding: utf-8 -*-
"""管理后台页面路由（2026-10-19 重构：React + Vite SPA 外壳）。

架构（用户 2026-10-19 指令：原样引入 React + Vite）：
- 前端：admin-ui/（React 18 + TS + Tailwind，视觉与交互照参考后台 1:1）
  构建产物落 app/admin/static/spa/，运行时**零构建、零外网依赖**
- 后端：本模块只负责「发外壳 + 会话门」，业务数据全部走 /api/admin/*
- 会话门与 API 层同源（auth.load_session）：未登录 303 /login；
  must_change_password 未完成 303 /change-password（⛔ 不进任何业务页）
- 兼容：旧书签 /groups/{group_id}/{tab} -> 303 /groups?group_id=…；
  已按用户指令移除的 /audit、/system -> 303 回仪表盘（⛔ 不做死链）
- 旧 Jinja2 模板与 Tabler 静态资源已于 2026-10-19 清除（用户指令「清除干净」）
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from ..backend.base import Backend
from ..config import BotConfig
from ..state import StateStore
from . import auth

_SPA_DIR = Path(__file__).resolve().parent / "static" / "spa"

# 旧群上下文档（仅用于兼容跳转校验，页面本身由 SPA 的 4 个 Tab 承载）
GROUP_TABS = {
    "config": "群配置与开关",
    "members": "群成员",
    "whitelist": "授权群管理员",
    "fields": "报数字段池",
}

_SPA_ROUTES = ("/", "/robot-instances", "/groups", "/stats",
               "/login", "/change-password")


def _identity(request: Request,
              state: StateStore) -> Optional[auth.AdminIdentity]:
    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    return auth.load_session(state, token)


def _require_session(request: Request, state: StateStore, *,
                     allow_must_change: bool = False):
    """返回 (identity, redirect)。redirect 非 None 时调用方直接返回它。"""
    identity = _identity(request, state)
    if identity is None:
        return None, RedirectResponse("/login", status_code=303)
    if identity.must_change_password and not allow_must_change:
        return None, RedirectResponse("/change-password", status_code=303)
    return identity, None


def _spa_index() -> HTMLResponse:
    """外壳页面（禁止缓存：改版后不能让浏览器拿旧外壳）。"""
    index = _SPA_DIR / "index.html"
    if not index.is_file():
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'>"
            "<title>后台前端未构建</title>"
            "<p>后台前端产物缺失：请在 admin-ui 目录执行构建后再访问。</p>",
            status_code=503, headers={"Cache-Control": "no-store"})
    return HTMLResponse(
        index.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"})


def _group_or_404(state: StateStore, group_id: str):
    row = state.get_group_config(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="群不存在")
    return {"group_id": row["group_id"],
            "group_name": row["group_name"] or row["group_id"]}


def add_page_routes(app: FastAPI, *, state: StateStore, backend: Backend,
                    config: BotConfig) -> None:
    """挂载页面路由（由 create_admin_app 调用；不改 API 契约）。"""
    router = APIRouter()

    # ---------- 登录 / 强制改密（SPA 自渲染表单，服务端只发外壳） ----------

    @router.get("/login", response_class=HTMLResponse)
    def page_login(request: Request):
        identity = _identity(request, state)
        if identity is not None:
            target = ("/change-password"
                      if identity.must_change_password else "/")
            return RedirectResponse(target, status_code=303)
        return _spa_index()

    @router.get("/change-password", response_class=HTMLResponse)
    def page_change_password(request: Request):
        identity, redirect = _require_session(request, state,
                                              allow_must_change=True)
        if redirect is not None:
            return redirect
        if not identity.must_change_password:
            return RedirectResponse("/", status_code=303)
        return _spa_index()

    # ---------- 4 大板块（外壳 + 会话门） ----------

    @router.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return _spa_index()

    @router.get("/robot-instances", response_class=HTMLResponse)
    def page_robot_instances(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return _spa_index()

    @router.get("/groups", response_class=HTMLResponse)
    def page_groups(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return _spa_index()

    @router.get("/stats", response_class=HTMLResponse)
    def page_stats(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return _spa_index()

    # ---------- 旧路由兼容（书签 / 外部链接） ----------

    @router.get("/groups/{group_id:path}/{tab}")
    def page_group_tab(request: Request, group_id: str, tab: str):
        if tab not in GROUP_TABS:
            raise HTTPException(status_code=404, detail="页面不存在")
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        _group_or_404(state, group_id)
        return RedirectResponse("/groups?group_id=%s" % group_id,
                                status_code=303)

    @router.get("/audit")
    def page_audit(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return RedirectResponse("/", status_code=303)

    @router.get("/system")
    def page_system(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return RedirectResponse("/", status_code=303)

    app.include_router(router)
