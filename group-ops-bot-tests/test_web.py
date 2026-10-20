# -*- coding: utf-8 -*-
"""Web 层测试（:18002 回调 + H5，TestClient 进程内 ASGI，§2.8）。

⛔ 这是「本地自测」证据，不是平台回调真实验证（§7.2 / D10 / D11）。
"""

from __future__ import annotations

import re
from pathlib import Path
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import commands
from app.tokens import token_id_of
from app.web.server import create_app
from doubles.fakes import FakeBackend, RecordingSender
from helpers import (GROUP, PHONE, TZ, enable_all, extract_token, make_config,
                     make_upload, setup_fields)

NOW = datetime(2026, 10, 13, 10, 0, tzinfo=TZ)
# 2026-10-19 D17：H5 链接身份绑定 —— 上传/提交要带本人手机号后 4 位
HOLDER_TAIL = PHONE[-4:]


def make_app(tmp_db, backend=None, sender=None, now=None):
    from pathlib import Path
    backend = backend or FakeBackend()
    sender = sender or RecordingSender()
    config = make_config(Path(tmp_db.db_path).parent)
    app = create_app(state=tmp_db, backend=backend, sender=sender,
                     config=config, now_fn=lambda: now or NOW)
    return app, backend, sender


def callback_payload(text, group_id=GROUP, phone=PHONE):
    return {"group_id": group_id, "phone": phone, "text": text,
            "reply_url": "http://reply.test/hook",
            "name": "测试用户", "user_id": "puid-1"}


def extract_token(sent_text):
    match = re.search(r"#t=(\S+)", sent_text)
    assert match, "no token in reply: %r" % sent_text
    return match.group(1)


class TestCallback:
    def test_unbound_group_gets_bind_guide(self, tmp_db):
        """⭐ 2026-10-19 用户指令：未绑定群不再静默，统一引导管理员绑定。"""
        app, backend, sender = make_app(tmp_db)
        client = TestClient(app)
        resp = client.post("/api/callback",
                           json=callback_payload("打卡", group_id="G999"))
        assert resp.status_code == 200
        assert resp.json()["replied"] is True
        assert len(sender.sent) == 1
        assert "本群尚未接入机器人" in sender.sent[0]["text"]
        assert "「@general-bot 绑定」" in sender.sent[0]["text"]
        # 回推真实 @ 发送者（⛔ 不靠正文假装 @）
        assert sender.sent[0]["mention_phones"] == [PHONE]
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='unauthorized_group'"
        ).fetchall()
        assert len(events) == 1

    def test_every_reply_mentions_sender(self, tmp_db):
        """⭐ 群聊所有反馈都 @ 发送者（2026-10-16 用户指令一）。

        覆盖四类回话点：业务命令回复 / 帮助文案 / 格式错误用法 / 未命中。
        """
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        for text in ("打卡", "打卡了", "总结 日报", "你好"):
            client.post("/api/callback", json=callback_payload(text))
        assert len(sender.sent) == 4
        for item in sender.sent:
            # ⛔ 真实手机号，不靠正文写「@姓名」假装 @
            assert item["mention_phones"] == [PHONE]
            assert item["group_id"] == GROUP

    def test_invalid_payload_400(self, tmp_db):
        app, _b, _s = make_app(tmp_db)
        client = TestClient(app)
        resp = client.post("/api/callback", json={"text": "打卡"})
        assert resp.status_code == 400

    def test_checkin_command_replies_link(self, tmp_db):
        enable_all(tmp_db)
        app, backend, sender = make_app(tmp_db)
        client = TestClient(app)
        resp = client.post("/api/callback", json=callback_payload("打卡"))
        assert resp.status_code == 200
        assert len(sender.sent) == 1
        assert "/h5/checkin#t=" in sender.sent[0]["text"]

    def test_unknown_command_help(self, tmp_db):
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("你好"))
        assert sender.sent and "打卡" in sender.sent[-1]["text"]

    def test_summary_two_round_via_callback(self, tmp_db):
        enable_all(tmp_db)
        app, backend, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("总结"))
        assert "请发送您的工作总结内容" in sender.sent[-1]["text"]
        client.post("/api/callback",
                    json=callback_payload("今天完成了登录模块"))
        assert "已成功完成" in sender.sent[-1]["text"]

    def test_command_preempts_pending(self, tmp_db):
        """有待处理总结时发新命令 -> 走命令，pending 保留。"""
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("总结"))
        client.post("/api/callback", json=callback_payload("打卡"))
        assert "/h5/checkin#t=" in sender.sent[-1]["text"]
        row = tmp_db.get_pending_session(GROUP, PHONE, "summary", NOW)
        assert row is not None


