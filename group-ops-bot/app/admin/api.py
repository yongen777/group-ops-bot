# -*- coding: utf-8 -*-
"""管理后台 API（:18003，§4.7 / 用户指令第一阶段）。

覆盖四项配置（群管理 / 群业务开关 / 应交人员 / 导出白名单）+ §2.12 另两项
（报数字段池 / 提醒规则）+ 登录鉴权与审计 + P0 群绑定（发现列表 / 后台绑定）。

硬约束：
- 未登录 ⛔ 不能改配置；绕前端直调同样被拒（服务端会话校验，§4.7 完成标准）
- 强制改密未完成时，除改密/登出/me 外一律 403
- 变更落库 + admin_audit 留痕（操作人 / 动作 / 目标 / 结果 / IP）
- ⛔ 三概念分离：群（授权）/ 应交名单 / 导出白名单是三张表三个接口，
  ⛔ 不混成一个字段（§4.7 关键约束）
- ⛔ 管理员凭据不进任何响应体与日志
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, HTTPException, Request, Response

from ..backend.base import Backend
from ..config import BotConfig
from ..state import StateStore
from . import auth
from . import board as board_mod
from . import stats_export as stats_mod

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SLOT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_MAX_GROUP_ID = 64
_MAX_NAME = 64
_MAX_ROLE = 32
# ⭐ 2026-10-20 用户指令：后台统计导出统一 xlsx（CSV 已下线）
_XLSX_MEDIA = ("application/vnd.openxmlformats-officedocument"
               ".spreadsheetml.sheet")

LOGIN_LOCKED_TEXT = "失败次数过多，账号已锁定，请稍后再试"

LOG = logging.getLogger("group-ops.admin")


def _validate_hhmm(value, field: str) -> str:
    if not isinstance(value, str) or not _HHMM_RE.match(value):
        raise HTTPException(status_code=400,
                            detail="%s 必须是 HH:MM 格式" % field)
    return value


def _normalize_clock(value, date_str: str) -> Optional[str]:
    """补录时间 -> "YYYY-MM-DD HH:MM:SS"（MySQL DATETIME 口径）。

    前端只提供 HH:MM（可带秒），日期取管理员选的补录日期；
    空值 / 未提供 -> None，由服务端记 NOW(3)（等价旧行为）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text:
        text = text.split("T", 1)[1]
    text = text.split("+", 1)[0].split("Z", 1)[0]
    parts = text.split(":")
    try:
        hour = int(parts[0])
        minute = int(parts[1])
        second = int(float(parts[2])) if len(parts) > 2 else 0
    except (ValueError, IndexError):
        raise HTTPException(status_code=400,
                            detail="补录时间格式不正确，请填写 HH:MM")
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise HTTPException(status_code=400,
                            detail="补录时间格式不正确，请填写 HH:MM")
    return "%s %02d:%02d:%02d" % (date_str, hour, minute, second)


def _resolve_period_from_body(state: StateStore, group_id: str, body: dict,
                              biz: str) -> str:
    """请求体 -> 周期键（2026-10-19：补录改按「日期」提交）。

    优先 date（YYYY-MM-DD）；兼容旧调用方的 period_key。
    总结按群配置的周期类型折算（日报=当天，周报=所在 ISO 周）。

    ⚠️ state 必须显式传入：本函数是模块级函数，早期版本直接引用
    create_admin_router 闭包里的 state，date 路径必炸（2026-10-19 修复）。
    """
    date_str = body.get("date")
    if isinstance(date_str, str) and date_str.strip():
        date_str = date_str.strip()
        try:
            return stats_mod.resolve_period_key(state, group_id, biz, date_str)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="日期格式不正确，请选择有效日期")
    legacy = body.get("period_key")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    raise HTTPException(status_code=400, detail="补录日期不能为空")


def _body_date(body: dict) -> str:
    """请求体里的业务日期（补录时间需要拼 YYYY-MM-DD HH:MM:SS）。"""
    date_str = body.get("date")
    if isinstance(date_str, str) and date_str.strip():
        return date_str.strip()
    legacy = body.get("period_key")
    if isinstance(legacy, str) and legacy.strip():
        return legacy.strip()
    raise HTTPException(status_code=400, detail="补录日期不能为空")


def _clean_str(value, field: str, max_len: int, required: bool = True
               ) -> Optional[str]:
    if value is None:
        if required:
            raise HTTPException(status_code=400, detail="%s 不能为空" % field)
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="%s 必须是字符串" % field)
    text = value.strip()
    if not text:
        if required:
            raise HTTPException(status_code=400, detail="%s 不能为空" % field)
        return None
    if len(text) > max_len:
        raise HTTPException(status_code=400,
                            detail="%s 长度不能超过 %d" % (field, max_len))
    return text


def _identity(request: Request, state: StateStore) -> auth.AdminIdentity:
    token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
    identity = auth.load_session(state, token)
    if identity is None:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return identity


# ---------- 打卡时间段校验（用户指令验收项 2：多区间 + 禁重叠） ----------

