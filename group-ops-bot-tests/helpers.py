# -*- coding: utf-8 -*-
"""业务处理器 / Web 层测试共享辅助（⛔ 只存在于测试目录，§2.8）。"""

from __future__ import annotations

import re
import secrets
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BOT_ROOT = Path(__file__).resolve().parent.parent / "group-ops-bot"
if str(BOT_ROOT) not in sys.path:
    sys.path.insert(0, str(BOT_ROOT))

from app.config import BotConfig  # noqa: E402
from app.handlers.base import HandlerContext  # noqa: E402
from app.tokens import token_id_of  # noqa: E402

TZ = ZoneInfo("Asia/Shanghai")
GROUP = "G100"
PHONE = "22644212509"
OTHER_PHONE = "22644212510"

# 令牌提取（⛔ 不 split：链接话术带「30 分钟有效」尾注，split 会把尾注
# 一起吞进令牌值；token_urlsafe 字符集 = A-Za-z0-9_-）
_TOKEN_RE = re.compile(r"#t=([A-Za-z0-9_-]+)")


def extract_token(reply: str) -> str:
    """从命令回复中提取 H5 令牌（打卡 / 报数链接共用）。"""
    match = _TOKEN_RE.search(reply or "")
    assert match, "no token in reply: %r" % (reply or "")[:120]
    return match.group(1)


def make_upload(store, token_value: str, *, group_id: str = GROUP,
                phone: str = PHONE, file_name: str = "checkin.jpg",
                mime: str = "image/jpeg", size: int = 2048,
                period_key: str = "2026-10-13") -> str:
    """造一条与令牌绑定的上传凭证（§4.11.4 D4 定夺后的 image_ref 真源）。

    ⛔ 不落真实文件：提交链路只查凭证行，本体落盘由上传端点负责。
    """
    ref = "up-" + secrets.token_hex(8)
    store.put_upload(
        ref=ref, token_id=token_id_of(token_value), group_id=group_id,
        phone=phone, file_name=file_name, mime=mime, size=size,
        rel_path="uploads/%s/%s/%s.jpg" % (group_id, period_key, ref))
    return ref


def make_config(data_dir) -> BotConfig:
    return BotConfig(
        callback_port=18002, admin_port=18003,
        external_base_url="http://127.0.0.1:18002",
        tz_name="Asia/Shanghai", data_dir=str(data_dir),
        daily_cutoff=(19, 0), mcp=None)


def make_ctx(store, backend, config, now=None, group_id=GROUP, phone=PHONE,
             text="", reply_url="http://reply.test/x", sender=None
             ) -> HandlerContext:
    return HandlerContext(
        group_id=group_id, phone=phone, name="测试用户",
        platform_user_id="puid-1", text=text, state=store, backend=backend,
        config=config, now=now or datetime(2026, 10, 13, 10, 0, tzinfo=TZ),
        reply_url=reply_url, sender=sender)


def enable_all(store, group_id=GROUP, period_type="daily") -> None:
    """开启全部业务 + 配宽松打卡时间段（多区间格式，验收项 2）。

    ⚠️ 区间配 00:00~12:00 / 12:00~23:59 是**测试夹具**默认值：让既有
    happy-path 用例聚焦原被测行为（12:00 分界与旧口径一致，am/pm 额度键不变）；
    截止阻断 / 免图直提 / 状态 / 补录由 test_checkin_phase2.py 专门覆盖
    （那里配产品默认 09:00 / 14:50）。
    """
    store.create_group(group_id, "测试群")
    store.upsert_group_config(
        group_id,
        biz_switches={"checkin": True, "summary": True, "report": True,
                      "reminder": True, "statistics": True},
        period_type=period_type,
        checkin_deadlines={"slots": [
            {"id": "am", "name": "上午", "start": "00:00", "end": "12:00"},
            {"id": "pm", "name": "下午", "start": "12:00", "end": "23:59"},
        ]})


def setup_fields(store, group_id=GROUP) -> None:
    store.upsert_field(group_id, 1, "bug修复数", None, ["开发"], True, 1)
    store.upsert_field(group_id, 2, "代码行数", "行", ["开发"], True, 2)