class TestH5Report:
    def test_full_flow(self, tmp_db):
        enable_all(tmp_db)
        setup_fields(tmp_db)
        tmp_db.upsert_member_task(GROUP, PHONE, role="开发")
        app, backend, sender = make_app(tmp_db)
        client = TestClient(app)

        client.post("/api/callback", json=callback_payload("报数"))
        token = extract_token(sender.sent[-1]["text"])

        info = client.post("/api/h5/report/info", json={"token": token})
        assert info.status_code == 200
        body = info.json()
        assert body["ok"] is True
        # 2026-10-16 去角色化：⛔ 不再返回角色；字段全局共享（不按角色过滤）
        assert "role" not in body
        assert len(body["fields"]) == 2

        sender.push_ok = True
        submit = client.post("/api/h5/report/submit", json={
            "token": token,
            "verify": HOLDER_TAIL,
            "items": [{"field_id": 1, "quantity": 3},
                      {"field_id": 2, "quantity": 500}]})
        assert submit.status_code == 200
        result = submit.json()
        assert result["ok"] is True and result["kind"] == "A"
        # 用户指令四：成功话术统一为总结格式风格
        assert result["message"] == "✅ 今日工作报数已成功完成。"
        names = [c[0] for c in backend.calls]
        assert "report_submit" in names
        # 角色服务端派生：应交名单配置优先（⛔ 不信客户端回传）
        _name, kwargs = backend.calls[-1]
        assert kwargs["role"] == "开发"
        # 用户指令三：成功后群内 @ 通知（真实手机号，⛔ 不靠文本假装 @）
        assert len(sender.pushed) == 1
        assert sender.pushed[0]["mention_phones"] == [PHONE]
        assert sender.pushed[0]["text"] == "✅ 今日工作报数已成功完成。"

    def test_bad_token(self, tmp_db):
        app, _b, _s = make_app(tmp_db)
        client = TestClient(app)
        resp = client.post("/api/h5/report/info", json={"token": "nope"})
        assert resp.json()["ok"] is False

    def test_pages_served(self, tmp_db):
        app, _b, _s = make_app(tmp_db)
        client = TestClient(app)
        for path, marker in (("/h5/checkin", "拍照打卡"),
                             ("/h5/report", "今日报数")):
            resp = client.get(path)
            assert resp.status_code == 200
            assert marker in resp.text

    def test_checkin_submit_c_class_when_backend_down(self, tmp_db):
        enable_all(tmp_db)
        backend = FakeBackend()
        backend.raise_on.add("checkin_submit")
        app, _b, sender = make_app(tmp_db, backend=backend)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("打卡"))
        token = extract_token(sender.sent[-1]["text"])
        # 先走上传端点拿真 image_ref（§4.11.4 D4 定夺后的硬校验），
        # 再让后端不可达 -> C 类（⛔ 不是「缺图」的 B）
        up = client.post("/api/h5/checkin/upload",
                         files={"file": ("a.jpg", JPEG_BYTES, "image/jpeg")},
                         data={"token": token, "verify": HOLDER_TAIL})
        assert up.status_code == 200
        resp = client.post("/api/h5/checkin/submit",
                           json={"token": token, "verify": HOLDER_TAIL,
                                 "image_ref": up.json()["image_ref"]})
        assert resp.json()["kind"] == "C"
        tasks = tmp_db.pending_recovery_tasks()
        assert len(tasks) == 1


JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x11" * 120
GIF_BYTES = b"GIF89a" + b"\x00" * 120