def _validate_slots(payload) -> dict:
    """校验打卡时间段配置，返回规范化存储值 {"slots":[...]}。

    硬规则：1~8 个区间；id 唯一（字母数字_-，≤16）；start < end
    （⛔ 不做跨夜区间）；**禁止区间重叠**（按开始时刻排序后逐一比对）。
    旧格式 {"am":"09:00","pm":"14:50"} 仍接受（转换为两个区间）。
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="打卡时间段配置格式不正确，请刷新页面重试")
    if "slots" not in payload:
        # 旧格式兼容（外部调用方）：两个截止时刻都必须给且合法
        for key in ("am", "pm"):
            if key not in payload:
                raise HTTPException(
                    status_code=400,
                    detail="旧格式必须同时提供 am / pm 两个截止时刻；"
                           "多时间段请用 {\"slots\": [...]}")
            _validate_hhmm(payload.get(key),
                           "上午截止" if key == "am" else "下午截止")
        return {"am": payload.get("am"), "pm": payload.get("pm")}
    slots = payload.get("slots")
    if not isinstance(slots, list) or not (1 <= len(slots) <= 8):
        raise HTTPException(status_code=400,
                            detail="时间段数量必须在 1~8 之间")
    normalized, seen = [], set()
    for item in slots:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="时间段格式错误")
        slot_id = item.get("id")
        if (not isinstance(slot_id, str) or not slot_id.strip()
                or len(slot_id) > 16
                or not _SLOT_ID_RE.match(slot_id)):
            raise HTTPException(
                status_code=400,
                detail="时间段 id 必须是 1~16 位字母/数字/-/_")
        if slot_id in seen:
            raise HTTPException(status_code=400,
                                detail="时间段 id 重复：%s" % slot_id)
        seen.add(slot_id)
        name = item.get("name")
        if name is not None and (not isinstance(name, str)
                                 or len(name) > 32):
            raise HTTPException(status_code=400,
                                detail="时间段名称不能超过 32 字符")
        start = _validate_hhmm(item.get("start"), "开始时间")
        end = _validate_hhmm(item.get("end"), "结束时间")
        if start >= end:
            raise HTTPException(
                status_code=400,
                detail="时间段「%s」的开始时间需要早于结束时间，且不支持跨夜"
                % slot_id)
        # ⭐ 2026-10-20 用户方案：提醒时间点必须落在时段**内部**（开区间）。
        # 8:00-8:30 的时段只能填 08:01~08:29 —— 等于开始 / 等于结束都拒绝。
        # 留空 -> 不写 remind_at，读取时按「结束前 5 分钟」补默认值。
        remind_at = item.get("remind_at")
        if isinstance(remind_at, str):
            remind_at = remind_at.strip()
        if remind_at in (None, ""):
            remind_at = None
        else:
            _validate_hhmm(remind_at, "提醒时间")
            from ..handlers import checkin as _checkin_mod
            problem = _checkin_mod.remind_at_error(start, end, remind_at)
            if problem:
                raise HTTPException(
                    status_code=400,
                    detail="时段「%s」%s（提醒时间必须严格落在时段开始与结束"
                           "之间，例如 08:00-08:30 的时段可填 08:01~08:29）"
                           % (slot_id, problem))
        entry = {"id": slot_id, "name": (name or "").strip(),
                 "start": start, "end": end}
        if remind_at:
            entry["remind_at"] = remind_at
        normalized.append(entry)
    normalized.sort(key=lambda s: s["start"])
    for prev, cur in zip(normalized, normalized[1:]):
        if cur["start"] < prev["end"]:
            raise HTTPException(
                status_code=400,
                detail="时间段重叠：「%s」（%s~%s）与「%s」（%s~%s）冲突，"
                       "保存被拒绝" % (prev["id"], prev["start"], prev["end"],
                                      cur["id"], cur["start"], cur["end"]))
    return {"slots": normalized}


# ---------- 提醒规则校验（用户指令验收项 4：按业务独立配置） ----------

def _validate_reminder_rules(payload, current_raw=None) -> dict:
    """校验提醒规则：每个业务独立 enabled / times / text。

    current_raw：库内现有 reminder_rules（部分更新时合并底座）。
    ⛔ 合并语义：未提供的业务**原样保留**（2026-10-16 用户指令三起，
    打卡行只送 enabled，若整列替换会把总结 / 报数规则清掉）。
    """
    from ..handlers import reminder as reminder_handler
    merged = reminder_handler.normalize_reminder_rules(current_raw)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="提醒规则格式不正确，请刷新页面重试")
    result = {biz: dict(merged[biz]) for biz in ("checkin", "summary",
                                                 "report")}
    for biz in ("checkin", "summary", "report"):
        value = payload.get(biz)
        if value is None:
            continue
        if isinstance(value, list):  # 旧格式：纯时间列表
            value = {"enabled": True, "times": value}
        if not isinstance(value, dict):
            raise HTTPException(
                status_code=400,
                detail="%s 的提醒规则格式不正确" % biz)
        rule = {"enabled": bool(value.get("enabled",
                                          result[biz]["enabled"]))}
        times = value.get("times")
        if times is None:
            # 未提供 times（如打卡行只送开关）-> 保留原值不清空
            times = result[biz]["times"]
        if not isinstance(times, list) or len(times) > 5:
            raise HTTPException(
                status_code=400,
                detail="%s 的提醒时间必须是 1~5 个 HH:MM" % biz)
        clean = []
        for t in times:
            clean.append(_validate_hhmm(t, "%s 提醒时间" % biz))
        if clean:
            rule["times"] = clean
        text = value.get("text")
        if text is None:
            text = result[biz]["text"]
        if not isinstance(text, str) or len(text) > 200:
            raise HTTPException(
                status_code=400,
                detail="%s 的提醒文本不能超过 200 字符" % biz)
        rule["text"] = text.strip()
        result[biz] = rule
    if not any(biz in payload for biz in ("checkin", "summary", "report")):
        raise HTTPException(status_code=400, detail="没有需要更新的提醒规则")
    return result


def _require_csrf(request: Request, identity: auth.AdminIdentity) -> None:
    if not auth.verify_csrf(identity, request):
        raise HTTPException(status_code=403, detail="操作已过期，请刷新页面后重试")


def _require_password_changed(identity: auth.AdminIdentity) -> None:
    if identity.must_change_password:
        raise HTTPException(
            status_code=403,
            detail="请先将初始密码修改为正式密码后再使用其他功能")


def _group_row(state: StateStore, group_id: str):
    row = state.get_group_config(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="群不存在")
    return row


def _require_member(state: StateStore, group_id: str, user_id: str) -> None:
    """白名单授权强校验（2026-10-19 用户指令）：授权对象限本群应交成员。

    ⛔ 非成员（含已停用成员）一律 400 —— 后台从群成员下拉选择，此处在
    API 层兜底，防绕过页面直调。
    """
    for row in state.list_member_tasks(group_id, enabled_only=True):
        if row["user_id"] == user_id:
            return
    raise HTTPException(
        status_code=400,
        detail="仅可授权本群应交人员（请先在群设置-成员中添加该成员）")


def _member_nickname(state: StateStore, group_id: str,
                     user_id: str):
    """白名单列表展示名：成员昵称优先，⛔ 取不到返 None（前端回落手机号）。"""
    for row in state.list_member_tasks(group_id, enabled_only=False):
        if row["user_id"] == user_id and row["nickname"]:
            return row["nickname"]
    return None


def _instance_row(state: StateStore, group_id: str):
    """机器人实例行（v6，D3-B1）：无记录 -> 404。⛔ V1 不支持手工预建，
    实例只能由真实回调 T0 自动发现（与「group_id 不让手填」纪律一致）。"""
    row = state.get_robot_instance(group_id)
    if row is None:
        raise HTTPException(status_code=404, detail="机器人实例不存在")
    return row


def _row_get(row, key: str, default=None):
    """按列名取 sqlite3.Row 的值（⛔ 缺列不抛异常，返回 default）。

    用途：`_instance_payload` 既要能吃掉带 `group_name` 的新行（LEFT JOIN
    群配置），也要能吃掉只 `SELECT *` 的旧形态行（测试里的裸实例行）。
    """
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def _instance_payload(row) -> dict:
    """实例行 -> API 载荷（凭证掩码硬约束：⛔ 不返回密文列、不返回明文、
    不返回长度线索 —— webhook_key_masked 固定 "****" 或 null）。

    ⭐ 2026-10-20：额外返回 `group_name`（群配置里用户绑定时录入的群名）。
    前端显示优先级 display_name（后台备注名）-> group_name -> group_id，
    修掉「群里录入了群名、后台却显示群 ID」的问题。
    """
    key_set = bool(row["webhook_key_encrypted"])
    return {
        "group_id": row["group_id"],
        "display_name": row["display_name"],
        "group_name": _row_get(row, "group_name"),
        "webhook_base_url": row["webhook_base_url"],
        "webhook_key_set": key_set,
        "webhook_key_masked": "****" if key_set else None,
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _audit(state: StateStore, request: Request, identity, action: str,
           target_type: Optional[str], target_id: Optional[str],
           detail: Optional[dict], result: str = "ok") -> None:
    state.insert_admin_audit(identity.username, action, target_type,
                             target_id, detail, result,
                             auth.client_ip(request))


def create_admin_router(*, state: StateStore, backend: Backend,
                        sender, config: BotConfig,
                        now_fn=None) -> APIRouter:
    """now_fn：时钟注入口（手动提醒用）。⛔ 生产不传 = 真实时钟；
    测试注入固定时刻 —— 否则提醒规则「是否过点」随墙钟漂，测试会时好时坏。
    """
    from .. import period as _period_mod
    _now = now_fn or _period_mod.now
    router = APIRouter(prefix="/api/admin")

    # ---------- 鉴权 ----------

    @router.post("/login")
    def login(body: dict = Body(...), request: Request = None,
              response: Response = None):
        username = _clean_str(body.get("username"), "用户名", 64)
        password = body.get("password")
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400, detail="密码不能为空")
        ok, identity = auth.login(state, username, password,
                                  auth.client_ip(request))
        if ok == "locked":
            raise HTTPException(status_code=429, detail=LOGIN_LOCKED_TEXT)
        if ok != "ok":
            _audit(state, request, _anonymous(), "login", None, username,
                   {"result": "invalid_credentials"}, result="rejected")
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = auth.create_session(state, identity, auth.client_ip(request))
        response.set_cookie(
            auth.SESSION_COOKIE_NAME, token, httponly=True,
            samesite="lax", secure=auth.cookie_secure(request),
            max_age=auth.SESSION_ABSOLUTE_HOURS * 3600, path="/")
        _audit(state, request, identity, "login", None, username, None)
        return {"ok": True, "username": identity.username,
                "must_change_password": identity.must_change_password,
                "csrf_token": identity.csrf_token}

    def _anonymous():
        return auth.AdminIdentity(user_id=0, username="(anonymous)",
                                  must_change_password=False, csrf_token="",
                                  session_token_hash="")

    @router.post("/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(auth.SESSION_COOKIE_NAME, "")
        auth.destroy_session(state, token)
        response.delete_cookie(auth.SESSION_COOKIE_NAME, path="/")
        return {"ok": True}

    @router.get("/me")
    def me(request: Request):
        identity = _identity(request, state)
        return {"ok": True, "username": identity.username,
                "must_change_password": identity.must_change_password,
                "csrf_token": identity.csrf_token}

    @router.post("/change-password")
    def change_password(body: dict = Body(...), request: Request = None):
        identity = _identity(request, state)
        _require_csrf(request, identity)
        old_password = body.get("old_password")
        new_password = body.get("new_password")
        if not isinstance(old_password, str) or not isinstance(
                new_password, str):
            raise HTTPException(status_code=400, detail="密码不能为空")
        ok, error = auth.change_password(state, identity, old_password,
                                         new_password)
        if not ok:
            _audit(state, request, identity, "change_password", None,
                   identity.username, {"result": error}, result="rejected")
            raise HTTPException(status_code=400, detail=error)
        _audit(state, request, identity, "change_password", None,
               identity.username, None)
        return {"ok": True, "message": "密码已修改，请重新登录"}

    @router.post("/callback-url")
    def callback_url(body: dict = Body(...), request: Request = None):
        """查看群回调地址（⭐ 2026-10-20 用户指令）。

        - **必须再次输入登录密码**才返回地址（⛔ 不另设一套查看口令）
        - 地址来自部署配置（`GOPS_EXTERNAL_BASE_URL` + `/api/callback`），
          后台**只读展示**，⛔ 不提供任何修改入口（改了会影响平台侧配置）
        - 审计只记「谁看过」，⛔ 不落密码
        """
        identity = _identity(request, state)
        _require_csrf(request, identity)
        password = body.get("password")
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400,
                                detail="请输入登录密码以验证身份")
        user = state.get_admin_user_by_id(identity.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录或会话已过期")
        if not auth.verify_password(password, user["password_hash"]):
            _audit(state, request, identity, "callback_url.view", None,
                   identity.username, {"result": "bad_password"},
                   result="rejected")
            raise HTTPException(status_code=403,
                                detail="密码不正确，无法查看回调地址")
        base = ((config.external_base_url if config else "") or "").rstrip("/")
        _audit(state, request, identity, "callback_url.view", None,
               identity.username, {"base_url": base})
        return {"ok": True, "url": base + "/api/callback",
                "base_url": base, "path": "/api/callback",
                "method": "POST", "readonly": True,
                "note": "该地址由服务器部署配置决定，后台只能查看与复制，不能修改"}

    @router.post("/username")
    def change_username(body: dict = Body(...), request: Request = None):
        """修改管理员登录账号名（⭐ 2026-10-20 用户指令）。

        需再次输入登录密码：账号名本身就是登录凭据，⛔ 不允许仅凭会话直接改。
        改完不吊销当前会话（load_session 按 user_id 回读用户名，立即生效）。
        """
        identity = _identity(request, state)
        _require_csrf(request, identity)
        new_username = _clean_str(body.get("new_username"), "新账号名", 32)
        password = body.get("password")
        if not isinstance(password, str) or not password:
            raise HTTPException(status_code=400,
                                detail="请输入登录密码以验证身份")
        ok, error = auth.change_username(state, identity, new_username, password)
        if not ok:
            _audit(state, request, identity, "admin.rename", None,
                   identity.username, {"result": error}, result="rejected")
            raise HTTPException(status_code=400, detail=error)
        _audit(state, request, identity, "admin.rename", None,
               identity.username,
               {"old_username": identity.username, "new_username": new_username})
        return {"ok": True, "username": new_username}

    # ---------- 群管理 ----------

    @router.get("/groups")
    def list_groups(request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        groups = []
        for row in state.list_groups():
            switches = state.normalized_switches(row)
            groups.append({
                "group_id": row["group_id"],
                "group_name": row["group_name"] if "group_name" in row.keys() else None,
                "status": row["status"] if "status" in row.keys() else "active",
                "switches": switches,
                "period_type": row["period_type"],
                "daily_cutoff": row["daily_cutoff"],
                "member_count": len(state.list_member_tasks(row["group_id"])),
            })
        return {"ok": True, "groups": groups}

    @router.post("/groups")
    def create_group(body: dict = Body(...), request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        group_id = _clean_str(body.get("group_id"), "群标识", _MAX_GROUP_ID)
        group_name = _clean_str(body.get("group_name"), "群名称", _MAX_NAME,
                                required=False)
        if state.get_group_config(group_id) is not None:
            raise HTTPException(status_code=409, detail="群已存在")
        state.create_group(group_id, group_name)
        _audit(state, request, identity, "group.create", "group", group_id,
               {"group_name": group_name})
        return {"ok": True, "group_id": group_id}

    @router.put("/groups/{group_id}")
    def update_group(group_id: str, body: dict = Body(...),
                     request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        fields = {}
        if "group_name" in body and body["group_name"] is not None:
            fields["group_name"] = _clean_str(body["group_name"], "群名称",
                                              _MAX_NAME)
        if "status" in body and body["status"] is not None:
            status = body["status"]
            if status not in ("active", "disabled"):
                raise HTTPException(status_code=400,
                                    detail="状态只能选择正常或停用")
            fields["status"] = status
        if not fields:
            raise HTTPException(status_code=400, detail="没有需要更新的字段")
        state.upsert_group_config(group_id, **fields)
        _audit(state, request, identity, "group.update", "group", group_id,
               fields)
        return {"ok": True}

    @router.get("/groups/{group_id}/config")
    def get_config(group_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        row = _group_row(state, group_id)
        return {"ok": True, "config": _config_payload(state, row)}

    @router.put("/groups/{group_id}/config")
    def update_config(group_id: str, body: dict = Body(...),
                      request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        row = _group_row(state, group_id)
        fields = {}
        if "switches" in body and body["switches"] is not None:
            switches = body["switches"]
            if not isinstance(switches, dict):
                raise HTTPException(status_code=400, detail="开关配置格式不正确，请刷新页面重试")
            current = state.normalized_switches(row)
            for key in StateStore.BIZ_SWITCH_KEYS:
                if key in switches:
                    current[key] = bool(switches[key])
            fields["biz_switches"] = current
        if "period_type" in body and body["period_type"] is not None:
            if body["period_type"] not in ("daily", "weekly"):
                raise HTTPException(status_code=400,
                                    detail="周期类型只能选择日报或周报")
            fields["period_type"] = body["period_type"]
        if "daily_cutoff" in body and body["daily_cutoff"] is not None:
            fields["daily_cutoff"] = _validate_hhmm(body["daily_cutoff"],
                                                    "截止时间")
        if "checkin_deadlines" in body and body["checkin_deadlines"] is not None:
            fields["checkin_deadlines"] = _validate_slots(
                body["checkin_deadlines"])
        if "checkin_require_image" in body and \
                body["checkin_require_image"] is not None:
            fields["checkin_require_image"] = 1 if body[
                "checkin_require_image"] else 0
        if "reminder_rules" in body and body["reminder_rules"] is not None:
            fields["reminder_rules"] = _validate_reminder_rules(
                body["reminder_rules"],
                current_raw=row["reminder_rules"] if row else None)
        if not fields:
            raise HTTPException(status_code=400, detail="没有需要更新的字段")
        state.upsert_group_config(group_id, **fields)
        _audit(state, request, identity, "group.config_update", "group",
               group_id, _audit_safe(fields))
        return {"ok": True, "config": _config_payload(
            state, state.get_group_config(group_id))}

    # ---------- 群绑定（P0：后台路径，与群内「绑定」命令双路径） ----------

    @router.get("/groups/discovered")
    def list_discovered(request: Request):
        """回调见过但没有 group_config 的群（机器人进群后即可在此绑定）。

        ⛔ 本列表不是授权：绑定动作才建 group_config，授权判定仍看它。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        groups = [{"group_id": r["group_id"],
                   "last_phone": r["last_phone"],
                   "last_name": r["last_name"],
                   "msg_count": r["msg_count"],
                   "last_seen": r["last_seen"]}
                  for r in state.list_unbound_groups()]
        return {"ok": True, "discovered": groups}

    @router.post("/groups/bind")
    def bind_group(body: dict = Body(...), request: Request = None):
        """把发现列表里的群接入（建 group_config，active；五业务 fail-closed 全关）。

        与群内「绑定」命令同口径：绑定只做接入，⛔ 不替管理员决定开哪些业务；
        后台手动建群（POST /groups）仍默认全开（§4.7 既有行为）。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        group_id = _clean_str(body.get("group_id"), "群标识", _MAX_GROUP_ID)
        group_name = _clean_str(body.get("group_name"), "群名称", _MAX_NAME,
                                required=False)
        if state.get_group_config(group_id) is not None:
            raise HTTPException(status_code=409, detail="群已绑定")
        state.create_group(group_id, group_name)
        state.upsert_group_config(
            group_id,
            biz_switches={k: False for k in state.BIZ_SWITCH_KEYS})
        _audit(state, request, identity, "group.bind", "group", group_id,
               {"group_name": group_name, "source": "admin_backend"})
        return {"ok": True, "group_id": group_id}

    # ---------- 应交人员（group_member_task_config） ----------

    @router.get("/groups/{group_id}/members")
    def list_members(group_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _group_row(state, group_id)
        members = [{"user_id": r["user_id"], "nickname": r["nickname"],
                    "role": r["role"], "enabled": bool(r["enabled"]),
                    "source": r["source"]}
                   for r in state.list_member_tasks(group_id)]
        return {"ok": True, "members": members}

    @router.post("/groups/{group_id}/members")
    def add_member(group_id: str, body: dict = Body(...),
                   request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        nickname = _clean_str(body.get("nickname"), "昵称", _MAX_NAME,
                              required=False)
        role = _clean_str(body.get("role"), "角色", _MAX_ROLE, required=False)
        enabled = body.get("enabled", True)
        state.upsert_member_task(group_id, user_id, nickname=nickname,
                                 role=role, enabled=bool(enabled))
        _audit(state, request, identity, "member.add", "member",
               "%s/%s" % (group_id, user_id),
               {"nickname": nickname, "role": role, "enabled": bool(enabled)})
        return {"ok": True}

    @router.put("/groups/{group_id}/members/{user_id}")
    def update_member(group_id: str, user_id: str, body: dict = Body(...),
                      request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        existing = [r for r in state.list_member_tasks(group_id)
                    if r["user_id"] == user_id]
        if not existing:
            raise HTTPException(status_code=404, detail="人员不在名单中")
        current = existing[0]
        nickname = _clean_str(body.get("nickname"), "昵称", _MAX_NAME,
                              required=False) if "nickname" in body \
            else current["nickname"]
        role = _clean_str(body.get("role"), "角色", _MAX_ROLE,
                          required=False) if "role" in body else current["role"]
        enabled = bool(body["enabled"]) if "enabled" in body \
            else bool(current["enabled"])
        # 2026-10-19 用户指令：群成员编辑可修改手机号（唯一标识）。
        # ⛔ 只改名单配置行（+ 跟着人走的导出授权），历史业务记录仍按旧手机号
        # 留在 MySQL，不会跟着改 —— 这一点必须让管理员知道。
        old_user_id = user_id
        if "new_user_id" in body:
            new_user_id = _clean_str(body.get("new_user_id"), "手机号码", 32)
            if new_user_id != user_id:
                if not re.match(r"^[0-9A-Za-z_-]{4,32}$", new_user_id):
                    raise HTTPException(
                        status_code=400,
                        detail="手机号码需为 4~32 位数字/字母")
                if any(r["user_id"] == new_user_id
                       for r in state.list_member_tasks(group_id)):
                    raise HTTPException(
                        status_code=409,
                        detail="该手机号码已在群成员名单中")
                state.rename_member_task(group_id, user_id, new_user_id)
                user_id = new_user_id
        state.upsert_member_task(group_id, user_id, nickname=nickname,
                                 role=role, enabled=enabled)
        _audit(state, request, identity, "member.update", "member",
               "%s/%s" % (group_id, user_id),
               {"nickname": nickname, "role": role, "enabled": enabled,
                "old_user_id": old_user_id, "new_user_id": user_id})
        return {"ok": True, "user_id": user_id}

    @router.delete("/groups/{group_id}/members/{user_id}")
    def remove_member(group_id: str, user_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        state.remove_member_task(group_id, user_id)
        # 2026-10-19 用户指令：白名单授权对象限群成员 —— 成员移除即级联
        # 删除其导出授权，⛔ 不留孤儿授权（remove_whitelist 幂等，无授权
        # 时静默）。
        state.remove_whitelist(group_id, user_id)
        _audit(state, request, identity, "member.remove", "member",
               "%s/%s" % (group_id, user_id),
               {"note": "停止未来任务；历史业务记录保留；导出授权级联失效"})
        return {"ok": True}

    # ---------- 导出白名单（2026-10-19 用户指令：单权化）----------
    # 权限只有 ADMIN_ALL 一种（一键 = 全部管理员操作权限）；授权对象限本群
    # 应交成员（group_member_task_config）——后台从群成员下拉选择，API 层
    # 同样校验，⛔ 非成员直调 API 也加不进来。成员被移除时其授权级联失效
    # （remove_member 内删 export_whitelist 行），⛔ 不留孤儿授权。

    @router.get("/groups/{group_id}/whitelist")
    def list_whitelist(group_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _group_row(state, group_id)
        entries = [{"user_id": r["user_id"],
                    "permission_type": r["permission_type"],
                    "nickname": _member_nickname(state, group_id,
                                                 r["user_id"]),
                    "updated_at": r["updated_at"]}
                   for r in state.list_whitelist(group_id)]
        return {"ok": True, "permissions": StateStore.WHITELIST_PERMISSIONS,
                "whitelist": entries}

    @router.post("/groups/{group_id}/whitelist")
    def add_whitelist(group_id: str, body: dict = Body(...),
                      request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        _require_member(state, group_id, user_id)
        state.upsert_whitelist(group_id, user_id)
        _audit(state, request, identity, "whitelist.add", "whitelist",
               "%s/%s" % (group_id, user_id), {"permission_type": "ADMIN_ALL"})
        return {"ok": True}

    @router.delete("/groups/{group_id}/whitelist")
    def remove_whitelist(group_id: str, body: dict = Body(...),
                         request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        state.remove_whitelist(group_id, user_id)
        _audit(state, request, identity, "whitelist.remove", "whitelist",
               "%s/%s" % (group_id, user_id), {"permission_type": "ADMIN_ALL"})
        return {"ok": True}

    # ---------- 报数字段池（§2.12 第 4 项） ----------

    @router.get("/groups/{group_id}/fields")
    def list_fields(group_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _group_row(state, group_id)
        fields = [{"field_id": r["field_id"], "field_name": r["field_name"],
                   "unit": r["unit"], "roles": r["roles"],
                   "enabled": bool(r["enabled"]),
                   "sort_order": r["sort_order"]}
                  for r in state.list_fields(group_id, enabled_only=False)]
        return {"ok": True, "fields": fields}

    @router.post("/groups/{group_id}/fields")
    def add_field(group_id: str, body: dict = Body(...),
                  request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        field_name = _clean_str(body.get("field_name"), "字段名", 128)
        unit = _clean_str(body.get("unit"), "单位", 32, required=False)
        roles = body.get("roles") or []
        if not isinstance(roles, list):
            raise HTTPException(status_code=400, detail="角色格式不正确")
        sort_order = body.get("sort_order", 0)
        if not isinstance(sort_order, int) or isinstance(sort_order, bool):
            raise HTTPException(status_code=400, detail="排序格式不正确")
        field_id = state.next_field_id(group_id)
        state.upsert_field(group_id, field_id, field_name, unit, roles, True,
                           sort_order)
        _audit(state, request, identity, "field.add", "field",
               "%s/%s" % (group_id, field_id), {"field_name": field_name})
        return {"ok": True, "field_id": field_id}

    @router.put("/groups/{group_id}/fields/{field_id}")
    def update_field(group_id: str, field_id: int, body: dict = Body(...),
                     request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        row = state.get_field(group_id, field_id)
        if row is None:
            raise HTTPException(status_code=404, detail="字段不存在")
        field_name = _clean_str(body.get("field_name"), "字段名", 128) \
            if "field_name" in body else row["field_name"]
        unit = _clean_str(body.get("unit"), "单位", 32, required=False) \
            if "unit" in body else row["unit"]
        roles = body["roles"] if "roles" in body else row["roles"]
        if isinstance(roles, str):
            import json as _json
            try:
                roles = _json.loads(roles)
            except ValueError:
                raise HTTPException(status_code=400, detail="roles 格式错误")
        if not isinstance(roles, list):
            raise HTTPException(status_code=400, detail="角色格式不正确")
        enabled = bool(body["enabled"]) if "enabled" in body \
            else bool(row["enabled"])
        sort_order = body["sort_order"] if "sort_order" in body \
            else row["sort_order"]
        if not isinstance(sort_order, int) or isinstance(sort_order, bool):
            raise HTTPException(status_code=400, detail="排序格式不正确")
        state.upsert_field(group_id, field_id, field_name, unit, roles,
                           enabled, sort_order)
        _audit(state, request, identity, "field.update", "field",
               "%s/%s" % (group_id, field_id),
               {"field_name": field_name, "enabled": enabled})
        return {"ok": True}

    @router.delete("/groups/{group_id}/fields/{field_id}")
    def remove_field(group_id: str, field_id: int, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        # 2026-10-20 用户指令：删除 = **物理删除该字段**（⛔ 不再是停用）。
        # 历史报数 ⛔ 不受影响：MySQL report_item 存 field_name/unit 快照，
        # 导出按快照出列；本表只决定「还能填哪些字段」。想保留只想暂停填报
        # 的，用 PUT enabled=false（前端「停用 / 启用」按钮）。
        row = state.get_field(group_id, field_id)
        if row is None:
            raise HTTPException(status_code=404, detail="字段不存在")
        state.delete_field(group_id, field_id)
        _audit(state, request, identity, "field.delete", "field",
               "%s/%s" % (group_id, field_id),
               {"field_name": row["field_name"],
                "note": "物理删除字段池行；历史报数按 MySQL 快照保留"})
        return {"ok": True, "message": "字段已删除（历史报数记录保留）"}

    # ---------- 审计 ----------

    @router.get("/audit")
    def audit(request: Request, limit: int = 100):
        identity = _identity(request, state)
        _require_password_changed(identity)
        if not (1 <= limit <= 500):
            limit = 100
        rows = [{"id": r["id"], "username": r["username"],
                 "action": r["action"], "target_type": r["target_type"],
                 "target_id": r["target_id"], "detail": r["detail"],
                 "result": r["result"], "ip": r["ip"],
                 "created_at": r["created_at"]}
                for r in state.list_admin_audit(limit)]
        return {"ok": True, "audit": rows}

    # ---------- 后台补录（用户指令第二/三/四阶段：管理员可补录） ----------

    def _backfill_ctx(group_id: str, user_id: str):
        from ..handlers.base import HandlerContext
        from .. import period as period_mod
        return HandlerContext(
            group_id=group_id, phone=user_id, name=None,
            platform_user_id=None, text="", state=state, backend=backend,
            config=config, now=period_mod.now())

    @router.post("/groups/{group_id}/backfill/checkin")
    def backfill_checkin(group_id: str, body: dict = Body(...),
                         request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        period_key = _resolve_period_from_body(state, group_id, body, "checkin")
        session = body.get("session")
        # 多时间段后：session 必须是本群已配置的区间 id（⛔ 不再是固定 am/pm）
        from ..handlers import checkin as checkin_handler
        ctx = _backfill_ctx(group_id, user_id)
        if checkin_handler.slot_by_id(ctx, session) is None:
            raise HTTPException(
                status_code=400,
                detail="时段必须是本群已配置的时间段标识（见群配置）")
        status = body.get("status", "ADMIN_EDIT")
        # 补录时间：管理员可指定（HH:MM[:SS]）+ 补录日期；不指定则由服务端记实际时刻
        checkin_time = _normalize_clock(body.get("checkin_time"),
                                        _body_date(body))
        result = checkin_handler.admin_backfill(ctx, period_key, session,
                                                status, checkin_time)
        _audit(state, request, identity, "backfill.checkin", "checkin",
               "%s/%s/%s" % (group_id, user_id, period_key),
               {"session": session, "status": status,
                "checkin_time": checkin_time, "kind": result.kind})
        if result.kind != "A":
            raise HTTPException(status_code=409,
                                detail=result.message or "补录被拒绝")
        return {"ok": True, "message": result.message,
                "record_id": result.record_id}

    @router.post("/groups/{group_id}/backfill/summary")
    def backfill_summary(group_id: str, body: dict = Body(...),
                         request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        period_type = body.get("period_type")
        if period_type not in ("daily", "weekly"):
            raise HTTPException(status_code=400,
                                detail="周期类型必须是 daily / weekly")
        # 2026-10-19：按日期补录；周期键按管理员选的日报/周报折算
        date_str = body.get("date")
        if isinstance(date_str, str) and date_str.strip():
            from ..period import parse_daily_key, period_key as _pk
            try:
                period_key = _pk(period_type,
                                 parse_daily_key(date_str.strip()))
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail="日期格式不正确，请选择有效日期")
        else:
            period_key = _clean_str(body.get("period_key"), "周期", 16)
        content = _clean_str(body.get("content"), "总结内容", 10000)
        from ..handlers import summary as summary_handler
        ctx = _backfill_ctx(group_id, user_id)
        result = summary_handler.admin_backfill(ctx, period_key, period_type,
                                                content)
        _audit(state, request, identity, "backfill.summary", "summary",
               "%s/%s/%s" % (group_id, user_id, period_key),
               {"period_type": period_type, "kind": result.kind})
        if result.kind != "A":
            raise HTTPException(status_code=409,
                                detail=result.message or "补录被拒绝")
        return {"ok": True, "message": "总结补录成功",
                "record_id": result.record_id}

    @router.post("/groups/{group_id}/backfill/report")
    def backfill_report(group_id: str, body: dict = Body(...),
                        request: Request = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _group_row(state, group_id)
        user_id = _clean_str(body.get("user_id"), "用户标识", 32)
        period_key = _resolve_period_from_body(state, group_id, body, "report")
        role = body.get("role")
        if not isinstance(role, str) or not role.strip():
            raise HTTPException(status_code=400, detail="角色不能为空")
        items = body.get("items")
        from ..handlers import report as report_handler
        ctx = _backfill_ctx(group_id, user_id)
        result = report_handler.admin_backfill(ctx, period_key, role, items)
        _audit(state, request, identity, "backfill.report", "report",
               "%s/%s/%s" % (group_id, user_id, period_key),
               {"role": role.strip(),
                "item_count": len(items) if isinstance(items, list) else 0,
                "kind": result.kind})
        if result.kind != "A":
            raise HTTPException(status_code=409,
                                detail=result.message or "补录被拒绝")
        return {"ok": True, "message": "报数补录成功",
                "record_id": result.record_id}

    # ---------- 管理员修改既有记录（D17，2026-10-19） ----------

    def _require_reason(body: dict) -> tuple:
        """修改事由必填 + 入库备注可选（用户指令：必填修改事由 + 入库备注）。"""
        reason = _clean_str(body.get("reason"), "修改事由", 200)
        remark = _clean_str(body.get("remark"), "入库备注", 500,
                            required=False)
        return reason, remark

    def _numeric_user_id(phone: str) -> int:
        """手机号 -> MySQL 数字人员 id；无档案时明确拒绝（⛔ 不隐式建档）。"""
        result = backend.user_get(phone=phone) or {}
        user = result.get("user") or {}
        if not user.get("id"):
            raise HTTPException(
                status_code=400,
                detail="该成员还没有业务档案，无法修改记录，请先补录一次")
        return int(user["id"])

    @router.post("/groups/{group_id}/records/{biz}/edit")
    def edit_record(group_id: str, biz: str, body: dict = Body(...),
                    request: Request = None):
        """修改已有业务记录（打卡时间/状态、总结正文/时间、报数各字段数值）。

        - ⛔ 只改已有行，不插入：没有记录时返回 409 并提示走补录
        - 必填修改事由（reason）+ 可选入库备注（remark），一并落审计
        - 保存后前端重新拉看板，✅ 不做本地乐观更新（以库为准）
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        if biz not in ("checkin", "summary", "report"):
            raise HTTPException(status_code=400,
                                detail="业务类型只能是 checkin / summary / report")
        _group_row(state, group_id)
        phone = _clean_str(body.get("user_id"), "用户标识", 32)
        reason, remark = _require_reason(body)
        period_key = _resolve_period_from_body(state, group_id, body, biz)
        date_str = _body_date(body)
        uid = _numeric_user_id(phone)

        changed = 0
        if biz == "checkin":
            slots = body.get("slots")
            if not isinstance(slots, list) or not slots:
                raise HTTPException(status_code=400,
                                    detail="请至少提供一个时间段的修改内容")
            for item in slots:
                if not isinstance(item, dict):
                    raise HTTPException(status_code=400,
                                        detail="时间段修改内容格式不正确")
                session = _clean_str(item.get("session"), "时段", 16)
                status = item.get("status")
                if status is not None and status not in (
                        "NORMAL", "LATE", "ADMIN_EDIT"):
                    raise HTTPException(
                        status_code=400,
                        detail="状态只能是 NORMAL / LATE / ADMIN_EDIT")
                result = backend.checkin_admin_update(
                    user_id=uid, group_id=group_id, period_key=period_key,
                    session=session,
                    checkin_time=_normalize_clock(item.get("time"), date_str),
                    status=status)
                if result.get("status") != "success":
                    raise HTTPException(
                        status_code=409,
                        detail=result.get("message") or "该时段没有可修改的记录")
                changed += 1

        elif biz == "summary":
            content = _clean_str(body.get("content"), "总结内容", 10000,
                                 required=False)
            submit_time = _normalize_clock(body.get("time"), date_str)
            if content is None and submit_time is None:
                raise HTTPException(status_code=400,
                                    detail="没有需要修改的内容")
            result = backend.summary_admin_update(
                user_id=uid, group_id=group_id, period_key=period_key,
                content=content, submit_time=submit_time)
            if result.get("status") != "success":
                raise HTTPException(
                    status_code=409,
                    detail=result.get("message") or "该用户本周期还没有总结记录")
            changed = 1

        else:
            items = body.get("items")
            submit_time = _normalize_clock(body.get("time"), date_str)
            payload_items = []
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        raise HTTPException(status_code=400,
                                            detail="报数字段修改内容格式不正确")
                    try:
                        quantity = int(item.get("quantity"))
                        if quantity < 0:
                            raise ValueError("negative")
                        payload_items.append({
                            "field_id": int(item.get("field_id")),
                            "quantity": quantity,
                        })
                    except (TypeError, ValueError):
                        raise HTTPException(
                            status_code=400, detail="报数必须是不小于 0 的整数")
            if not payload_items and submit_time is None:
                raise HTTPException(status_code=400,
                                    detail="没有需要修改的内容")
            result = backend.report_admin_update(
                user_id=uid, group_id=group_id, period_key=period_key,
                items=payload_items, submit_time=submit_time)
            if result.get("status") != "success":
                raise HTTPException(
                    status_code=409,
                    detail=result.get("message") or "该用户本周期还没有报数记录")
            changed = 1

        _audit(state, request, identity, "record.edit", biz,
               "%s/%s/%s" % (group_id, phone, period_key),
               {"reason": reason, "remark": remark, "changed": changed,
                "fields": body.get("slots") or body.get("items")
                or {"content": bool(body.get("content")),
                    "time": body.get("time")}})
        biz_label = {"checkin": "打卡", "summary": "总结",
                     "report": "报数"}[biz]
        return {"ok": True, "changed": changed,
                "message": "%s记录已更新并保存入库" % biz_label}

    @router.get("/uploads/{ref}")
    def read_upload(ref: str, request: Request):
        """读取上传图片（后台鉴权；文件在机器人数据目录，⛔ 不走静态目录）。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        from fastapi.responses import FileResponse
        from ..web import uploads as uploads_mod
        if not ref or len(ref) > 64:
            raise HTTPException(status_code=404, detail="图片不存在")
        row = state.get_upload(ref)
        if row is None:
            raise HTTPException(status_code=404, detail="图片不存在或已清理")
        path = uploads_mod.upload_abs_path(config.data_dir, row["rel_path"])
        if path is None:
            raise HTTPException(
                status_code=404, detail="图片已过保留期，无法查看")
        return FileResponse(str(path), media_type=row["mime"])

    @router.post("/groups/{group_id}/reminders/run")
    def run_reminder(group_id: str, body: dict = Body(default={}),
                     request: Request = None):
        """手动提醒（用户指令第五阶段 + 2026-10-16 三：按业务一键触发）。

        body.biz 可选：checkin / summary / report；缺省 = 三个业务全跑。

        与自动调度（reminder.tick）共用 run_group：同一套开关门控 /
        差集判定 / 5 分钟冷却 / 落库，⛔ 不搞第二套逻辑。
        manual=True：跳过「到点才判定」时间门控（打卡只取当前时段）。
        ⚠️ 推送端口未接通时如实记 unsent（⛔ 不伪造已发送）。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        group_row = _group_row(state, group_id)
        biz = body.get("biz") if isinstance(body, dict) else None
        if biz is not None and biz not in ("checkin", "summary", "report"):
            raise HTTPException(
                status_code=400,
                detail="业务只能是 checkin / summary / report")
        from ..handlers import reminder as reminder_handler
        from ..web.sender import platform_push
        # 阶段八：push 优先取装配注入的平台适配器口；测试替身等无 push_text
        # 的 sender 回落 fail-closed 桩（⛔ 不伪造已发送）
        push = getattr(sender, "push_text", None) or platform_push
        summary = reminder_handler.run_group(state, backend, push,
                                             group_row, _now(),
                                             biz=biz, manual=True)
        _audit(state, request, identity, "reminder.run", "reminder",
               group_id,
               {"biz": biz, "targets": summary["targets"],
                "new": summary["new"], "sent": summary["sent"],
                "unsent": summary["unsent"],
                "skipped": summary.get("skipped")})
        biz_label = {"checkin": "打卡", "summary": "总结",
                     "report": "报数"}.get(biz, "全部业务")
        if summary.get("skipped") == "group_disabled":
            return {"ok": True, "message": "该群已停用，未发送提醒",
                    "summary": summary}
        if summary.get("skipped") == "reminder_disabled":
            return {"ok": True, "message": "该群未开启提醒，未发送",
                    "summary": summary}
        if summary.get("skipped") == "checkin_all_done":
            return {"ok": True,
                    "message": "今日打卡时段已过，无需提醒（补交请走后台补录）",
                    "summary": summary}
        cooled = summary.get("skipped_cooled", 0)
        # ⭐ 2026-10-20（P0-3）：全员已达标的「空转」要说人话 ——
        # 旧回执是「已执行一轮全部业务提醒（新计划 0 条，发送 0 条）」，
        # 管理员看不出到底是没触发、还是都完成了、还是时段过了。
        if summary["sent"] == 0 and summary["new"] == 0 and cooled == 0:
            return {"ok": True,
                    "message": "当前没有需要提醒的未完成成员"
                               "（或今日打卡时段已结束）",
                    "summary": summary}
        suffix = "（%d 人处于 5 分钟冷却内，未重复打扰）" % cooled if cooled else ""
        return {"ok": True,
                "message": "已执行一轮%s提醒（新计划 %d 条，发送 %d 条）%s"
                           % (biz_label, summary["new"], summary["sent"],
                              suffix),
                "summary": summary}

    # ---------- 仪表盘聚合（D3-C：只读；不当成功也不当失败） ----------

    @router.get("/dashboard")
    def dashboard(request: Request, group_id: Optional[str] = None):
        identity = _identity(request, state)
        _require_password_changed(identity)
        return build_dashboard(state, backend, group_id=group_id)

    # ---------- 成员级业务看板（2026-10-19：一天 × 全员 × 三业务） ----------

    @router.get("/groups/{group_id}/board")
    def group_board(group_id: str, request: Request,
                    date: Optional[str] = None):
        """群看板（纯读）。date 缺省 = 今天；格式错 400，群不存在 404。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        if state.get_group_config(group_id) is None:
            raise HTTPException(status_code=404, detail="群不存在")
        from ..period import daily_key, parse_daily_key
        date_str = (date or "").strip() or daily_key()
        try:
            parse_daily_key(date_str)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="日期格式不正确，请选择有效日期")
        return board_mod.build_board(state, backend, group_id, date_str)

    # ---------- 统计导出（D3-C；⭐ 2026-10-20 起统一 XLSX；纯读，失败不出空文件） ----------

    @router.get("/stats/export")
    def stats_export(request: Request, group_id: str, date: str,
                     biz: Optional[str] = None, scope: str = "single",
                     format: str = "xlsx", remark: Optional[str] = None):
        """统计导出（⭐ 2026-10-20 用户指令：**统一 XLSX，CSV 已下线**）。

        理由：群内导出也是 xlsx（量子密信电脑端打不开 CSV），前后台口径要一致。
        format 只接受 xlsx，传 csv 明确 400 —— ⛔ 不静默降级成别的格式冒充。
        scope=all（一天×全员×三业务汇总单表）/ single（单业务清单）两种口径
        都走 xlsx；remark 只落审计，⛔ 不进文件内容。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        if scope not in ("single", "all"):
            raise HTTPException(status_code=400, detail="导出范围不正确")
        if format != "xlsx":
            raise HTTPException(status_code=400,
                                detail="目前仅支持 XLSX 导出（CSV 导出能力已下线）")
        row = state.get_group_config(group_id)
        if row is None:
            raise HTTPException(status_code=404, detail="群不存在")
        group_name = (row["group_name"] if "group_name" in row.keys()
                      else group_id)
        period_type = (row["period_type"] if "period_type" in row.keys()
                       else "daily")

        if scope == "all":
            from ..period import parse_daily_key
            try:
                parse_daily_key(date)
            except ValueError:
                raise HTTPException(status_code=400,
                                    detail="日期格式不正确，请选择有效日期")
            board = board_mod.build_board(state, backend, group_id, date)
            if any(board.get("unknown", {}).values()):
                # ⛔ 不出半张空表冒充成功：查不到就明确说稍后重试
                raise HTTPException(status_code=503,
                                    detail="数据暂时查不到，请稍后重试")
            headers, rows = board_mod.board_rows(board)
            content = stats_mod.multi_sheet_xlsx([("统计汇总", headers, rows)])
            media = _XLSX_MEDIA
            filename = stats_mod.all_export_filename(group_name, date, format)
            _audit(state, request, identity, "stats.export", "group",
                   group_id, {"scope": "all", "format": format,
                              "date": date, "remark": remark})
            return Response(content=content, media_type=media, headers={
                "Content-Disposition":
                    "attachment; filename*=UTF-8''%s" % quote(filename)})

        if biz not in ("checkin", "summary", "report"):
            raise HTTPException(status_code=400,
                                detail="请选择要导出的业务")
        try:
            period_k = stats_mod.resolve_period_key(state, group_id, biz,
                                                    date)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="日期格式不正确，请选择有效日期")
        try:
            records = stats_mod.query_records(backend, biz, group_id,
                                              period_k)
        except Exception:
            raise HTTPException(status_code=503,
                                detail="数据暂时查不到，请稍后重试")
        filename = stats_mod.export_filename(group_name, biz, period_k,
                                             format)
        # ⭐ 2026-10-20（P1-2）：单业务导出带上本群时段名（自定义时段不再显示成「下午」）
        slot_labels = stats_mod.slot_labels_for(state, group_id)
        content = stats_mod.to_xlsx(biz, records, period_type, slot_labels)
        media = _XLSX_MEDIA
        _audit(state, request, identity, "stats.export", "group", group_id,
               {"scope": "single", "biz": biz, "format": format,
                "date": date, "remark": remark})
        return Response(content=content, media_type=media, headers={
            "Content-Disposition":
                "attachment; filename*=UTF-8''%s" % quote(filename)
        })

    # ---------- 量子机器人实例（v6，D3-B1；三门纪律：实例状态⛔ 不是业务授权） ----------

    @router.get("/robot-instances")
    def list_robot_instances(request: Request,
                             status: Optional[str] = None):
        """实例列表（可按 WAIT_BIND/ACTIVE/DISABLED 过滤；⛔ 无手工预建）。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        if status is not None and \
                status not in state.ROBOT_INSTANCE_STATUSES:
            raise HTTPException(status_code=400, detail="非法实例状态过滤值")
        instances = [_instance_payload(r)
                     for r in state.list_robot_instances(status)]
        return {"ok": True, "instances": instances}

    @router.get("/robot-instances/{group_id}")
    def get_robot_instance(group_id: str, request: Request):
        identity = _identity(request, state)
        _require_password_changed(identity)
        return {"ok": True,
                "instance": _instance_payload(_instance_row(state, group_id))}

    @router.post("/robot-instances/{group_id}/bind")
    def bind_robot_instance(group_id: str, body: dict = Body(...),
                            request: Request = None):
        """T2 绑定激活：WAIT_BIND -> ACTIVE（guard = 状态必须是 WAIT_BIND）。

        ⛔ V1 拒收 webhook_key：平台签名机制未确认（D3-A 问题清单 Q8），
        加密层（AES-256-GCM + env KEK）未实现未批准 —— fail-closed 拒写，
        ⛔ 绝不明文落库；批准后随加密层一同开放。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        row = _instance_row(state, group_id)
        if row["status"] == "ACTIVE":
            raise HTTPException(status_code=409,
                                detail="实例已绑定（更新请用 PUT）")
        if body.get("webhook_key") is not None:
            raise HTTPException(
                status_code=400,
                detail="平台签名机制未确认，暂不支持 webhook_key"
                       "（fail-closed：绝不明文落库）")
        url = body.get("webhook_base_url")
        display_name = _clean_str(body.get("display_name"), "群备注名",
                                  _MAX_NAME, required=False)
        try:
            ok = state.bind_robot_instance(group_id, url,
                                           display_name=display_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            # 竞态：校验后行被并发改状态（禁用/再绑定）-> 明确 409，⛔ 不伪装成功
            raise HTTPException(status_code=409,
                                detail="实例状态已变化，请刷新后重试")
        if isinstance(url, str) and url.strip().lower().startswith("http:"):
            LOG.warning("robot instance %s bound with plaintext http"
                        " webhook (test-only, D3-B0 §1.1)", group_id)
        _audit(state, request, identity, "robot_instance.bind",
               "robot_instance", group_id,
               _audit_safe({"display_name": display_name,
                            "webhook_scheme": (url or "").split(":", 1)[0],
                            "source": "admin_backend"}))
        return {"ok": True, "instance": _instance_payload(
            state.get_robot_instance(group_id))}

    @router.put("/robot-instances/{group_id}")
    def update_robot_instance(group_id: str, body: dict = Body(...),
                              request: Request = None):
        """局部更新（备注名 / webhook 基址）。⛔ 禁止清空 webhook_base_url
        （传空串 -> 400）；webhook_key 同样拒收（见 bind 说明）。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _instance_row(state, group_id)
        if body.get("webhook_key") is not None:
            raise HTTPException(
                status_code=400,
                detail="平台签名机制未确认，暂不支持 webhook_key")
        display_name = (_clean_str(body.get("display_name"), "群备注名",
                                   _MAX_NAME, required=False)
                        if "display_name" in body else None)
        url = body.get("webhook_base_url") \
            if "webhook_base_url" in body else None
        try:
            ok = state.update_robot_instance(
                group_id, display_name=display_name,
                webhook_base_url=url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not ok:
            raise HTTPException(status_code=404, detail="机器人实例不存在")
        _audit(state, request, identity, "robot_instance.update",
               "robot_instance", group_id,
               _audit_safe({"display_name": display_name,
                            "webhook_updated": url is not None}))
        return {"ok": True, "instance": _instance_payload(
            state.get_robot_instance(group_id))}

    @router.post("/robot-instances/{group_id}/disable")
    def disable_robot_instance(group_id: str, request: Request = None):
        """T3 禁用（任意状态，幂等）。⚠️ 只影响将来 push/file 出站能否使用
        已登记 webhook；⛔ 不阻断 reply 回推、⛔ 不改变群业务授权。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _instance_row(state, group_id)
        state.set_robot_instance_status(group_id, "DISABLED")
        _audit(state, request, identity, "robot_instance.disable",
               "robot_instance", group_id, None)
        return {"ok": True, "instance": _instance_payload(
            state.get_robot_instance(group_id))}

    @router.post("/robot-instances/{group_id}/enable")
    def enable_robot_instance(group_id: str, request: Request = None):
        """T4 启用（guard = webhook_base_url 已设置，否则 400）。"""
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        _instance_row(state, group_id)
        if not state.set_robot_instance_status(group_id, "ACTIVE"):
            raise HTTPException(status_code=400,
                                detail="启用前必须先绑定 webhook_base_url")
        _audit(state, request, identity, "robot_instance.enable",
               "robot_instance", group_id, None)
        return {"ok": True, "instance": _instance_payload(
            state.get_robot_instance(group_id))}

    @router.delete("/robot-instances/{group_id}")
    def remove_robot_instance(group_id: str, request: Request = None):
        """移除群聊（2026-10-19 新增，照参考后台）。

        语义（用户当面选定）：实例 + 群配置 + 应交名单 + 导出授权 +
        报数字段池 **全删**；MySQL 侧业务历史记录 ⛔ 不动（历史可查）。
        group_seen 保留 —— 群里再 @ 一次可按 WAIT_BIND 重新接入。
        幂等口径：群不存在返回 404（⛔ 不伪装成功）。
        """
        identity = _identity(request, state)
        _require_password_changed(identity)
        _require_csrf(request, identity)
        if state.get_group_config(group_id) is None:
            raise HTTPException(status_code=404, detail="群聊不存在或已移除")
        removed = state.delete_group_cascade(group_id)
        _audit(state, request, identity, "robot_instance.remove",
               "robot_instance", group_id, removed)
        return {"ok": True, "message": "群聊已移除", "removed": removed}

    return router


def _config_payload(state: StateStore, row) -> dict:
    """群配置行 -> API 载荷（开关归一化 + 结构化打卡时间段 / 提醒规则）。"""
    import json as _json
    from ..handlers import checkin as _checkin
    from ..handlers import reminder as _reminder
    try:
        deadlines = _json.loads(row["checkin_deadlines"] or "{}")
    except ValueError:
        deadlines = {}
    return {
        "group_id": row["group_id"],
        "group_name": row["group_name"],
        "status": row["status"] if "status" in row.keys() else "active",
        "switches": state.normalized_switches(row),
        "period_type": row["period_type"],
        "daily_cutoff": row["daily_cutoff"],
        "checkin_deadlines": deadlines,
        # 多时间段（验收项 2）：规范化区间列表，SPA 直接渲染
        "checkin_slots": _checkin.slots_from_row(row),
        "checkin_require_image": bool(row["checkin_require_image"]),
        # 提醒规则按业务独立配置（验收项 4）：enabled/times/text
        "reminder_rules": _reminder.normalize_reminder_rules(
            row["reminder_rules"]),
    }


# 审计 detail 脱敏黑名单（D3-B1 硬约束）：webhook 凭证⛔ 不得落入审计 detail
# —— 当前 V1 绑定 API 已拒收 webhook_key（fail-closed），此处为纵深防御：
# 将来任何端点若把凭证键写进 detail，一律在此被剔除，⛔ 不静默透传。
_AUDIT_REDACT_KEYS = ("webhook_key", "webhook_key_encrypted")


def _audit_safe(fields: dict) -> dict:
    """审计 detail 脱敏过滤（红黑名单：敏感键一律剔除）。"""
    return {k: v for k, v in fields.items()
            if k not in _AUDIT_REDACT_KEYS}


def build_dashboard(state: StateStore, backend: Backend,
                    group_id: Optional[str] = None) -> dict:
    """仪表盘聚合（纯读：SQLite 群/实例/提醒 + MCP 只读记录查询）。

    两种模式：
    - group_id=None：全量聚合（历史契约，既有 API 测试锁定此形态）
    - group_id 指定：按群独立展示——仪表盘不展示总数，仅当前选中群数据。
      群不存在 -> {"ok": False, "error": "群不存在"}

    口径（不猜、不四舍五入）：
    - 分母 = 该群应交人员（group_member_task_config enabled=1）人数；
      分子 = 当日该业务有记录的去重手机号数（有记录即完成，不按时段折算）
    - 停用群不计入全量分母（按未绑定处理，与回调抑制同口径）
    - 任一业务的 MCP 查询失败 -> 该业务 unknown=True（不当 0 也不当成功）
    - 待提醒人数 = 应交人数 − 当日任一开启业务有记录的去重手机号数
    - 页面与 API 共用本函数（同一口径，不两套实现）
    """
    from datetime import datetime as _dt

    from ..period import TZ

    today = _dt.now(TZ).date().isoformat()
    queries = {"checkin": backend.checkin_query_records,
               "summary": backend.summary_query_records,
               "report": backend.report_query_records}
    time_field = {"checkin": "checkin_time", "summary": "updated_time",
                  "report": "created_time"}

    def _recent_entry(name, gid, record):
        return {
            "biz": name,
            "group_id": gid,
            "phone": record.get("phone"),
            "name": record.get("name"),
            "at": record.get(time_field[name]) or "",
            "entry_source": record.get("entry_source"),
            "status": record.get("status"),
        }

    # ---------- 按群模式（D3-C 用户指令：仅当前选中群数据） ----------

    if group_id is not None:
        row = state.get_group_config(group_id)
        if row is None:
            return {"ok": False, "error": "群不存在"}
        switches = state.normalized_switches(row)
        try:
            members = len(list(state.list_member_tasks(group_id,
                                                       enabled_only=True)))
        except Exception:  # 名单读取异常不当 0 人（分母缺失即不可比）
            members = 0
        biz = {}
        recent: list = []
        phones_any: set = set()
        enabled_any = False
        for name, query in queries.items():
            enabled = bool(switches.get(name))
            enabled_any = enabled_any or enabled
            entry = {"done": 0, "total": members, "unknown": False,
                     "enabled": enabled}
            try:
                records = (query(group_id=group_id,
                                 period_key=today).get("records") or [])
            except Exception:
                entry["unknown"] = True
                records = []
            phones = {r.get("phone") for r in records if r.get("phone")}
            entry["done"] = len(phones)
            if enabled:
                phones_any |= phones
            for record in records:
                recent.append(_recent_entry(name, group_id, record))
            biz[name] = entry
        recent.sort(key=lambda item: item["at"] or "", reverse=True)
        instance = None
        for inst in state.list_robot_instances():
            if inst["group_id"] == group_id:
                instance = {"status": inst["status"],
                            "display_name": inst["display_name"]}
                break
        recovery_pending = sum(
            1 for task in state.pending_recovery_tasks()
            if (task["scope_key"] or "").split("|")[0] == group_id)
        pending_people = (max(members - len(phones_any), 0)
                          if enabled_any else 0)
        return {
            "ok": True,
            "mode": "group",
            "period_key": today,
            "group": {
                "group_id": group_id,
                "group_name": (row["group_name"]
                               if "group_name" in row.keys() else None),
                "status": (row["status"] if "status" in row.keys()
                           else "active"),
            },
            "biz": biz,
            "reminders": {"pending": state.count_pending_reminders(group_id)},
            "recovery": {"pending": recovery_pending},
            "pending_people": pending_people,
            "instance": instance,
            "recent": recent[:10],
        }

    # ---------- 全量模式（历史契约，形态不变） ----------

    groups = list(state.list_groups())
    instances = list(state.list_robot_instances())
    biz = {name: {"done": 0, "total": 0, "unknown": False}
           for name in ("checkin", "summary", "report")}
    recent = []
    for group in groups:
        if (group["status"] or "active") != "active":
            continue
        gid = group["group_id"]
        try:
            members = len(list(state.list_member_tasks(gid,
                                                       enabled_only=True)))
        except Exception:  # 名单读取异常不当 0 人（分母缺失即不可比）
            members = 0
        for name, query in queries.items():
            biz[name]["total"] += members
            try:
                records = (query(group_id=gid,
                                 period_key=today).get("records") or [])
            except Exception:
                biz[name]["unknown"] = True
                continue
            phones = {r.get("phone") for r in records if r.get("phone")}
            biz[name]["done"] += len(phones)
            for record in records:
                recent.append(_recent_entry(name, gid, record))
    recent.sort(key=lambda item: item["at"] or "", reverse=True)
    status_counts = {"WAIT_BIND": 0, "ACTIVE": 0, "DISABLED": 0}
    for inst in instances:
        if inst["status"] in status_counts:
            status_counts[inst["status"]] += 1
    active_groups = sum(1 for group in groups
                        if (group["status"] or "active") == "active")
    return {
        "ok": True,
        "mode": "all",
        "period_key": today,
        "groups": {"total": len(groups), "active": active_groups,
                   "disabled": len(groups) - active_groups},
        "instances": status_counts,
        "biz": biz,
        "reminders": {"pending": state.count_pending_reminders()},
        "recovery": {"pending": len(list(state.pending_recovery_tasks()))},
        "recent": recent[:10],
    }
