# -*- coding: utf-8 -*-
"""量子密信平台适配器（用户指令第八阶段：真实接口适配层）。

定位：**全部平台相关假设的唯一落脚点**。实测（§7.2 / D10-D12）后发现
形态不符时，只改本模块，⛔ 不散落改业务处理器。

三种出站能力（2026-10-16 实测后状态）：
- reply_text  入站回推：✅ **已实测接通** —— 载荷 ``{"type":"text",
  "textMsg":{"content":...}}``，实测返回 ``{"ok":true,"code":200,
  "message":"成功"}``；⛔ 判成功必须读业务码（HTTP 200 也可能是业务失败）
- push_text   主动推送 / 提醒：✅ **已实测接通**（与 reply 同形态，
  地址取群级 webhook_base_url；仅 ACTIVE 实例，⛔ 无全局兜底地址）
- send_file   群内文件发送（统计导出用）：✅ **2026-10-20 实测接通**
  —— 两步：① multipart 上传到 ``im.zdxlz.com/im-api/v1/webHook/
  uploadAttachment?key=…&type=2`` 拿 ``content.id``；② 再向群 webhook 发
  ``{"type":"file","fileMsg":{"fileId":<id>}}``。实测返回
  ``{"ok":true,"code":200,"message":"成功"}``（D12 阻塞解除）

⚠️ **HTTP 200 ≠ 送达**：量子密信把业务失败（如缺字段 5005、限流 7102、
内容为空 7101）也包在 200 里。⛔ 任何出站判定都必须解析 JSON 的
``ok``/``code``，只看状态码会把失败误判成成功 —— 这正是 2026-10-15
打卡回推「200 但群里无消息」的根因。

实测脚本：deploy/probe_webhook_payload.py（字段名靠「故意发错 →
读平台报错」反推，⛔ 不猜协议）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..web.sender import ReplySender

LOG = logging.getLogger("group-ops.platform.qmx")


class QmxAdapter(ReplySender):
    """量子密信出站适配器：reply / push / send_file 三口收敛。

    实现机器人侧唯一的平台出站接口；后续实测修订只发生在这里。
    """

    def __init__(self, timeout_seconds: float = 10.0,
                 url_resolver=None) -> None:
        """url_resolver：``group_id -> webhook 地址`` 的可调用对象（可空）。

        ⚠️ push 需要按群取地址（群级配置），而入站 reply 的地址由平台随
        载荷回传、⛔ 不需要查库。装配处（app/main.py `_build`）注入
        ``lambda gid: state.get_robot_instance(gid)`` 的读取结果；
        ⛔ 未注入时 push 如实返回未发送（fail-closed），不伪造已发送。
        """
        self._timeout = timeout_seconds
        self._url_resolver = url_resolver

    def _post_json(self, url: str, payload: dict) -> bool:
        """向发送网关 POST 一份载荷，按**业务码**判定是否送达。

        ⛔ 共享实现：reply_text 与 push_text 必须走同一套判定
        （否则两处真相，将来必然漂移）。观测探针也只此一份。
        """
        import httpx
        try:
            resp = httpx.post(url, json=payload, timeout=self._timeout)
        except httpx.HTTPError as exc:
            LOG.warning("outbound POST failed: %s", exc)
            return False
        # ---- 只观测探针（⛔ 不打 query：URL 的 key= 就是平台凭据）----
        try:
            host = httpx.URL(url).host
        except Exception:  # pragma: no cover - URL 异常不该影响发送
            host = "?"
        body = str(getattr(resp, "text", "") or "")[:200]
        LOG.info("outbound_probe host=%s status=%s body=%r text_len=%d "
                 "keys=%s", host, resp.status_code, body,
                 len(str(payload.get("textMsg", {}).get("content", ""))),
                 sorted(payload.get("textMsg", {}).keys()))
        if resp.status_code >= 300:
            LOG.warning("outbound POST -> HTTP %s", resp.status_code)
            return False
        # ⚠️ HTTP 200 不等于送达：必须解析业务码（见 docstring 事实 1/2）
        # ⛔ 捕获 Exception 而非仅 ValueError：出站判定**绝不允许**异常上抛
        # 穿透到回调主流程（server 层会把异常转成 HTTP 500），故响应对象
        # 形态异常（如替身缺 .json）一律按"未送达"处理。
        try:
            data = resp.json()
        except Exception as exc:
            LOG.warning("outbound POST -> unreadable body (HTTP %s): %s",
                        resp.status_code, type(exc).__name__)
            return False
        if not isinstance(data, dict):
            LOG.warning("outbound POST -> body is not a JSON object")
            return False
        ok = bool(data.get("ok")) or data.get("code") == 200
        if not ok:
            LOG.warning("outbound rejected by platform: code=%s msg=%s",
                        data.get("code"), data.get("message")
                        or data.get("msg"))
        return ok

    # ---------- 1. 入站回推（载荷已实测） ----------

    def reply_text(self, reply_url: str, text: str,
                   mention_phones=None, group_id: str = "") -> bool:
        """向回推地址发文本（§3.2 出站段）+ ⭐ @ 发送者（用户指令 2026-10-16 一）。

        ⭐ **@ 发送者**（群聊所有反馈、提示、回执都要 @ 到人）：mention_phones
        非空 -> textMsg 内补官方 @ 字段（``isMentioned`` /
        ``mentionType:2`` / ``mentionedMobileList`` / ``groupId``），与
        push_text **同载荷形态**（push 侧已实测接通）；⛔ 用真实手机号，
        ⛔ 不靠 content 里写 ``@姓名`` 假装 @。同一发送网关，部署后首次
        真实回复由 outbound_probe 的 ``keys=`` 观察平台是否接受。

        ✅ **载荷形态已实测**（2026-10-16，probe_webhook_payload.py 逐轮探测）：
        正确形态 = ``{"type": "text", "textMsg": {"content": <正文>}}``。

        实测过程中确认的三条硬事实（⛔ 都是观测，不是推断）：
        1. ⭐ **HTTP 200 ≠ 成功**：平台把业务失败也包在 200 里
           （缺字段回 200 + ``code:5005``、限流回 200 + ``code:7102``）。
           判定必须读 JSON 的 ``ok``/``code``，⛔ 不能只看 HTTP 状态码 ——
           2026-10-15 打卡回推就是 HTTP 200 但业务失败，被误判"发送成功"，
           实则群里无任何消息。
        2. 成功的响应体结构与失败**不同**：失败是 ``{"success":false,
           "code":5xxx,"message":...}``，成功是 ``{"ok":true,"code":200,
           "message":"成功"}``。故判定同时看 ``ok`` 与 ``code``。
        3. ``type`` 必须是**字符串** ``"text"``（数字 1 会回 500）；
           正文必须嵌在 ``textMsg.content``（平铺的 ``content``/``text``/
           ``msgContent`` 一律回 7101「消息内容不可为空」）。
        """
        phones = [str(p) for p in (mention_phones or []) if p]
        payload = {"type": "text", "textMsg": {"content": text}}
        if phones:
            payload["textMsg"].update({
                "isMentioned": True,
                "mentionType": 2,
                "mentionedMobileList": phones,
                "groupId": str(group_id or ""),
            })
        return self._post_json(reply_url, payload)

    # ReplySender 抽象的别名：装配处把本适配器当 ReplySender 用
    def reply(self, reply_url: str, text: str, mention_phones=None,
              group_id: str = "") -> bool:
        return self.reply_text(reply_url, text,
                               mention_phones=mention_phones,
                               group_id=group_id)

    # ---------- 2. 主动推送 / @（能力未确认，fail-closed） ----------

    def push_text(self, group_id: str, text: str, mention_phones=None):
        """定时 / 手动提醒的群推送（2026-10-16 已实测接通 + 真正 @）。

        ✅ 载荷形态（实测 + 用户提供的官方协议）：
        ``{"type":"text","textMsg":{"content":..., "isMentioned":bool,
        "mentionType":2, "mentionedMobileList":[手机号], "groupId":群ID}}``

        ⚠️ **HTTP 200 ≠ 送达**：平台把业务失败包在 200 里，判定必须
        读 JSON 的 ``ok``/``code``（详见 reply_text docstring 事实 1/2）。

        mention_phones 非空 -> 真正 @ 这些手机号（⛔ ⛔ 用**真实手机号**，
        ⛔ 不靠 content 里写 ``@姓名`` 假装 @）；为空 -> 普通文本发送。
        """
        if self._url_resolver is None:
            return False, "push not wired: url_resolver not injected"
        try:
            url = self._url_resolver(group_id)
        except Exception as exc:
            return False, "push url lookup error: %s" % exc
        if not url or not str(url).strip():
            return False, "group has no webhook_base_url (bind in admin first)"
        phones = [str(p) for p in (mention_phones or []) if p]
        payload = {"type": "text", "textMsg": {"content": text}}
        if phones:
            # 官方 @ 协议：mentionType=2 表示按手机号 @
            payload["textMsg"].update({
                "isMentioned": True,
                "mentionType": 2,
                "mentionedMobileList": phones,
                "groupId": group_id,
            })
        ok = self._post_json(str(url).strip(), payload)
        if ok:
            return True, ""
        return False, "platform rejected push (see reply_probe log)"

    # ---------- 3. 群内文件发送（2026-10-20 实测接通，D12 解除） ----------

    #: 附件上传路径。⚠️ 与 send **不同主机**：send 在 ``imtwo.zdxlz.com/
    #: im-external/v1/webhook/send``，upload 在 ``im.zdxlz.com/im-api/v1/
    #: webHook/uploadAttachment``（2026-10-20 逐个候选实测，只有这条不 404）。
    _UPLOAD_PATH = "/im-api/v1/webHook/uploadAttachment"

    def _upload_urls(self, send_url: str) -> list:
        """群 send 地址 -> 候选上传地址（按实测命中顺序，⛔ 不猜协议）。

        1) ``imtwo`` 主机换成 ``im``（实测命中路径）
        2) 同主机 + 上传路径（主备：平台若收敛到单主机直接生效）
        3) 同源把 ``/send`` 换成 ``/uploadAttachment``（旧版网关兜底）
        """
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(send_url)
        host = parts.hostname or ""
        port = (":%d" % parts.port) if parts.port else ""
        query = parts.query
        if not query:
            query = "type=2"
        elif "type=" not in query:
            query = query + "&type=2"
        urls = []
        if "imtwo" in host:
            urls.append(urlunsplit((parts.scheme,
                                    host.replace("imtwo", "im", 1) + port,
                                    self._UPLOAD_PATH, query, "")))
        urls.append(urlunsplit((parts.scheme, parts.netloc,
                                self._UPLOAD_PATH, query, "")))
        urls.append(urlunsplit((parts.scheme, parts.netloc,
                                parts.path.replace("/send",
                                                   "/uploadAttachment"),
                                query, "")))
        # 去重但保序
        seen, out = set(), []
        for url in urls:
            if url not in seen:
                seen.add(url)
                out.append(url)
        return out

    @staticmethod
    def _file_id_of(data: dict) -> str:
        """上传响应里取附件 ID（实测 ``content.id``；兼容 ``data.id``）。"""
        for key in ("content", "data"):
            node = data.get(key)
            if isinstance(node, dict) and node.get("id"):
                return str(node["id"])
        if data.get("id"):
            return str(data["id"])
        return ""

    def upload_file(self, send_url: str, path, filename: str = None):
        """上传附件拿 fileId，返回 ``(file_id, reason)``。

        ⛔ 失败一律如实返回原因（⛔ 不伪造 ID）；⛔ 不打 URL 的 query（key）。
        """
        import httpx
        name = filename or Path(path).name
        try:
            blob = Path(path).read_bytes()
        except OSError as exc:
            return None, "read export file failed: %s" % exc
        last = "upload endpoint not reachable"
        for url in self._upload_urls(send_url):
            try:
                resp = httpx.post(url, files={"file": (name, blob)},
                                  data={"type": "2"},
                                  timeout=max(self._timeout, 30.0))
            except httpx.HTTPError as exc:
                last = "upload transport: %s" % exc
                LOG.warning("upload POST failed: %s", exc)
                continue
            try:
                host = httpx.URL(url).host
            except Exception:  # pragma: no cover
                host = "?"
            body = str(getattr(resp, "text", "") or "")[:220]
            LOG.info("upload_probe host=%s status=%s body=%r size=%d name=%s",
                     host, resp.status_code, body, len(blob), name)
            if resp.status_code >= 300:
                last = "upload HTTP %s" % resp.status_code
                continue
            try:
                data = resp.json()
            except Exception:
                last = "upload unreadable body"
                continue
            if not isinstance(data, dict):
                last = "upload body is not a JSON object"
                continue
            ok = bool(data.get("ok")) or data.get("code") == 200
            file_id = self._file_id_of(data)
            if ok and file_id:
                return file_id, ""
            last = "upload rejected: code=%s msg=%s" % (
                data.get("code"), data.get("message") or data.get("msg"))
        return None, last

    def send_file(self, group_id: str, path, filename: str = None):
        """群内文件发送（统计导出用）：上传取 fileId -> 发 file 消息。

        ✅ 2026-10-20 实测链路（线上真实群）：
        ① ``POST https://im.zdxlz.com/im-api/v1/webHook/uploadAttachment?
           key=…&type=2``（multipart ``file`` + form ``type=2``）
           -> ``{"ok":true,"code":200,"content":{"id":"…",...}}``
        ② ``POST {群 webhook}`` ``{"type":"file","fileMsg":{"fileId":"…"}}``
           -> ``{"ok":true,"code":200,"message":"成功"}``

        ⛔ 判成功只认 ② 的业务码；①成功②失败时如实返回未发送。
        """
        if self._url_resolver is None:
            return False, "file send not wired: url_resolver not injected"
        try:
            url = self._url_resolver(group_id)
        except Exception as exc:
            return False, "file send url lookup error: %s" % exc
        if not url or not str(url).strip():
            return False, "group has no webhook_base_url (bind in admin first)"
        url = str(url).strip()
        file_id, reason = self.upload_file(url, path, filename)
        if not file_id:
            return False, reason
        ok = self._post_json(url, {"type": "file",
                                   "fileMsg": {"fileId": file_id}})
        if ok:
            return True, ""
        return False, "platform rejected file message (see send probe log)"

    @staticmethod
    def file_name_of(path) -> str:
        """导出文件名（发送时展示用；路径不含敏感信息）。"""
        return Path(path).name