class TestH5CheckinUpload:
    """§4.11.4 D4 定夺（2026-10-16）：图片上传通道真实链路。

    口径：方案 A（H5 -> 机器人 multipart -> 服务器磁盘）+ 凭证行与令牌绑定。
    ⛔ 不信客户端声明的类型（魔数嗅探）；⛔ 不认他人令牌的 image_ref。
    """

    def _issue(self, tmp_db, client, sender):
        enable_all(tmp_db)
        client.post("/api/callback", json=callback_payload("打卡"))
        return extract_token(sender.sent[-1]["text"])

    def test_upload_then_submit_notifies_group_with_mention(self, tmp_db):
        app, backend, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)

        info = client.post("/api/h5/checkin/info", json={"token": token})
        body = info.json()
        assert body["ok"] is True
        assert body["expires_at"]          # H5 倒计时数据源（用户指令二）
        assert body["ttl_minutes"] == 30

        up = client.post("/api/h5/checkin/upload",
                         files={"file": ("shot.jpg", JPEG_BYTES,
                                         "image/jpeg")},
                         data={"token": token, "verify": HOLDER_TAIL})
        assert up.status_code == 200
        ref = up.json()["image_ref"]

        row = tmp_db.get_upload(ref)
        assert row["token_id"] == token_id_of(token)
        assert row["mime"] == "image/jpeg"
        from app.web import uploads as uploads_mod
        path = uploads_mod.upload_abs_path(
            Path(tmp_db.db_path).parent, row["rel_path"])
        assert path is not None and path.read_bytes() == JPEG_BYTES

        sender.push_ok = True
        submit = client.post("/api/h5/checkin/submit",
                             json={"token": token, "image_ref": ref,
                                 "verify": HOLDER_TAIL})
        assert submit.json()["kind"] == "A"
        # 用户指令二：成功后群内 @ 通知（真实手机号，⛔ 不靠文本假装 @）
        assert len(sender.pushed) == 1
        assert sender.pushed[0]["mention_phones"] == [PHONE]
        # 用户指令四（2026-10-16）：三业务成功话术统一为总结格式风格
        assert sender.pushed[0]["text"] == "✅ 上午打卡已成功完成。"
        # 凭证标记已引用（审计 / 清理用）
        assert tmp_db.get_upload(ref)["consumed"] == 1

    def test_upload_rejects_non_image(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        resp = client.post("/api/h5/checkin/upload",
                           files={"file": ("a.gif", GIF_BYTES, "image/gif")},
                           data={"token": token, "verify": HOLDER_TAIL})
        assert resp.status_code == 400
        assert "JPG" in resp.json()["message"]

    def test_upload_rejects_oversize(self, tmp_db):
        from app.web import uploads as uploads_mod
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        big = b"\xff\xd8\xff" + b"\x00" * (uploads_mod.MAX_UPLOAD_BYTES + 16)
        resp = client.post("/api/h5/checkin/upload",
                           files={"file": ("big.jpg", big, "image/jpeg")},
                           data={"token": token, "verify": HOLDER_TAIL})
        assert resp.status_code == 400
        assert "上限" in resp.json()["message"]

    def test_upload_unknown_token_rejected(self, tmp_db):
        app, _b, _s = make_app(tmp_db)
        client = TestClient(app)
        resp = client.post("/api/h5/checkin/upload",
                           files={"file": ("a.jpg", JPEG_BYTES,
                                           "image/jpeg")},
                           data={"token": "nope"})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "not_found"

    def test_upload_expired_token_rejected(self, tmp_db):
        from datetime import timedelta
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("打卡"))
        token = extract_token(sender.sent[-1]["text"])
        # 时钟推到 31 分钟后（30 分钟硬上限外）再上传 -> 明确拒绝
        app2, _b2, _s2 = make_app(tmp_db, now=NOW + timedelta(minutes=31))
        client2 = TestClient(app2)
        resp = client2.post("/api/h5/checkin/upload",
                            files={"file": ("a.jpg", JPEG_BYTES,
                                            "image/jpeg")},
                            data={"token": token, "verify": HOLDER_TAIL})
        assert resp.status_code == 400
        assert resp.json()["reason"] == "expired"

    def test_submit_rejects_foreign_upload_ref(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        foreign = make_upload(tmp_db, "not-the-token")
        resp = client.post("/api/h5/checkin/submit",
                           json={"token": token, "image_ref": foreign,
                                 "verify": HOLDER_TAIL})
        assert resp.json()["kind"] == "B"
        assert "图片凭证" in resp.json()["message"]

    def test_submit_without_image_ref_rejected(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        resp = client.post("/api/h5/checkin/submit",
                           json={"token": token, "image_ref": "",
                                 "verify": HOLDER_TAIL})
        assert resp.json()["kind"] == "B"
        assert "上传" in resp.json()["message"]

    def test_page_has_camera_and_gallery_entries(self, tmp_db):
        """用户指令一：拍照 + 图库两个入口（⛔ 不只强制摄像头）。"""
        app, _b, _s = make_app(tmp_db)
        client = TestClient(app)
        resp = client.get("/h5/checkin")
        assert resp.status_code == 200
        assert "拍照上传" in resp.text
        assert "从图库选择" in resp.text
        assert "链接已过期" in resp.text      # 逾期禁用话术（用户指令二）

    def test_link_text_carries_ttl_hint(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        enable_all(tmp_db)
        client.post("/api/callback", json=callback_payload("打卡"))
        assert "30 分钟" in sender.sent[-1]["text"]
        assert "逾期" in sender.sent[-1]["text"]


class TestStatsCommand:
    def test_stats_command_is_gone(self, tmp_db):
        """⭐「统计」已由「导出」取代 -> 精确匹配口径下是无关文本。"""
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("统计"))
        text = sender.sent[-1]["text"]
        assert text == commands.HELP_TEXT
        # ⭐ 群聊回复必 @ 发送者（用户指令一）
        assert sender.sent[-1]["mention_phones"] == [PHONE]

    def test_export_requires_admin_all(self, tmp_db):
        """一键导出全部业务：需 ADMIN_ALL 单权（⛔ 不再有分权概念）。"""
        enable_all(tmp_db)
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("导出"))
        assert "导出权限" in sender.sent[-1]["text"]

    def test_legacy_split_permission_rejected(self, tmp_db):
        """单权化：历史遗留的旧三权行一律拒绝（upsert 已拒旧权限值，
        故用原生 SQL 插入模拟存量数据）。"""
        enable_all(tmp_db)
        raw = tmp_db._conn
        raw.execute(
            "INSERT OR IGNORE INTO export_whitelist(group_id, user_id,"
            " permission_type, updated_at) VALUES(?,?,?,?)",
            (GROUP, PHONE, "EXPORT_CHECKIN", "2026-10-15 16:35:00+08:00"))
        raw.commit()
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("导出"))
        text = sender.sent[-1]["text"]
        assert "导出权限" in text

    def test_export_generates_workbook(self, tmp_db, tmp_path):
        enable_all(tmp_db)
        tmp_db.upsert_whitelist(GROUP, PHONE)
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [{
            "record_id": 1, "phone": PHONE, "name": "测试用户",
            "period_key": "2026-10-13", "session": "am",
            "checkin_time": "2026-10-13 09:01:00", "image_ref": "img/1.jpg",
            "image_name": "1.jpg", "image_size": 100}]}
        app, _b, sender = make_app(tmp_db, backend=backend)
        client = TestClient(app)
        client.post("/api/callback", json=callback_payload("导出"))
        text = sender.sent[-1]["text"]
        # 2026-10-20：file 通道已接通，但测试 sender 不带真实通道 ->
        # 如实告知发送失败；⛔ 不谎称已发送，⛔ 不把服务器路径贴进群聊
        assert "发送到群聊失败" in text
        assert "本地路径" not in text
        from pathlib import Path
        exports = Path(tmp_db.db_path).parent / "exports"
        assert list(exports.glob("*.xlsx"))  # 文件确实生成（只是没送达）


