# -*- coding: utf-8 -*-
""":18002 业务服务（群回调 + H5 同源，§2.9 / §3.1 / §3.2）。

路由总览：
- POST /api/callback            平台群消息回调（§3.2 通用骨架）
- GET  /h5/checkin              打卡 H5（令牌经 URL fragment，⛔ 不进 query）
- GET  /h5/report               报数 H5（字段池动态逐行添加，2026-10-16 去角色化）
- POST /api/h5/checkin/info     页面初始化（只本地校验令牌）
- POST /api/h5/checkin/upload   图片上传（multipart；§4.11.4 D4 定夺：
  方案 A 机器人中转 + 服务器磁盘，2026-10-16 落地）
- POST /api/h5/checkin/submit   打卡提交（A/B/C 归类）
- POST /api/h5/report/info      页面初始化（令牌 + 字段池；⛔ 不再返回角色）
- POST /api/h5/report/submit    报数提交（A/B/C 归类；⛔ 不再收客户端角色）
- GET  /healthz                 存活探针

顶层消息路由顺序（⭐ 2026-10-19 用户指定，⛔ 不得调整）：

  0) 归一化 + 回调发现记账 + 机器人实例登记（只记账，⛔ 不回复、不是授权）
  1) 是否 @机器人？—— 否 -> 静默不处理；是 -> 下一步
     ⚠️ 容错：平台载荷**没有** @ 字段时（2026-10-19 实测形态）一律放行，
        ⛔ 不能拿不存在的字段做拦截（否则全群静默）。见 `mention_state`。
  2) 指令是否精确等于「绑定」？—— 是 -> 第 1 轮绑定（鉴权 + 挂等待会话）；
     否 -> 下一步
  3) 当前群 + 发送者是否存在待输入群名的绑定会话？—— 是 -> 第 2 轮
     （群名接收 / 取消 / 超时 / 二次鉴权 / 落库）；否 -> 下一步
  4) 群是否已绑定且已启用？—— 否 -> **回固定引导话术**（未绑定=请管理员
     发「@机器人 绑定」；已停用=请后台启用）。⭐ 2026-10-19 起不再静默。
     是 -> 下一步
  5) 已绑定群：① 实名待输入会话（优先）② 暂停考核拦截 ③ 业务命令分派
     （打卡 / 总结 / 报数 / 导出 / 实名 / 两轮总结 / 帮助文案）

⛔ 为什么第 3 步必须早于第 4 步：第二轮发的群名来自「尚未完成绑定」的群，
   一旦先做群授权判定，第二轮会被当成未绑定群的普通消息**静默吞掉**，
   用户输完群名却零反馈 —— 这是 2026-10-19 用户点出的顺序缺陷。

⭐ 2026-10-17 用户指令一：绑定入口**只识别精确「绑定」二字**，
⛔ 「绑定: 群名」一步到位路径已移除（群名一律等第二轮纯文本）。

⚠️ 未实测项（§7.2）：平台回调来源真实性验证（D10）、回推域名与载荷（D11）。
⛔ 未验证前不得宣称「平台联调通过」。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .. import commands
from .. import normalize as normalize_mod
from .. import tokens as tokens_mod
from ..backend.base import Backend
from ..config import BotConfig
from ..handlers import bind as bind_handler
from ..handlers import checkin as checkin_handler
from ..handlers import realname as realname_handler
from ..handlers import report as report_handler
from ..handlers import stats as stats_handler
from ..handlers import summary as summary_handler
from ..handlers.base import (KIND_A, PAUSED_MEMBER_TEXT, HandlerContext,
                             token_record_from_row)
from ..router import Router
from ..state import StateStore
from ..web import uploads as uploads_mod
from ..web.payload_probe import shape_line
from ..web.platform_adapter import adapt as adapt_platform_payload
from ..web.sender import ReplySender

LOG = logging.getLogger("group-ops.web")

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "h5templates"

TOKEN_NOT_FOUND_MESSAGE = ("链接无效或已被作废，"
                           "请返回群聊重新发送命令获取新链接")

# ---------- 第 1 步的 @ 判定（容错放行） ----------

# 显式布尔型 @ 标记（平台若提供，按它判）
_MENTION_BOOL_KEYS = ("isMentioned", "is_mentioned", "isAt", "atMe",
                      "mentioned")
# 名单型 @ 标记（非空 = 有 @；显式空数组 = 明确没 @）
_MENTION_LIST_KEYS = ("mentionedMobileList", "mentionedList", "atList",
                      "atPhones", "mentionList")
# @ 标记可能嵌在这些对象里（与实测 textMsg 结构同层思路）
_MENTION_BOXES = ("textMsg", "text_msg", "message", "data")


def mention_state(raw) -> Optional[bool]:
    """本条消息是否 @ 了机器人：True / False / None（载荷没说）。

    ⚠️ **实测依据（2026-10-19 09:36–09:39 探针日志 `payload_shape`）**：
    量子密信入站载荷只有
    ``callBackMethod, callBackUrl, groupId, groupName, phone, robotId,
    tenantId, textMsg{content,tenantId}, type, userName``
    —— **没有任何 @ 字段**。因此当前恒返回 None。

    ⛔ 纪律：None **必须放行**（见调用点）。拿一个不存在的字段做拦截，
    等于把所有消息静默掉；这正是「未观测 ≠ 不存在」的反面。
    平台日后补上 @ 字段时，本函数自动生效（显式 False 才静默）。
    """
    if not isinstance(raw, dict):
        return None
    nodes = [raw]
    for box in _MENTION_BOXES:
        node = raw.get(box)
        if isinstance(node, dict):
            nodes.append(node)
    for node in nodes:
        for key in _MENTION_BOOL_KEYS:
            value = node.get(key)
            if isinstance(value, bool):
                return value
        for key in _MENTION_LIST_KEYS:
            value = node.get(key)
            if isinstance(value, list):
                return len(value) > 0
    return None


def build_router() -> Router:
    """装配四个入口命令的处理器（§4.1：固定路由，无动态路由）。"""
    router = Router()
    router.register(commands.CMD_CHECKIN, checkin_handler.handle_command)
    router.register(commands.CMD_SUMMARY, summary_handler.handle_command)
    router.register(commands.CMD_REPORT, report_handler.handle_command)
    # 「导出」= 一键导出全部业务（2026-10-16 用户指令三.1）
    router.register(commands.CMD_EXPORT, stats_handler.handle_command)
    # 「实名」= 两轮实名登记（2026-10-19 用户指令）
    router.register(commands.CMD_REALNAME, realname_handler.handle_command)
    return router


def create_app(*, state: StateStore, backend: Backend, sender: ReplySender,
               config: BotConfig, router: Optional[Router] = None,
               now_fn=None) -> FastAPI:
    """应用工厂（依赖全部显式注入，便于测试替身，§2.8）。"""
    app = FastAPI(title="group-ops-bot", docs_url=None, redoc_url=None)
    app.state.store = state
    router = router or build_router()

    # ---------- 内部 helpers ----------

    def _now_dt() -> datetime:
        from .. import period as period_mod
        return now_fn() if now_fn else period_mod.now()

    def _make_ctx(group_id: str, phone: str, name: Optional[str],
                  platform_user_id: Optional[str], text: str,
                  reply_url: str = "", raw: Optional[dict] = None
                  ) -> HandlerContext:
        raw = raw or {}
        # 平台真实群名：2026-10-15 实测载荷字段 `groupName`（如「量子密信群机器人
        # 开发沟通群」）。⛔ 不再用发送者昵称冒充群名 —— 绑定后后台显示管理员名字
        # 就是这个 bug。⛔ 非字符串 / 空白一律当未提供（不猜）。
        raw_group_name = raw.get("groupName")
        group_name = (raw_group_name.strip()
                      if isinstance(raw_group_name, str)
                      and raw_group_name.strip() else None)
        return HandlerContext(
            group_id=group_id, phone=phone, name=name,
            platform_user_id=platform_user_id, text=text, state=state,
            backend=backend, config=config, now=_now_dt(),
            reply_url=reply_url, raw=raw, sender=sender,
            group_name=group_name)

    def _ctx_from_token(token_value: str):
        """H5 端点专用：令牌即身份凭证（§2.4），群/用户从令牌绑定还原。"""
        if not token_value:
            return None, None
        row = state.get_token(tokens_mod.token_id_of(token_value))
        record = token_record_from_row(row)
        if record is None:
            return None, None
        ctx = _make_ctx(record.group_id, record.phone, None, None, "")
        return ctx, record

    def _token_of(body: dict) -> str:
        value = body.get("token")
        return value.strip() if isinstance(value, str) else ""

    def _token_str(value) -> str:
        """表单字段版令牌提取（multipart 上传用；同 _token_of 口径）。"""
        return value.strip() if isinstance(value, str) else ""

    # ---------- H5 链接身份绑定（2026-10-19 D17 用户指令） ----------
    # 令牌本身绑定手机号，但链接是在群里发的 —— 谁都能点开。要求输入
    # 「本人手机号后 4 位」才放行上传/提交：其他人打开看到明确提醒且被拒。
    # ⛔ 后端强制（不是只做前端提示），不匹配一律 403 + 安全留痕。

    def _holder_name(group_id: str, phone: str) -> str:
        try:
            for item in state.list_member_tasks(group_id):
                if str(item["user_id"]) == str(phone):
                    return str(item["nickname"] or "")
        except Exception:  # pragma: no cover - 只影响展示
            return ""
        return ""

    def _holder_ok(record, provided) -> bool:
        tail = str(getattr(record, "phone", "") or "")[-4:]
        text = provided.strip() if isinstance(provided, str) else ""
        return bool(tail) and text == tail

    def _holder_reject(record, action: str):
        name = _holder_name(record.group_id, record.phone) or "该成员"
        state.record_security_event(
            "holder_mismatch", record.group_id, record.phone,
            "h5 %s identity mismatch" % action)
        return JSONResponse(status_code=403, content={
            "ok": False, "reason": "holder_mismatch",
            "message": "此链接为「%s」的专属链接，仅本人可%s。"
                       "请填写本人手机号后 4 位验证身份。" % (name, action),
            "holder_name": name})

    # 注：令牌拒绝的安全留痕在处理器内统一落（覆盖 info + submit 两条路径），
    #     ⛔ 这里不再记一份，否则同一拒绝会重复两条 security_event。
    # ---------- 探针 ----------

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---------- 平台回调（§3.2） ----------

    @app.post("/api/callback")
    async def callback(request: Request):
        try:
            payload = await request.json()
        except Exception:
            state.record_security_event("payload_error", None, None,
                                        "body is not valid JSON")
            raise HTTPException(status_code=400, detail="invalid json body")
        if not isinstance(payload, dict):
            state.record_security_event("payload_error", None, None,
                                        "body is not a JSON object")
            raise HTTPException(status_code=400, detail="invalid json body")
        # 观测探针（⚠️ 临时：Q3 入站字段名对齐用；⛔ 只记字段结构、不记值、
        # ⛔ 不改变下面任何判定与响应码；协议确认后连同 payload_probe.py 一并退役）
        LOG.info(shape_line(payload))
        try:
            # 平台字段别名适配（⚠️ 2026-10-15 实测：平台用 callBackUrl / textMsg.content
            # / userName，normalize.py 的假想协议候选键未覆盖；⛔ 差异隔离在适配层，
            # ⛔ normalize.py 一行未改；适配层⛔ 不猜不补，取不到仍由它如实报缺失）
            inbound = normalize_mod.normalize_payload(adapt_platform_payload(payload))
        except normalize_mod.PayloadError as exc:
            state.record_security_event("payload_error", None, None,
                                        "%s | %s" % (str(exc)[:200],
                                                      shape_line(payload)))
            raise HTTPException(status_code=400, detail="invalid payload")

        # ===================================================================
        # 顶层消息路由（⭐ 2026-10-19 用户指定顺序，⛔ 不得调换）
        # ===================================================================

        # 第 0 步：观测层记账（发现未绑定群 + 机器人实例）。
        # ⛔ 只记账：不回复、不是授权（授权判定只看 group_config，见第 4 步）。
        # 放在 @ 判定之前是有意的——即便消息没 @ 机器人，也说明该群里有机器人。
        state.note_group_seen(inbound.group_id, inbound.phone, inbound.name)
        try:
            state.ensure_robot_instance(inbound.group_id)
        except Exception:
            LOG.exception("ensure_robot_instance failed")

        # 第 1 步：是否 @机器人？—— 否 -> 静默不处理。
        # ⚠️ 载荷没有 @ 字段时 mention_state 返回 None -> **放行**
        #    （2026-10-19 实测平台载荷无 @ 字段，⛔ 不能拿不存在的字段拦截）
        mentioned = mention_state(inbound.raw)
        if mentioned is False:
            LOG.info("callback ignored: bot not mentioned")
            return {"ok": True, "replied": False, "ignored": "not_mentioned"}

        parsed = commands.parse(inbound.text)

        # 第 2 步：指令是否精确等于「绑定」？—— 是 -> 第 1 轮
        # （鉴权 fail-closed 在处理器内；本步只提示输入群名 + 挂等待会话）
        if parsed.command == commands.CMD_BIND:
            ctx = _make_ctx(inbound.group_id, inbound.phone, inbound.name,
                            inbound.platform_user_id, inbound.text,
                            inbound.reply_url, inbound.raw)
            reply = bind_handler.handle_command(parsed, ctx)
            if reply and inbound.reply_url:
                sender.reply(inbound.reply_url, reply,
                             mention_phones=[inbound.phone],
                             group_id=inbound.group_id)
            return {"ok": True, "replied": bool(reply)}

        # 第 3 步：当前群 + 发送者是否有「待输入群名」的绑定会话？—— 是 -> 第 2 轮
        # ⛔ 必须早于第 4 步：待绑群此刻还没有授权，若先做群授权判定，
        #    第二轮群名会被「未绑定群静默」吞掉，用户输完群名零反馈。
        bind_pending_reply = bind_handler.handle_pending_text_if_any(
            _make_ctx(inbound.group_id, inbound.phone, inbound.name,
                      inbound.platform_user_id, inbound.text,
                      inbound.reply_url, inbound.raw))
        if bind_pending_reply is not None:
            if bind_pending_reply and inbound.reply_url:
                sender.reply(inbound.reply_url, bind_pending_reply,
                             mention_phones=[inbound.phone],
                             group_id=inbound.group_id)
            return {"ok": True, "replied": bool(bind_pending_reply)}

        # 第 4 步：群绑定状态检查（⭐ 2026-10-19 用户指令：⛔ 不再静默）
        # - 未绑定（无群配置）-> 统一引导管理员发「@机器人 绑定」
        # - 已绑定但停用 -> 单独话术（⛔ 不能让用户误以为绑定就能救回）
        # 两者都先落安全留痕，再回话；回推仍真实 @ 发送者。
        if not state.group_active(inbound.group_id):
            state.record_security_event(
                "unauthorized_group", inbound.group_id, inbound.phone,
                "group not bound or disabled")
            bound_row = state.get_group_config(inbound.group_id)
            reply = (bind_handler.UNBOUND_TEXT if bound_row is None
                     else bind_handler.DISABLED_GROUP_TEXT)
            if inbound.reply_url:
                sender.reply(inbound.reply_url, reply,
                             mention_phones=[inbound.phone],
                             group_id=inbound.group_id)
            return {"ok": True, "replied": True,
                    "reason": "unbound" if bound_row is None else "disabled"}

        # 第 5 步：已绑定群的业务分支
        # 入站学习（§2.7 组合方案 C）
        state.learn_user(inbound.phone, inbound.name,
                         inbound.platform_user_id)

        ctx = _make_ctx(inbound.group_id, inbound.phone, inbound.name,
                        inbound.platform_user_id, inbound.text,
                        inbound.reply_url, inbound.raw)
        reply: Optional[str] = None
        try:
            # ---- 5.1 多轮会话：实名优先于总结（⛔ 两条会话不互相吞）----
            if realname_handler.has_pending(ctx):
                reply = realname_handler.handle_pending_text(ctx)
            else:
                # ---- 5.2 暂停考核拦截（2026-10-19 用户口径）----
                # 暂停 = 不能在群内执行业务操作（⛔ 不只是不再提醒）。
                # 实名 / 导出 / 绑定 不属业务操作，不拦。
                paused = state.is_member_paused(inbound.group_id,
                                                inbound.phone)
                business_cmd = parsed.command in (
                    commands.CMD_CHECKIN, commands.CMD_SUMMARY,
                    commands.CMD_REPORT)
                summary_pending = state.get_pending_session(
                    inbound.group_id, inbound.phone, summary_handler.BIZ,
                    _now_dt()) is not None
                if paused and (business_cmd
                               or (parsed.result == commands.RESULT_UNKNOWN
                                   and summary_pending)):
                    # 清掉总结待输入会话：恢复考核后 ⛔ 不突然提交旧内容
                    if summary_pending:
                        state.drop_pending_session(
                            inbound.group_id, inbound.phone,
                            summary_handler.BIZ)
                    reply = PAUSED_MEMBER_TEXT
                elif parsed.result == commands.RESULT_DISPATCH:
                    reply = router.dispatch(parsed, ctx)
                elif parsed.result == commands.RESULT_UNKNOWN:
                    # 两轮总结优先于未知命令话术（§2.2）
                    reply = summary_handler.handle_pending_text(ctx)
                    if reply is None:
                        reply = commands.reply_text(parsed)
                else:
                    reply = commands.reply_text(parsed)
        except Exception:
            # ⛔ 不向用户暴露内部细节；安全事件落库，运维查 security_event
            state.record_security_event(
                "handler_error", inbound.group_id, inbound.phone,
                "command=%r result=%r" % (parsed.command, parsed.result))
            LOG.exception("handler failed")
            raise HTTPException(status_code=500, detail="internal error")

        delivered = None
        if reply and inbound.reply_url:
            # best-effort：回推失败只记日志，⛔ 不改写已完成的 A/B/C 归类
            # ⭐ 2026-10-16 用户指令一：群聊所有反馈都 @ 发送者（真实手机号，
            # ⛔ 不靠正文假装 @）；mention 由适配器注入载荷，见 qmx.reply_text。
            # ⭐ 2026-10-20 用户指令：**导出成功的回执是面向全员的通报**，
            # 文案里已写「@所有人」，⛔ 不再额外 @ 提问者（他不需要被单独点名）。
            # 失败/部分成功仍 @ 提问者 —— 他得知道文件没发出去。
            mention = (None if stats_handler.is_broadcast_receipt(reply)
                       else [inbound.phone])
            delivered = sender.reply(inbound.reply_url, reply,
                                     mention_phones=mention,
                                     group_id=inbound.group_id)
            # replied 只说明"有话要说"，⛔ 不代表群里真的出现（2026-10-15
            # 实测：平台回 2xx 但群里无消息，故 replied/delivered 分开报）
            if not delivered:
                LOG.warning("reply not delivered: text_len=%d", len(reply))
        return {"ok": True, "replied": bool(reply), "delivered": delivered}

    # ---------- H5 页面（同源，令牌走 fragment） ----------

    @app.get("/h5/checkin", response_class=HTMLResponse)
    def h5_checkin():
        return _render_template("checkin.html")

    @app.get("/h5/report", response_class=HTMLResponse)
    def h5_report():
        return _render_template("report.html")

    @app.post("/api/h5/checkin/info")
    def checkin_info(body: dict = Body(...)):
        token = _token_of(body)
        ctx, record = _ctx_from_token(token)
        if record is None:
            return {"ok": False, "reason": "not_found",
                    "message": TOKEN_NOT_FOUND_MESSAGE}
        data = checkin_handler.token_info(ctx, token)
        if isinstance(data, dict) and data.get("ok"):
            data["holder_name"] = _holder_name(ctx.group_id, ctx.phone)
            data["require_verify"] = True
        return data

    @app.post("/api/h5/checkin/upload")
    async def checkin_upload(request: Request):
        """图片上传（§4.11.4 D4 定夺：方案 A 机器人中转 + 服务器磁盘）。

        顺序纪律（与 info/submit 同一套硬鉴权，⛔ 不搞第二套）：
        令牌存在性 -> 越权/周期/状态校验（含留痕）-> 类型嗅探 + 大小上限
        -> 落盘 + 记凭证行（ref 与令牌绑定）-> 返回 image_ref。

        ⛔ 不信客户端声明的 content-type / 扩展名（魔数嗅探判定）；
        ⛔ 超限 / 非图片一律明确拒绝，⛔ 不静默截断、⛔ 不伪造已发送。

        ⚠️ 为什么不直接用 ``UploadFile = File(...)``：FastAPI 经
        ``request.form()`` 解析 multipart，starlette 默认 **1MB** 单部件
        上限（`max_part_size`）—— 手机拍照图普遍 2-8MB，会被挡在
        「There was an error parsing the body」这种不可读的通用 400 外。
        这里自调 ``form(max_part_size=...)``，让大小判定回到我们自己的
        明确话术（上限见 uploads.MAX_UPLOAD_BYTES）。
        """
        try:
            form = await request.form(
                max_part_size=uploads_mod.MAX_UPLOAD_BYTES + 1024 * 1024)
        except Exception:
            LOG.info("upload form parse failed")
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "read_failed",
                "message": "图片读取失败或超过大小上限（10MB），请压缩后重试"})
        token_value = form.get("token")
        upload = form.get("file")
        if not isinstance(token_value, str) or not hasattr(upload, "read"):
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "bad_request",
                "message": "请求格式不正确，请重新选择图片"})
        ctx, record = _ctx_from_token(_token_str(token_value))
        if record is None:
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "not_found",
                "message": TOKEN_NOT_FOUND_MESSAGE})
        # 身份核对先于一切业务校验：非本人连上传通道都不进（fail-closed）
        if not _holder_ok(record, form.get("verify")):
            return _holder_reject(record, "上传打卡图片")
        _record, reason = checkin_handler.validate_token(
            ctx, _token_str(token_value), with_session=False)
        if reason is not None:
            # 含已消费令牌：打卡已完成就无需再上传（复用统一拒绝话术 + 留痕）
            checkin_handler.audit_token_reject(ctx, reason)
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": reason.value,
                "message": checkin_handler.reject_text(reason)})
        try:
            content = await upload.read()
        except Exception:
            LOG.warning("upload read failed")
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "read_failed",
                "message": "图片读取失败，请重试"})
        ref = uploads_mod.generate_ref()
        try:
            saved = uploads_mod.save_upload(
                data_dir=ctx.config.data_dir, group_id=ctx.group_id,
                period_key=record.period_key, ref=ref,
                file_name=getattr(upload, "filename", "") or "",
                content=content)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={
                "ok": False, "reason": "bad_image",
                "message": str(exc)})
        state.put_upload(
            ref=ref, token_id=record.token_id, group_id=ctx.group_id,
            phone=ctx.phone, file_name=saved["file_name"], mime=saved["mime"],
            size=saved["size"], rel_path=saved["rel_path"], now=ctx.now)
        return {"ok": True, "image_ref": ref, "name": saved["file_name"],
                "size": saved["size"], "mime": saved["mime"]}

    @app.post("/api/h5/checkin/submit")
    def checkin_submit(body: dict = Body(...)):
        token = _token_of(body)
        ctx, record = _ctx_from_token(token)
        if record is None:
            return {"ok": False, "kind": "B", "reason": "not_found",
                    "message": TOKEN_NOT_FOUND_MESSAGE}
        image_ref = body.get("image_ref")
        image_meta = body.get("image_meta")
        if image_meta is not None and not isinstance(image_meta, dict):
            image_meta = None
        if not _holder_ok(record, body.get("verify")):
            return _holder_reject(record, "提交打卡")
        result = checkin_handler.submit_from_h5(
            ctx, token, image_ref if isinstance(image_ref, str) else "",
            image_meta)
        return {"ok": result.kind == KIND_A, "kind": result.kind,
                "message": result.message}

    @app.post("/api/h5/report/info")
    def report_info(body: dict = Body(...)):
        token = _token_of(body)
        ctx, record = _ctx_from_token(token)
        if record is None:
            return {"ok": False, "reason": "not_found",
                    "message": TOKEN_NOT_FOUND_MESSAGE}
        data = report_handler.token_info(ctx, token)
        if isinstance(data, dict) and data.get("ok"):
            data["holder_name"] = _holder_name(ctx.group_id, ctx.phone)
            data["require_verify"] = True
        return data

    @app.post("/api/h5/report/submit")
    def report_submit(body: dict = Body(...)):
        token = _token_of(body)
        ctx, record = _ctx_from_token(token)
        if record is None:
            return {"ok": False, "kind": "B", "reason": "not_found",
                    "message": TOKEN_NOT_FOUND_MESSAGE}
        # 2026-10-16 去角色化：⛔ 不再收客户端 role（服务端派生）
        if not _holder_ok(record, body.get("verify")):
            return _holder_reject(record, "提交报数")
        result = report_handler.submit_from_h5(ctx, token, body.get("items"))
        return {"ok": result.kind == KIND_A, "kind": result.kind,
                "message": result.message}

    return app


def _render_template(name: str) -> HTMLResponse:
    path = _TEMPLATES_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=500, detail="template missing")
    return HTMLResponse(path.read_text(encoding="utf-8"))
