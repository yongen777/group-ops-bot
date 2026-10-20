# -*- coding: utf-8 -*-
"""D3-C 本地视觉验收预览（⛔ 临时进程：不碰服务器、不碰真实库）。
stub 后端返回空记录；仅用于浏览器截图核对 Tabler 布局。
2026-10-15 起绑 0.0.0.0（局域网验收用）；种子群默认关闭，见 SEED_DEMO。"""
import os
import sys
import tempfile
import uvicorn
from pathlib import Path

sys.path.insert(0, "D:/group-ops/group-ops-bot")

from app.admin import auth as admin_auth
from app.admin.server import create_admin_app
from app.config import BotConfig
from app.state import StateStore
from app.web.sender import ReplySender


class StubBackend:
    def user_upsert(self, **kw): return {"user_id": 1, "created": True}
    def user_get(self, **kw): return {"user": None}
    def checkin_submit(self, **kw): return {"status": "NORMAL"}
    def checkin_query_status(self, **kw): return {"completed": False}
    def checkin_query_records(self, **kw): return {"records": []}
    def summary_save(self, **kw): return {"record_id": 1}
    def summary_query_status(self, **kw): return {"completed": False}
    def summary_query_records(self, **kw): return {"records": []}
    def report_submit(self, **kw): return {"record_id": 1}
    def report_query_status(self, **kw): return {"completed": False}
    def report_query_records(self, **kw): return {"records": []}
    def op_query_by_request(self, **kw): return {"found": False}
    def close(self): pass


class StubSender(ReplySender):
    def reply(self, reply_url, text): return True


tmp = Path(tempfile.mkdtemp(prefix="gops-preview-"))
store = StateStore(tmp / "preview.db")
# 演示种子群默认不种（GOPS_PREVIEW_SEED=1 才种）——2026-10-15 用户指令：
# 接入真实群聊测试前恢复干净状态，本地预览库不留任何测试群
SEED_DEMO = os.environ.get("GOPS_PREVIEW_SEED", "").strip() == "1"
if SEED_DEMO:
    store.create_group("d2-acceptance-test", "阶段D验收测试群（非真实群）")
    store.upsert_member_task("d2-acceptance-test", "17313771297", "郑佳", None, True)
    store.upsert_group_config(
        "d2-acceptance-test",
        biz_switches={"checkin": True, "summary": True, "report": True,
                      "reminder": True, "statistics": True},
        period_type="daily",
        checkin_deadlines={"slots": [
            {"id": "am", "name": "上午", "start": "09:00", "end": "12:00"},
            {"id": "pm", "name": "下午", "start": "14:00", "end": "18:00"}]})
    store.ensure_robot_instance("qmx-preview-group-001")

# ⭐ D30（2026-10-20）：env 引导已移除，预览数据直接建号（本地临时库）
admin_auth.create_admin(store, "preview", "preview-pass-1")

cfg = BotConfig(callback_port=18002, admin_port=18003,
                external_base_url="http://127.0.0.1:18002",
                tz_name="Asia/Shanghai", data_dir=str(tmp), mcp=None)
app = create_admin_app(state=store, backend=StubBackend(),
                       sender=StubSender(), config=cfg)
print("preview data dir:", tmp)
# 2026-10-15：按用户指令绑 0.0.0.0 —— 同一局域网下其他人可直接访问
# （http://<本机局域网IP>:18099）；Windows 防火墙需放行 18099 入站
uvicorn.run(app, host="0.0.0.0", port=18099, log_level="warning")
