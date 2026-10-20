# -*- coding: utf-8 -*-
"""阶段八：真实环境准备 —— 平台适配层 + 出站接缝测试。

覆盖（用户指令第八阶段）：
1. QmxAdapter 三口（reply / push / file）的 fail-closed 纪律：
   能力未确认前如实返回未发送 + 原因，⛔ 不伪造成功
2. reply_text 的真实 HTTP 路径：网络异常 / 非 2xx -> False（不当已发送）
3. 统计导出的文件发送接缝：适配器接通 -> 话术切「已发送」；
   未发送 / 异常 -> 如实说「发送到群聊失败」（⛔ 不谎称送达，⛔ 不贴服务器路径）

⚠️ 真实链路验证（回调可达 / 载荷形态 / H5 手机访问 / 文件送达）
被 P2 阻塞（等紫玉公网 IP + 机器人接口）。本文件只锁「接通后行为
可切换、接通前不虚报」；⛔ 不代表联调通过。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app import commands
from app.handlers import stats as stats_handler
from app.platform.qmx import QmxAdapter
from doubles.fakes import FakeBackend, RecordingSender
from helpers import GROUP, PHONE, TZ, enable_all, make_config, make_ctx

AM = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)


def ctx_with_sender(tmp_db, backend, sender, now=AM):
    ctx = make_ctx(tmp_db, backend,
                   make_config(Path(str(tmp_db.db_path)).parent), now=now)
    ctx.sender = sender
    return ctx


class TestAdapterFailClosed:
    """能力未确认前：⛔ 不伪造成功（§5.2 / D10 / D12）。"""

    def test_push_text_without_resolver_is_fail_closed(self):
        """⛔ 未注入地址解析器 -> 如实未发送（fail-closed），⛔ 不伪造成功。"""
        ok, reason = QmxAdapter().push_text(GROUP, "x")
        assert ok is False
        assert "not wired" in reason

    def test_push_text_without_bound_url_is_fail_closed(self):
        """⛔ 群未绑定 webhook 地址 -> 如实未发送，⛔ 不回退全局默认地址。"""
        adapter = QmxAdapter(url_resolver=lambda gid: None)
        ok, reason = adapter.push_text(GROUP, "x")
        assert ok is False
        assert "no webhook_base_url" in reason

    def test_push_text_uses_injected_url_and_returns_true(self, monkeypatch):
        """✅ 已接通：按注入的群级地址发送，同 reply 载荷形态。"""
        import httpx

        captured = {}

        class Resp:
            status_code = 200
            text = '{"ok":true,"code":200,"message":"成功"}'

            def json(self):
                return {"ok": True, "code": 200, "message": "成功"}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        adapter = QmxAdapter(
            url_resolver=lambda gid: "http://x/webhook/send?key=k")
        ok, reason = adapter.push_text(GROUP, "提醒正文")
        assert ok is True and reason == ""
        assert captured["url"] == "http://x/webhook/send?key=k"
        assert captured["json"] == {"type": "text",
                                    "textMsg": {"content": "提醒正文"}}

    def test_push_text_platform_reject_is_not_sent(self, monkeypatch):
        """⛔ 平台业务拒绝（HTTP 200 但 ok=false）-> 未发送，不因 200 误判。"""
        import httpx

        class Resp:
            status_code = 200
            text = '{"success":false,"code":7101,"message":"消息内容不可为空"}'

            def json(self):
                return {"success": False, "code": 7101,
                        "message": "消息内容不可为空"}

        monkeypatch.setattr(httpx, "post",
                            lambda url, json=None, timeout=None: Resp())
        adapter = QmxAdapter(url_resolver=lambda gid: "http://x/y")
        ok, reason = adapter.push_text(GROUP, "x")
        assert ok is False
        assert "rejected" in reason

    def test_send_file_without_resolver(self):
        """2026-10-20：file 通道已接通，但未注入群地址解析器仍须 fail-closed。"""
        ok, reason = QmxAdapter().send_file(GROUP, "/tmp/a.xlsx")
        assert ok is False
        assert "not wired" in reason

    def test_file_name_of(self):
        assert QmxAdapter.file_name_of("/a/b/c.xlsx") == "c.xlsx"

    def test_reply_text_network_error_returns_false(self, monkeypatch):
        import httpx

        def boom(*args, **kwargs):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "post", boom)
        assert QmxAdapter().reply_text("http://x/y", "hi") is False

    def test_reply_text_http_error_status_returns_false(self, monkeypatch):
        import httpx

        class Resp:
            status_code = 500

        monkeypatch.setattr(httpx, "post", lambda *a, **kw: Resp())
        assert QmxAdapter().reply_text("http://x/y", "hi") is False

    def test_reply_text_success_posts_text_payload(self, monkeypatch):
        """✅ 载荷形态已实测（2026-10-16）：type=text + textMsg.content。"""
        import httpx

        captured = {}

        class Resp:
            status_code = 200
            text = '{"ok":true,"code":200,"message":"成功"}'

            def json(self):
                return {"ok": True, "code": 200, "message": "成功"}

        def fake_post(url, json=None, timeout=None):
            captured["url"] = url
            captured["json"] = json
            return Resp()

        monkeypatch.setattr(httpx, "post", fake_post)
        assert QmxAdapter().reply_text("http://x/y", "hi") is True
        assert captured["json"] == {"type": "text",
                                    "textMsg": {"content": "hi"}}
        assert captured["url"] == "http://x/y"

    def test_reply_text_http200_business_fail_is_not_delivered(self,
                                                               monkeypatch):
        """⭐ HTTP 200 但业务码失败 -> **必须判未送达**。

        2026-10-15 线上根因：平台把业务失败包在 HTTP 200 里，原实现只
        看状态码，于是「回推成功」而群里无消息（打卡零落地）。
        ⛔ 这条用例就是防止该回归再发生。
        """
        import httpx

        class Resp:
            status_code = 200
            text = '{"success":false,"code":7101,"message":"消息内容不可为空"}'

            def json(self):
                return {"success": False, "code": 7101,
                        "message": "消息内容不可为空"}

        monkeypatch.setattr(httpx, "post",
                            lambda url, json=None, timeout=None: Resp())
        assert QmxAdapter().reply_text("http://x/y", "hi") is False

    def test_reply_text_non_json_body_is_not_delivered(self, monkeypatch):
        """⛔ 响应体非 JSON / 响应对象形态异常 -> 未送达，⛔ 且不抛异常。

        ⛔ 出站判定的异常绝不允许穿透到回调主流程（server 层会转 500），
        故替身刻意缺 .json()（模拟第三方库行为差异）来锁死这一点。
        """
        import httpx

        class Resp:
            status_code = 200
            text = "OK"
            # ⛔ 刻意不提供 .json()

        monkeypatch.setattr(httpx, "post",
                            lambda url, json=None, timeout=None: Resp())
        assert QmxAdapter().reply_text("http://x/y", "hi") is False

    def test_reply_text_non_dict_json_is_not_delivered(self, monkeypatch):
        """⛔ JSON 体不是对象（如数组）-> 未送达，⛔ 不因 200 误判成功。"""
        import httpx

        class Resp:
            status_code = 200
            text = "[1,2]"

            def json(self):
                return [1, 2]

        monkeypatch.setattr(httpx, "post",
                            lambda url, json=None, timeout=None: Resp())
        assert QmxAdapter().reply_text("http://x/y", "hi") is False


class TestExportSendSeam:
    """统计导出的文件发送接缝：接通后只换适配器实现，处理器不改。"""

    def _setup(self, tmp_db):
        enable_all(tmp_db)
        # ⭐ 2026-10-19 单权化：白名单只有 ADMIN_ALL 一种
        tmp_db.upsert_whitelist(GROUP, PHONE)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": []}
        return backend

    def _export(self, tmp_db, backend, ctx):
        return stats_handler.handle_command(commands.parse("导出"), ctx)

    def test_no_sender_keeps_blocked_wording(self, tmp_db):
        backend = self._setup(tmp_db)
        ctx = make_ctx(tmp_db, backend,
                       make_config(Path(str(tmp_db.db_path)).parent), now=AM)
        reply = self._export(tmp_db, backend, ctx)
        # 2026-10-20：失败话术不再带服务器路径（内部信息不进群聊）
        assert "发送到群聊失败" in reply and "本地路径" not in reply

    def test_adapter_sent_switches_wording(self, tmp_db):
        backend = self._setup(tmp_db)
        sender = RecordingSender()
        sender.file_ok = True  # 模拟「实测接通后」
        ctx = ctx_with_sender(tmp_db, backend, sender)
        reply = self._export(tmp_db, backend, ctx)
        # ⭐ 2026-10-20 用户指令：回执统一成「@所有人 + 固定文案」
        assert reply.startswith("@所有人 📊")
        assert "已发送至群聊" in reply
        assert len(sender.files_sent) == 1
        assert sender.files_sent[0]["group_id"] == GROUP
        # 优先 CSV（与后台导出口径一致）
        assert sender.files_sent[0]["filename"].endswith(".csv")

    def test_adapter_failure_keeps_blocked_wording(self, tmp_db):
        backend = self._setup(tmp_db)
        sender = RecordingSender()  # 默认未接通（D12）
        ctx = ctx_with_sender(tmp_db, backend, sender)
        reply = self._export(tmp_db, backend, ctx)
        assert "发送到群聊失败" in reply
        assert sender.files_sent == []

    def test_adapter_exception_not_counted_as_sent(self, tmp_db):
        backend = self._setup(tmp_db)

        class BoomSender:
            def send_file(self, *args, **kwargs):
                raise RuntimeError("socket down")

        ctx = ctx_with_sender(tmp_db, backend, BoomSender())
        reply = self._export(tmp_db, backend, ctx)
        assert "发送到群聊失败" in reply  # ⛔ 适配器异常不当已送达


class TestFileSendChannel:
    """2026-10-20 实测接通的 file 通道（D12 解除）。

    链路：uploadAttachment（multipart, type=2）-> content.id ->
    ``{"type":"file","fileMsg":{"fileId":…}}``。
    这里锁「两步都做了、载荷正确、任一步失败都⛔ 不算已发送」。
    """

    URL = "https://imtwo.zdxlz.com/im-external/v1/webhook/send?key=K"

    def _adapter(self):
        return QmxAdapter(url_resolver=lambda gid: self.URL)

    class _Resp:
        def __init__(self, payload, status=200):
            self.status_code = status
            self._payload = payload
            self.text = "{}"

        def json(self):
            return self._payload

    def _file(self, tmp_path):
        path = tmp_path / "export_all.xlsx"
        path.write_bytes(b"PK\x03\x04demo")
        return path

    def test_upload_then_send(self, monkeypatch, tmp_path):
        import httpx
        calls = []

        def fake_post(url, files=None, data=None, json=None, timeout=None):
            calls.append({"url": url, "files": files, "data": data,
                          "json": json})
            if "uploadAttachment" in url:
                return self._Resp({"ok": True, "code": 200,
                                   "content": {"id": "FID-1"}})
            return self._Resp({"ok": True, "code": 200, "message": "成功"})

        monkeypatch.setattr(httpx, "post", fake_post)
        ok, reason = self._adapter().send_file(GROUP, self._file(tmp_path),
                                               filename="export_all.xlsx")
        assert ok is True and reason == ""
        assert len(calls) == 2
        # ① 上传：实测命中的 im 主机 + 正确路径 + type=2
        assert calls[0]["url"].startswith(
            "https://im.zdxlz.com/im-api/v1/webHook/uploadAttachment")
        assert "type=2" in calls[0]["url"]
        assert calls[0]["data"] == {"type": "2"}
        assert calls[0]["files"]["file"][0] == "export_all.xlsx"
        # ② 发 file 消息：只认业务码（这里 200/ok）
        assert calls[1]["url"] == self.URL
        assert calls[1]["json"] == {"type": "file",
                                    "fileMsg": {"fileId": "FID-1"}}

    def test_upload_rejected_is_not_sent(self, monkeypatch, tmp_path):
        """上传失败 -> ⛔ 不发消息、⛔ 不返回成功。"""
        import httpx
        calls = []

        def fake_post(url, files=None, data=None, json=None, timeout=None):
            calls.append(url)
            return self._Resp({"success": False, "code": 7101,
                               "message": "文件内容不可为空"})

        monkeypatch.setattr(httpx, "post", fake_post)
        ok, reason = self._adapter().send_file(GROUP, self._file(tmp_path))
        assert ok is False
        assert "upload rejected" in reason
        assert all("uploadAttachment" in u for u in calls)  # 只试上传，没发消息

    def test_send_rejected_after_upload(self, monkeypatch, tmp_path):
        """上传成功但发消息被拒 -> 如实「未发送」。"""
        import httpx

        def fake_post(url, files=None, data=None, json=None, timeout=None):
            if "uploadAttachment" in url:
                return self._Resp({"ok": True, "code": 200,
                                   "content": {"id": "FID-2"}})
            return self._Resp({"success": False, "code": 500,
                               "message": "系统异常"})

        monkeypatch.setattr(httpx, "post", fake_post)
        ok, reason = self._adapter().send_file(GROUP, self._file(tmp_path))
        assert ok is False
        assert "rejected file message" in reason

    def test_without_resolver_is_fail_closed(self, tmp_path):
        ok, reason = QmxAdapter().send_file(GROUP, self._file(tmp_path))
        assert ok is False
        assert "not wired" in reason