class TestH5HolderBinding:
    """D17（2026-10-19）：H5 链接与用户身份绑定。

    口径：链接在群里发，谁都能点开 —— 必须填「本人手机号后 4 位」才能
    上传/提交；不匹配一律 403 + 明确话术 + 安全留痕，⛔ 不做「只提示不拦」。
    """

    def _issue(self, tmp_db, client, sender):
        enable_all(tmp_db)
        client.post("/api/callback", json=callback_payload("打卡"))
        return extract_token(sender.sent[-1]["text"])

    def test_info_exposes_holder_name(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        body = client.post("/api/h5/checkin/info", json={"token": token}).json()
        assert body["ok"] is True
        assert body["require_verify"] is True
        # 名单里没有该成员时后端回空串（页面回落到「该成员」占位）
        assert "holder_name" in body

    def test_upload_rejects_other_holder(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        resp = client.post("/api/h5/checkin/upload",
                           files={"file": ("a.jpg", JPEG_BYTES,
                                           "image/jpeg")},
                           data={"token": token, "verify": "0000"
                                 if HOLDER_TAIL != "0000" else "1111"})
        assert resp.status_code == 403
        assert resp.json()["reason"] == "holder_mismatch"
        assert "专属链接" in resp.json()["message"]
        events = tmp_db._conn.execute(
            "SELECT * FROM security_event WHERE event_type='holder_mismatch'"
        ).fetchall()
        assert len(events) == 1

    def test_upload_rejects_missing_verify(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        resp = client.post("/api/h5/checkin/upload",
                           files={"file": ("a.jpg", JPEG_BYTES,
                                           "image/jpeg")},
                           data={"token": token})
        assert resp.status_code == 403
        assert resp.json()["reason"] == "holder_mismatch"

    def test_submit_rejects_other_holder(self, tmp_db):
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        up = client.post("/api/h5/checkin/upload",
                         files={"file": ("a.jpg", JPEG_BYTES, "image/jpeg")},
                         data={"token": token, "verify": HOLDER_TAIL})
        ref = up.json()["image_ref"]
        wrong = "0000" if HOLDER_TAIL != "0000" else "1111"
        resp = client.post("/api/h5/checkin/submit",
                           json={"token": token, "image_ref": ref,
                                 "verify": wrong})
        assert resp.status_code == 403
        assert resp.json()["reason"] == "holder_mismatch"

    def test_submit_rejects_other_holder_without_image(self, tmp_db):
        """即使没图，别人也不能借这个链接提交（先拦住身份，再谈业务）。"""
        app, _b, sender = make_app(tmp_db)
        client = TestClient(app)
        token = self._issue(tmp_db, client, sender)
        wrong = "0000" if HOLDER_TAIL != "0000" else "1111"
        resp = client.post("/api/h5/checkin/submit",
                           json={"token": token, "image_ref": "",
                                 "verify": wrong})
        assert resp.status_code == 403
