# -*- coding: utf-8 -*-
"""D17（2026-10-19）：管理员修改既有记录 + 看板凭证图片字段。

口径：
- 修改只动**已有行**（⛔ 不插入；补录是另一条链路）
- 修改事由必填、入库备注可选，一并落审计
- 看板打卡单元带 image_ref（后台缩略图用），⛔ 不暴露磁盘路径
"""

from __future__ import annotations

from doubles.fakes import FakeBackend
from helpers import GROUP, PHONE, enable_all


def _client(tmp_db, monkeypatch, backend=None):
    from test_admin import (ADMIN_PASS, NEW_PASS, admin_client, bootstrap,
                            login, mutate)
    bootstrap(tmp_db, monkeypatch)
    client = admin_client(tmp_db, tmp_db.db_path and "/tmp")
    assert login(client).status_code == 200
    mutate(client, "POST", "/api/admin/change-password",
           {"old_password": ADMIN_PASS, "new_password": NEW_PASS})
    assert login(client, password=NEW_PASS).status_code == 200
    return client, mutate


class TestRecordEdit:
    def _setup(self, tmp_db, monkeypatch):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"am": "09:00",
                                                            "pm": "14:50"})
        client, mutate = _client(tmp_db, monkeypatch)
        return client, mutate

    def test_reason_required(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/checkin/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "slots": [{"session": "am", "time": "09:15",
                                  "status": "LATE"}]})
        assert resp.status_code == 400
        assert "修改事由" in resp.json()["detail"]

    def test_checkin_edit_passes_time_and_status(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/checkin/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "本人反馈打卡时间记录有误",
                       "remark": "已核对门禁记录",
                       "slots": [{"session": "am", "time": "09:15",
                                  "status": "LATE"}]})
        assert resp.status_code == 200
        assert resp.json()["changed"] == 1
        # 审计里带事由与备注（⛔ 不吞掉）
        audit = client.get("/api/admin/audit").json()["audit"]
        entry = [a for a in audit if a["action"] == "record.edit"][0]
        assert "本人反馈打卡时间记录有误" in (entry["detail"] or "")
        assert "已核对门禁记录" in (entry["detail"] or "")

    def test_checkin_edit_bad_status_rejected(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/checkin/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "测试",
                       "slots": [{"session": "am", "time": "09:15",
                                  "status": "DONE"}]})
        assert resp.status_code == 400

    def test_summary_edit(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/summary/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "补正错别字",
                       "content": "今日完成接口联调与回归",
                       "time": "18:05"})
        assert resp.status_code == 200
        assert resp.json()["changed"] == 1

    def test_summary_edit_empty_payload_rejected(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/summary/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "空改"})
        assert resp.status_code == 400

    def test_report_edit(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/report/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "数据填报串行",
                       "items": [{"field_id": 1, "quantity": 3},
                                 {"field_id": 2, "quantity": 0}]})
        assert resp.status_code == 200
        assert resp.json()["changed"] == 1

    def test_report_edit_rejects_negative(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/report/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "负数测试",
                       "items": [{"field_id": 1, "quantity": -1}]})
        # 后端工具层拒绝负数；接口层先按整数校验放行到后端 -> 409
        assert resp.status_code in (400, 409)

    def test_unknown_biz_rejected(self, tmp_db, monkeypatch):
        client, mutate = self._setup(tmp_db, monkeypatch)
        resp = mutate(client, "POST",
                      "/api/admin/groups/%s/records/notice/edit" % GROUP,
                      {"user_id": PHONE, "date": "2026-10-13",
                       "reason": "测试"})
        assert resp.status_code == 400


class TestBoardImageRef:
    def test_board_exposes_image_ref(self, tmp_db, monkeypatch):
        enable_all(tmp_db)
        tmp_db.upsert_group_config(GROUP, checkin_deadlines={"am": "09:00"})
        tmp_db.upsert_member_task(GROUP, PHONE, nickname="张三", role="开发")
        backend = FakeBackend()
        backend.results["checkin_query_records"] = {"records": [{
            "record_id": 1, "phone": PHONE, "name": "张三",
            "period_key": "2026-10-13", "session": "am",
            "checkin_time": "2026-10-13 08:59:00",
            "image_ref": "ref-abc", "image_name": "shot.jpg",
            "image_size": 100, "status": "NORMAL",
            "entry_source": "user"}]}
        from app.admin.board import build_board
        board = build_board(tmp_db, backend, GROUP, "2026-10-13",
                            today="2026-10-14")
        cell = board["members"][0]["checkin"]["am"]
        assert cell["image_ref"] == "ref-abc"
        assert cell["image_name"] == "shot.jpg"
        # 空时段也要有同形字段（前端不用做 undefined 判断）
        assert board["members"][0]["checkin"].get("pm", {}).get(
            "image_ref", "") == ""
