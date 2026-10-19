# -*- coding: utf-8 -*-
"""管理后台页面路由（D3-C：FastAPI + Jinja2 + Tabler 服务端渲染）。

与 API 层（api.py）的边界：
- 页面只做渲染与会话门；交互数据仍走 /api/admin/*（前端 fetch + CSRF 头）
- 仪表盘 / 审计 / 统计 / 系统设置为纯读页，由本模块服务端直查渲染
  （口径与 API 共用 build_dashboard，不两套实现）
- 鉴权复用 admin/auth.load_session：未登录 -> 303 跳 /login；
  must_change_password 门与 API 层同源（不改密不进任何业务页）
- 本模块不引入任何构建步骤：模板 + /static 静态资源，零前端工具链
- 仪表盘按群独立展示（D3-C 用户指令）：不展示群总数，仅当前选中群数据；
  群选择走 ?group_id= 查询参数，缺省选中首个活跃群
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..backend.base import Backend
from ..config import BotConfig
from ..period import daily_key
from ..state import SCHEMA_VERSION, StateStore
from . import auth
from . import stats_export as stats_mod
from .api import build_dashboard

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# 群上下文档（sidebar「业务配置」下拉 + /groups/{group_id}/{tab} 路由）
GROUP_TABS = {
    "config": "群配置与开关",
    "members": "应交人员",
    "whitelist": "导出白名单",
    "fields": "报数字段池",
    "backfill": "后台补录",
}


def _identity(request: Request, state: StateStore) -> Optional[auth.AdminIdentity]:
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


def _group_or_404(state: StateStore, group_id: str):
    row = state.get_group_config(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="群不存在")
    return {"group_id": row["group_id"],
            "group_name": row["group_name"] or row["group_id"]}


def add_page_routes(app: FastAPI, *, state: StateStore, backend: Backend,
                    config: BotConfig) -> None:
    """挂载页面路由（由 create_admin_app 调用；不改 API 契约）。"""

    def _group_brief(row):
        return {"group_id": row["group_id"],
                "group_name": row["group_name"] or row["group_id"],
                "status": row["status"] if "status" in row.keys()
                          else "active"}

    def _sidebar_groups():
        """侧边栏多级菜单用的群列表（闭包绑定本应用 state，
        避免模块级全局在测试多库场景串库）。"""
        return [_group_brief(row) for row in state.list_groups()]

    def _sidebar_wait_bind():
        return sum(1 for inst in state.list_robot_instances()
                   if inst["status"] == "WAIT_BIND")

    def _ctx(request: Request, identity: auth.AdminIdentity, active: str,
             **extra):
        ctx = {
            "request": request,
            "active": active,
            "username": identity.username,
            "csrf_token": identity.csrf_token,
            "group": None,
            "groups": _sidebar_groups(),
            "instance_wait_bind": _sidebar_wait_bind(),
        }
        ctx.update(extra)
        return ctx

    # ---------- 登录 / 强制改密（无会话门） ----------

    @app.get("/login", response_class=HTMLResponse)
    def page_login(request: Request):
        identity = _identity(request, state)
        if identity is not None:
            target = ("/change-password"
                      if identity.must_change_password else "/")
            return RedirectResponse(target, status_code=303)
        return templates.TemplateResponse(
            request, "login.html", {"request": request, "active": "login",
                           "username": "", "csrf_token": "", "group": None})

    @app.get("/change-password", response_class=HTMLResponse)
    def page_change_password(request: Request):
        identity, redirect = _require_session(request, state,
                                              allow_must_change=True)
        if redirect is not None:
            return redirect
        if not identity.must_change_password:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request, "change_password.html",
            _ctx(request, identity, "changepw"))

    # ---------- 仪表盘（纯读，服务端渲染；按群独立展示） ----------

    @app.get("/", response_class=HTMLResponse)
    def page_dashboard(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        groups = _sidebar_groups()
        selected = (request.query_params.get("group_id") or "").strip()
        if not selected:
            active = [g for g in groups if g["status"] == "active"]
            selected = (active or groups)[0]["group_id"] if groups else ""
        payload = build_dashboard(state, backend,
                                  group_id=selected or None)
        return templates.TemplateResponse(
            request, "dashboard.html",
            _ctx(request, identity, "dashboard", data=payload,
                 selected_group_id=selected))

    # ---------- 群实例绑定（D3-B1 后端契约的 UI） ----------

    @app.get("/robot-instances", response_class=HTMLResponse)
    def page_robot_instances(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        return templates.TemplateResponse(
            request, "robot_instances.html", _ctx(request, identity, "instances"))

    # ---------- 群管理 ----------

    @app.get("/groups", response_class=HTMLResponse)
    def page_groups(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        groups = _sidebar_groups()
        discovered = [{"group_id": row["group_id"],
                       "last_phone": row["last_phone"],
                       "last_name": row["last_name"],
                       "msg_count": row["msg_count"]}
                      for row in state.list_unbound_groups()]
        return templates.TemplateResponse(
            request, "groups.html",
            _ctx(request, identity, "groups", groups=groups,
                 discovered=discovered))

    @app.get("/groups/{group_id:path}/{tab}", response_class=HTMLResponse)
    def page_group_tab(request: Request, group_id: str, tab: str):
        if tab not in GROUP_TABS:
            raise HTTPException(status_code=404, detail="页面不存在")
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        group = _group_or_404(state, group_id)
        return templates.TemplateResponse(
            request, "group_%s.html" % tab,
            _ctx(request, identity, "group-%s" % tab, group=group,
                 tab_label=GROUP_TABS[tab]))

    # ---------- 统计中心（纯读：按群多维统计 + 导出入口） ----------

    @app.get("/stats", response_class=HTMLResponse)
    def page_stats(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        groups = _sidebar_groups()
        known = {g["group_id"] for g in groups}
        selected = (request.query_params.get("group_id") or "").strip()
        if selected not in known:
            active = [g for g in groups if g["status"] == "active"]
            selected = (active or groups)[0]["group_id"] if groups else ""
        biz = (request.query_params.get("biz") or "").strip()
        if biz not in ("checkin", "summary", "report"):
            biz = "checkin"
        date_str = (request.query_params.get("date") or "").strip()
        if not date_str:
            date_str = daily_key()
        stats = None
        period_k = ""
        error = ""
        if selected:
            try:
                period_k = stats_mod.resolve_period_key(state, selected, biz,
                                                        date_str)
            except ValueError:
                error = "日期格式不正确，请选择有效日期"
            else:
                try:
                    stats = stats_mod.build_stats(state, backend, selected,
                                                  biz, period_k)
                except Exception:
                    error = "统计数据暂时查不到，请稍后重试"
        return templates.TemplateResponse(
            request, "stats.html",
            _ctx(request, identity, "stats", selected_group_id=selected,
                 biz=biz, date=date_str, period_key=period_k,
                 stats=stats, error=error))

    # ---------- 审计日志（纯读，服务端渲染） ----------

    @app.get("/audit", response_class=HTMLResponse)
    def page_audit(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        entries = [{"created_at": r["created_at"],
                    "username": r["username"], "action": r["action"],
                    "target": "%s %s" % (r["target_type"] or "",
                                         r["target_id"] or ""),
                    "result": r["result"], "ip": r["ip"],
                    "detail": r["detail"]}
                   for r in state.list_admin_audit(100)]
        return templates.TemplateResponse(
            request, "audit.html", _ctx(request, identity, "audit", entries=entries))

    # ---------- 系统设置（纯读：⛔ 不含任何凭据） ----------

    @app.get("/system", response_class=HTMLResponse)
    def page_system(request: Request):
        identity, redirect = _require_session(request, state)
        if redirect is not None:
            return redirect
        mcp = config.mcp
        info = {
            "schema_version": SCHEMA_VERSION,
            "callback": "%s:%s" % (config.callback_host, config.callback_port),
            "admin": "%s:%s" % (config.admin_host, config.admin_port),
            "external_base_url": config.external_base_url or "（未配置）",
            "mcp_base_url": mcp.base_url if mcp else "（未配置）",
            "mcp_timeout": mcp.timeout_seconds if mcp else "—",
            "tz": config.tz_name,
            "data_dir": config.data_dir,
            "scheduler": os.environ.get("GOPS_ENABLE_SCHEDULER", "1") == "1",
            "bind_admin_count": len(config.bind_admin_phones),
        }
        return templates.TemplateResponse(
            request, "system.html", _ctx(request, identity, "system", info=info))
