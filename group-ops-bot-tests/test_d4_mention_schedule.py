# -*- coding: utf-8 -*-
"""D4 专项测试：真正 @ / 提醒时刻 / 完成人员过滤 / MCP 会话复用。

对应用户指令（2026-10-16）：
- 一：机器人回复必须真正 @（isMentioned / mentionType /
  mentionedMobileList / groupId），⛔ 禁止靠 content 文本假装 @
- 四：提醒时刻 = 各时段配置的提醒时间点（2026-10-20 起；⛔ 不再固定等于时段开始）
- 五：提醒对象 = 严格按完成状态（已完成 ⛔ 不 @；全完成 ⛔ 不发送；
  时段已结束 ⛔ 不提醒）
- D4-2：MCP 连接复用（首次建立 / 后续复用 / 失效自动重建）
"""

from __future__ import annotations

import inspect
from datetime import datetime

from app.backend.mcp_adapter import McpSseAdapter
from app.handlers import reminder as reminder_handler
from app.platform.qmx import QmxAdapter
from helpers import GROUP, OTHER_PHONE, PHONE, TZ

# ---------- 一：真正 @ 的载荷构造 ----------

GROUP_ID = "2109573070744977410"


def _resp_ok(captured):
    import httpx

    class R:
        status_code = 200
        text = '{"ok":true,"code":200,"message":"成功"}'

        def json(self):
            return {"ok": True, "code": 200}

    def fake(url, json=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        return R()

    return fake


class TestRealMentionPayload:
    """@ 必须走官方协议字段，⛔ 不得只在 content 里写 @姓名。"""

    def test_mention_phones_produce_official_fields(self, monkeypatch):
        import httpx
        captured = {}
        monkeypatch.setattr(httpx, "post", _resp_ok(captured))
        adapter = QmxAdapter(url_resolver=lambda g: "http://x/webhook/send?key=k")
        ok, _ = adapter.push_text(GROUP_ID, "@郑佳",
                                  mention_phones=[PHONE, OTHER_PHONE])
        assert ok is True
        msg = captured["json"]["textMsg"]
        assert msg["isMentioned"] is True
        assert msg["mentionType"] == 2
        assert msg["mentionedMobileList"] == [PHONE, OTHER_PHONE]
        assert msg["groupId"] == GROUP_ID
        assert captured["json"]["type"] == "text"

    def test_no_phones_is_plain_text(self, monkeypatch):
        """无 @ 对象时不得带 isMentioned（⛔ 普通回复不假装 @）。"""
        import httpx
        captured = {}
        monkeypatch.setattr(httpx, "post", _resp_ok(captured))
        adapter = QmxAdapter(url_resolver=lambda g: "http://x/webhook/send?key=k")
        ok, _ = adapter.push_text(GROUP_ID, "普通文本", mention_phones=[])
        assert ok is True
        msg = captured["json"]["textMsg"]
        assert "isMentioned" not in msg
        assert "mentionedMobileList" not in msg

    def test_empty_phone_filtered_out(self, monkeypatch):
        """空值不得进 mentionedMobileList（⛔ 只放真实手机号）。"""
        import httpx
        captured = {}
        monkeypatch.setattr(httpx, "post", _resp_ok(captured))
        adapter = QmxAdapter(url_resolver=lambda g: "http://x/webhook/send?key=k")
        adapter.push_text(GROUP_ID, "x", mention_phones=[PHONE, None, ""])
        assert captured["json"]["textMsg"]["mentionedMobileList"] == [PHONE]


# ---------- 四：提醒时刻 = 各时段配置的提醒时间点（D26 起） ----------

SLOTS_TWO = {"slots": [
    {"id": "am", "name": "上午", "start": "09:00", "end": "12:00",
     "remind_at": "09:10"},
    {"id": "pm", "name": "下午", "start": "14:00", "end": "18:00",
     "remind_at": "14:10"},
]}


def _row(slots):
    import json
    return {"checkin_deadlines": json.dumps(slots)}


class TestReminderMomentUsesRemindAt:
    """提醒时刻 = 该时段配置的 remind_at（⭐ 2026-10-20 用户方案）。

    旧口径是「提醒 = 时段开始」，用户反馈「人还没开工就被催」；
    现改为每时段单独配置、必须 开始 < remind_at < 结束（本夹具 09:10/14:10）。
    """

    def test_fires_at_0910(self):
        at = datetime(2026, 10, 16, 9, 10, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == ["am"]

    def test_fires_at_1410(self):
        at = datetime(2026, 10, 16, 14, 10, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == ["pm"]

    def test_not_fire_at_slot_start(self):
        """⛔ 时段开始那一刻不再触发（这正是用户要改掉的观感）。"""
        at = datetime(2026, 10, 16, 9, 0, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == []

    def test_not_fire_at_0930(self):
        """⛔ 09:30 不再触发（这正是用户报的「配 09:00 却 09:30 才提醒」）。"""
        at = datetime(2026, 10, 16, 9, 30, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == []

    def test_not_fire_at_1430(self):
        at = datetime(2026, 10, 16, 14, 30, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == []

    def test_window_covers_scheduler_granularity(self):
        """窗口必须 > 调度间隔(60s)，保证开始那一刻必被某轮覆盖。"""
        assert reminder_handler.REMINDER_TRIGGER_WINDOW_MINUTES > 1

    def test_ended_slot_not_reminded(self):
        """指令五：时段已结束 -> 不再提醒该时段。"""
        at = datetime(2026, 10, 16, 18, 30, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == []

    def test_am_only_once_at_pm_moment(self):
        """多时段：14:10 那轮只提醒 pm，⛔ 不重复提醒 am。"""
        at = datetime(2026, 10, 16, 14, 12, tzinfo=TZ)
        assert reminder_handler._checkin_reminder_times(
            _row(SLOTS_TWO), at) == ["pm"]


# ---------- 五：提醒对象按完成状态过滤 ----------

class TestReminderTargets:
    """已完成 ⛔ 不 @；全完成 ⛔ 不发送。"""

    def test_incomplete_only(self, tmp_db):
        from test_reminder_stats import (ReminderBackend,
                                         setup_group_with_required)
        setup_group_with_required(tmp_db)
        backend = ReminderBackend(completed={(PHONE, "checkin", "am")})
        ctx = _ctx(tmp_db, backend, datetime(2026, 10, 16, 9, 1, tzinfo=TZ))
        targets = reminder_handler.compute_incomplete(
            ctx, GROUP, "checkin", "2026-10-16", sessions=["am"])
        assert [t.phone for t in targets] == [OTHER_PHONE]

    def test_all_completed_returns_empty(self, tmp_db):
        from test_reminder_stats import (ReminderBackend,
                                         setup_group_with_required)
        setup_group_with_required(tmp_db)
        both = {(PHONE, "checkin", "am"), (OTHER_PHONE, "checkin", "am")}
        backend = ReminderBackend(completed=both)
        ctx = _ctx(tmp_db, backend, datetime(2026, 10, 16, 9, 1, tzinfo=TZ))
        assert reminder_handler.compute_incomplete(
            ctx, GROUP, "checkin", "2026-10-16", sessions=["am"]) == []

    def test_all_completed_sends_nothing(self, tmp_db):
        """全完成 -> tick 不产生任何推送（⛔ 不空发提醒）。"""
        from test_reminder_stats import (ReminderBackend,
                                         setup_group_with_required)
        setup_group_with_required(tmp_db)
        both = {(PHONE, "checkin", "am"), (OTHER_PHONE, "checkin", "am")}
        pushed = []

        def push(group_id, text, mention_phones=None):
            pushed.append((text, mention_phones))
            return True, ""

        backend = ReminderBackend(completed=both)
        reminder_handler.tick(tmp_db, backend, push,
                              datetime(2026, 10, 16, 9, 1, tzinfo=TZ))
        assert pushed == []

    def test_reminder_push_has_no_platform_mention(self, tmp_db):
        """⛔ 2026-10-17 用户指令：提醒推送不再带平台 @（mentionedMobileList）
        —— 消息开头那个 @ 已按要求「移除第一个，保留一个」，只保留文案内
        的未完成者名单（@姓名(上午)）。⛔ 不得把 mention_phones 加回来。"""
        from test_reminder_stats import ReminderBackend, setup_group_with_required
        setup_group_with_required(tmp_db)
        backend = ReminderBackend(completed={(PHONE, "checkin", "am")})
        captured = {}

        def push(group_id, text, mention_phones=None):
            captured["phones"] = mention_phones
            captured["text"] = text
            return True, ""

        reminder_handler.tick(tmp_db, backend, push,
                              datetime(2026, 10, 16, 0, 1, tzinfo=TZ))
        # 平台 @ 已移除：push 不再收到 mention_phones
        assert captured["phones"] is None
        # 文案内仍保留未完成者名单（用户「保留一个就行了」的那一个），
        # 且只含未完成者（PHONE 已完成 -> 不出现在名单里）
        assert OTHER_PHONE[-4:] in captured["text"]
        # ⭐ 2026-10-20：名单里 ⛔ 不再带「(上午)」，时段写进标题
        assert "【打卡·上午】" in captured["text"]
        assert "(上午)" not in captured["text"]
        assert PHONE[-4:] not in captured["text"]

    def test_single_merged_message_not_one_per_target(self, tmp_db):
        """合并成一条：多个未完成者只发 1 条消息（原实现会每人一条）。"""
        from test_reminder_stats import ReminderBackend, setup_group_with_required
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        pushed = []

        def push(group_id, text, mention_phones=None):
            pushed.append(text)
            return True, ""

        reminder_handler.tick(tmp_db, backend, push,
                              datetime(2026, 10, 16, 0, 1, tzinfo=TZ))
        assert len(pushed) == 1

    def test_message_contains_slot_window_and_deadline(self, tmp_db):
        """指令三：文案含时段与截止时间。"""
        from test_reminder_stats import ReminderBackend, setup_group_with_required
        setup_group_with_required(tmp_db)
        backend = ReminderBackend()
        pushed = []

        def push(group_id, text, mention_phones=None):
            pushed.append(text)
            return True, ""

        reminder_handler.tick(tmp_db, backend, push,
                              datetime(2026, 10, 16, 0, 1, tzinfo=TZ))
        assert pushed
        body = pushed[0]
        assert "【打卡·上午】未完成提醒" in body
        assert "⏰ 截止时间：" in body
        # 只催当时那一个时段 -> 截止时间只写时刻（时段名在标题里）
        assert "⏰ 截止时间：12:00" in body


def _ctx(store, backend, now):
    import pathlib
    from helpers import make_config, make_ctx
    return make_ctx(store, backend,
                    make_config(pathlib.Path(str(store.db_path)).parent),
                    now=now, group_id=GROUP, phone=PHONE)


# ---------- D4-2：MCP 会话复用与失效重建 ----------

class TestMcpSessionReuse:
    def test_session_cached_not_rebuilt_each_call(self):
        """第二次调用复用同一会话（⛔ 不每条消息重握手）。"""
        adapter = McpSseAdapter(base_url="http://x")

        class Alive:
            def alive(self):
                return True

            def is_closed(self):
                return False

        fake = Alive()
        adapter._session = fake
        assert adapter._ensure_session() is fake

    def test_dead_session_triggers_rebuild(self, monkeypatch):
        """失效会话必须重建（⛔ 否则 keepalive 超时后永久失败）。"""
        adapter = McpSseAdapter(base_url="http://x")

        class Dead:
            def alive(self):
                return False

            def is_closed(self):
                return True

            def close(self):
                pass

        adapter._session = Dead()
        built = []

        class Fresh:
            def __init__(self, client, sse_path):
                self.client = client
                self.sse_path = sse_path

            def alive(self):
                return True

            def start(self, timeout):
                built.append(self)

        monkeypatch.setattr("app.backend.mcp_adapter._SseSession", Fresh)
        got = adapter._ensure_session()
        assert isinstance(got, Fresh)
        assert len(built) == 1, "失效会话必须重建一次"
        assert adapter._session is got

    def test_idle_timeout_constant_used_by_alive(self):
        """alive() 必须把空闲超时算作失效（⛔ 死常量不得再存在）。"""
        from app.backend import mcp_adapter
        assert mcp_adapter._IDLE_TIMEOUT_SECONDS > 0
        src = inspect.getsource(mcp_adapter._SseSession.alive)
        assert "_IDLE_TIMEOUT_SECONDS" in src
