# -*- coding: utf-8 -*-
"""机器人侧持久层（§3.1：SQLite 只归机器人侧）。

存储内容（§3.1 / §2.4 / §2.5 / §2.7 / §2.12 / §4.7）：
- tokens            令牌记录（存哈希，⛔ 不存明文）
- submit_intent     提交意图（业务范围键 + 请求幂等键，调用 MCP 前落盘）
- receipt           回执（仅 A 确认成功后落；⛔ 本地记录不当成功证据，只是恢复凭据）
- recovery_task     C 类恢复任务（op.query_by_request 查证）
- pending_session   总结两轮待处理（§2.2）+ 覆盖确认待处理（10 分钟 TTL）
- group_config      群配置（群名 / 状态 / 业务开关 / 周期 / 截止 / 提醒规则 / 绑定管理员）
- group_seen        回调发现但未绑定的群（P0 群绑定：供后台一键绑定）
- qmx_robot_instance 量子机器人实例登记（v6：webhook 凭证 + 实例状态机，
  D3-B1；⚠️ 三门纪律——实例状态门 ≠ 群授权门 ≠ 业务开关门）
- group_member_task_config  应交人员名单（v4 起取代 checkin_required，§4.7）
- report_field_pool 报数字段池（§2.12 第 4 项，权威在机器人侧，⛔ 不入 MySQL）
- export_whitelist  导出白名单（2026-10-19 用户指令：单权化——权限只有
  ADMIN_ALL 一种，一键授全部管理员操作权限；授权对象限本群应交成员。
  ⛔ 推翻 v4 的按业务三权分权，旧数据由 _migrate v7->v8 合并）
- learned_user      入站学习人员（§2.7 组合方案 C）
- security_event    安全事件（未授权群等，§3.2 第 3 步）
- admin_user / admin_session / admin_audit / admin_login_attempt
  管理后台（:18003）鉴权与审计（§4.7：密码单向哈希、会话、限流、变更留痕）

纪律：
- 全部参数化查询，⛔ 不字符串拼接 SQL
- 单连接 + WAL（单副本部署，§2.9；多进程并发需求出现前不引连接池）
- 令牌/凭据 ⛔ 不进日志（本模块不打印任何行内容）
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse

LOG = logging.getLogger("group-ops.state")

SCHEMA_VERSION = 7  # v7：h5_upload（H5 图片上传凭证：ref -> 落盘文件绑定令牌，
# 2026-10-16 用户指令一 / §4.11.4 D4 定夺：方案 A 机器人中转 + 服务器磁盘；
# ⛔ 本体只落服务器磁盘，MySQL 侧仍只存 image_ref/元数据）
# v6：qmx_robot_instance（量子机器人实例登记：webhook 凭证 +
# 实例状态机 WAIT_BIND/ACTIVE/DISABLED，D3-B1；⛔ 不是业务授权）
# v5：group_seen（回调发现的未绑定群，P0 群绑定）、
# group_config.bind_admins（群绑定管理员留痕）、tokens pending_verify 状态
# （H5 提交中禁重复提交，用户指令 P0/P1 验收项）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id             TEXT PRIMARY KEY,          -- SHA-256(value)，⛔ 不存明文
    phone                TEXT NOT NULL,
    group_id             TEXT NOT NULL,
    biz                  TEXT NOT NULL,             -- checkin/summary/report
    period_key           TEXT NOT NULL,
    session              TEXT,                      -- 打卡时段 am/pm，其他 NULL
    created_at           TEXT NOT NULL,             -- ISO8601 UTC+8
    expires_at           TEXT NOT NULL,
    state                TEXT NOT NULL,             -- active/consumed/expired/revoked/pending_verify
    consumed_request_id  TEXT                       -- 成功提交的请求键（安全重试用）
);
CREATE INDEX IF NOT EXISTS ix_tokens_scope ON tokens(group_id, phone, biz, period_key);

CREATE TABLE IF NOT EXISTS submit_intent (
    request_id    TEXT PRIMARY KEY,                 -- 请求幂等键（§2.5.0）
    scope_key     TEXT NOT NULL,                    -- 业务记录范围键（§2.5.0）
    biz           TEXT NOT NULL,
    group_id      TEXT NOT NULL,
    phone         TEXT NOT NULL,
    period_key    TEXT NOT NULL,
    session       TEXT,
    payload_json  TEXT NOT NULL,                    -- 提交内容快照（⛔ 脱敏：无令牌）
    token_id      TEXT,                             -- 绑定的令牌 id（恢复判 A 后补消费，§2.4）
    status        TEXT NOT NULL,                    -- pending/confirmed/rejected/unknown
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_intent_scope ON submit_intent(scope_key, status);

CREATE TABLE IF NOT EXISTS receipt (
    request_id   TEXT PRIMARY KEY,
    scope_key    TEXT NOT NULL,
    biz          TEXT NOT NULL,
    record_id    TEXT NOT NULL,                     -- MCP Server 侧记录标识
    confirmed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_receipt_scope ON receipt(scope_key);

CREATE TABLE IF NOT EXISTS recovery_task (
    request_id  TEXT PRIMARY KEY,
    scope_key   TEXT NOT NULL,
    biz         TEXT NOT NULL,
    status      TEXT NOT NULL,                      -- pending/resolved
    due_at      TEXT NOT NULL,
    outcome     TEXT,                               -- resolved 后的最终归类 A/B
    attempts    INTEGER NOT NULL DEFAULT 0,         -- 已查证次数（unknown 才计数）
    escalated   INTEGER NOT NULL DEFAULT 0,         -- 是否已告警管理员（仍继续恢复）
    last_error  TEXT,                               -- 最近一次查证失败原因（脱敏）
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS pending_session (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_key   TEXT NOT NULL UNIQUE,               -- group+user_id+biz（一次一个待处理）
    group_id    TEXT NOT NULL,
    phone       TEXT NOT NULL,
    biz         TEXT NOT NULL,
    arg         TEXT,                               -- 总结的日报/周报参数
    ttl_minutes INTEGER NOT NULL DEFAULT 30,        -- §2.2 两轮 30 分钟；覆盖确认 10 分钟
    payload_json TEXT,                              -- 覆盖确认流携带的新正文（⛔ 脱敏）
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_config (
    group_id        TEXT PRIMARY KEY,
    group_name      TEXT,                                 -- 群名称（后台维护，§4.7）
    status          TEXT NOT NULL DEFAULT 'active',       -- active/disabled（禁用=未绑定处理）
    biz_switches    TEXT NOT NULL DEFAULT '{}',     -- {"checkin":true,...} 五业务开关
    period_type     TEXT NOT NULL DEFAULT 'daily',  -- 总结周期：daily/weekly
    daily_cutoff    TEXT NOT NULL DEFAULT '19:00',  -- 日报/报数截止 HH:MM
    checkin_deadlines TEXT NOT NULL DEFAULT '{"am":"09:00","pm":"14:50"}', -- 打卡两时段截止
    checkin_require_image INTEGER NOT NULL DEFAULT 1,     -- 1=需上图 H5；0=命令直提
    reminder_rules  TEXT NOT NULL DEFAULT '{}',
    bind_admins     TEXT,                                 -- 群绑定管理员手机号 JSON 数组（P0 绑定留痕）
    created_at      TEXT,
    updated_at      TEXT NOT NULL
);

-- 回调发现表（P0 群绑定）：平台回调里出现过但尚未建配置的群也记账，
-- 供管理后台「发现但未绑定」列表一键绑定（⛔ 不是授权：授权仍看 group_config）。
CREATE TABLE IF NOT EXISTS group_seen (
    group_id   TEXT PRIMARY KEY,
    last_phone TEXT,
    last_name  TEXT,
    msg_count  INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

-- 量子机器人实例登记（v6，D3-B1）：一个机器人 = 一个群 = 一个 webhook。
-- ⚠️ 三门纪律（D3-B0 §0）：本表 status 只决定将来 push/file 出站能否使用
-- 已登记 webhook；⛔ 不是业务授权（授权看 group_config.status=active），
-- ⛔ 也不影响 reply 回推（回推地址来自每条消息载荷的 reply_url）。
-- group_seen 仍是回调发现账本，本表只登记「群 ↔ 机器人实例」关系。
CREATE TABLE IF NOT EXISTS qmx_robot_instance (
    group_id              TEXT PRIMARY KEY,      -- 来自回调的真实群标识（≤64）
    display_name          TEXT,                  -- 管理员备注名（空 = 展示回退 group_id）
    webhook_base_url      TEXT,                  -- 发送网关基础地址（绑定后不可为空）
    webhook_key_encrypted TEXT,                  -- 凭证密文信封（可空；平台无签名机制时恒空）
    status                TEXT NOT NULL DEFAULT 'WAIT_BIND'
                          CHECK (status IN ('WAIT_BIND','ACTIVE','DISABLED')),
    -- ⭐ v11（2026-10-20）：最近一次**入站回调自带的回推地址**（只读观测值）。
    -- 用途：待绑定时后台可「一键填入」——平台每条消息都带着该群的回推地址，
    -- 不必让管理员去平台后台翻。⛔ 不等于已绑定（绑定仍要人工确认后写
    -- webhook_base_url）；⛔ 不进日志 / 不进审计 detail（地址内含平台 key）。
    last_callback_url     TEXT,
    last_callback_at      TEXT,
    created_at            TEXT,
    updated_at            TEXT NOT NULL
);

-- 应交人员名单（v4 起；user_id 当前存手机号——平台稳定自然键，§4.11.2 sys_user.phone）。
-- 规则：新增 -> 进入后续任务；删除 -> 停止未来任务；⛔ 历史业务记录不随配置删除（在 MySQL 侧）。
CREATE TABLE IF NOT EXISTS group_member_task_config (
    group_id  TEXT NOT NULL,
    user_id   TEXT NOT NULL,                        -- 用户标识（当前集成 = 手机号）
    nickname  TEXT,                                 -- 显示名（提醒文案/后台展示）
    enabled   INTEGER NOT NULL DEFAULT 1,           -- 0=停止未来任务（保留行可再启用）
    role      TEXT,                                 -- 开发/测试/产品（Q-BIZ2）
    source    TEXT NOT NULL DEFAULT 'manual',       -- manual/learned
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS member_phone_conflict (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    phone       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    detail      TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE IF NOT EXISTS group_member_removed (
    group_id   TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    removed_at TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id)
);

CREATE TABLE IF NOT EXISTS report_field_pool (
    group_id   TEXT NOT NULL,
    field_id   INTEGER NOT NULL,                    -- 机器人侧字段 id（不透明引用，§4.11.2）
    field_name TEXT NOT NULL,
    unit       TEXT,
    roles      TEXT NOT NULL DEFAULT '[]',          -- 适用角色 JSON 数组
    enabled    INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (group_id, field_id)
);

-- 报数字段 id 发号器（2026-10-20 用户指令：删除 = 物理删除）。
-- ⛔ 字段可被真删后，next_field_id 必须取本表（⛔ 不能再用
-- MAX(report_field_pool.field_id)+1 —— 删掉最大号后新字段会复用旧 id，
-- 而 MySQL report_item 按 field_id 归并历史列，复用会把已删字段的历史
-- 数量并入新字段）。last_id 只增不减，删字段 / 移除群聊都不回退。
CREATE TABLE IF NOT EXISTS report_field_seq (
    group_id   TEXT NOT NULL PRIMARY KEY,
    last_id    INTEGER NOT NULL
);

-- 导出白名单（2026-10-19 用户指令：单权化）：permission_type ∈ ADMIN_ALL
-- （一键授全部管理员操作权限，当前即一键导出打卡/总结/报数三表）。
-- 授权对象限本群应交成员（group_member_task_config，后台下拉选择 + API 校验）。
-- ⛔ 推翻 v4 按业务三权分权（EXPORT_CHECKIN/SUMMARY/REPORT），旧数据由
-- _migrate v7->v8 合并为每人一行 ADMIN_ALL。
CREATE TABLE IF NOT EXISTS export_whitelist (
    group_id       TEXT NOT NULL,
    user_id        TEXT NOT NULL,                   -- 用户标识（当前集成 = 手机号）
    permission_type TEXT NOT NULL,                  -- ADMIN_ALL
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (group_id, user_id, permission_type)
);

CREATE TABLE IF NOT EXISTS learned_user (
    phone            TEXT PRIMARY KEY,
    name             TEXT,
    platform_user_id TEXT,
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL
);

-- H5 图片上传凭证（v7，§4.11.4 D4 定夺：方案 A 机器人中转 + 服务器磁盘）。
-- ref 由服务端签发（⛔ 不用客户端文件名）；行与令牌绑定，提交时校验
-- token_id 一致才认（防拿别人的 image_ref 冒领上传通道）。
-- ⛔ 本体只落服务器磁盘（rel_path 相对 data_dir），MySQL 侧仍只存
-- image_ref/元数据（ddl 未批准未改，§4.11.2）。
CREATE TABLE IF NOT EXISTS h5_upload (
    ref        TEXT PRIMARY KEY,                       -- 服务端签发的上传引用
    token_id   TEXT NOT NULL,                          -- 绑定令牌（SHA-256，⛔ 非明文）
    group_id   TEXT NOT NULL,
    phone      TEXT NOT NULL,
    file_name  TEXT NOT NULL,                          -- 客户端原始文件名（仅展示）
    mime       TEXT NOT NULL,                          -- 嗅探判定类型（⛔ 不信客户端声明）
    size       INTEGER NOT NULL,
    rel_path   TEXT NOT NULL,                          -- 相对 data_dir 的存储路径
    created_at TEXT NOT NULL,
    consumed   INTEGER NOT NULL DEFAULT 0              -- 已被成功提交引用（审计/清理用）
);
CREATE INDEX IF NOT EXISTS ix_h5_upload_token ON h5_upload(token_id);
CREATE INDEX IF NOT EXISTS ix_h5_upload_created ON h5_upload(created_at);

CREATE TABLE IF NOT EXISTS security_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type  TEXT NOT NULL,                      -- unauthorized_group/payload_error/...
    group_id    TEXT,
    phone       TEXT,
    detail      TEXT NOT NULL,                      -- ⛔ 脱敏：不含令牌/凭据/完整请求体
    created_at  TEXT NOT NULL
);

-- ---------- 管理后台（:18003，§4.7） ----------
-- 密码单向哈希（PBKDF2-SHA256，格式 pbkdf2_sha256$iterations$salt$hash）；
-- ⛔ 不存明文；⛔ 凭据不进前端代码与日志。
CREATE TABLE IF NOT EXISTS admin_user (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    username            TEXT NOT NULL UNIQUE,
    password_hash       TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 1, -- 初始凭据登录后强制改密
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    last_login_at       TEXT
);

-- 服务端会话（⛔ 不用浏览器可自签的 token；Cookie HttpOnly + SameSite）。
CREATE TABLE IF NOT EXISTS admin_session (
    token_hash   TEXT PRIMARY KEY,                  -- SHA-256(会话令牌)，⛔ 不存明文
    user_id      INTEGER NOT NULL,
    csrf_token   TEXT NOT NULL,                     -- 会话绑定 CSRF 令牌（防跨站）
    created_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    expires_at   TEXT NOT NULL,                     -- 绝对过期（12h）
    ip           TEXT
);
CREATE INDEX IF NOT EXISTS ix_admin_session_user ON admin_session(user_id);

-- 登录限流（按 用户名+IP）：失败计数 + 锁定期。
CREATE TABLE IF NOT EXISTS admin_login_attempt (
    username        TEXT NOT NULL,
    ip              TEXT NOT NULL,
    failed_count    INTEGER NOT NULL DEFAULT 0,
    first_failed_at TEXT,
    locked_until    TEXT,
    PRIMARY KEY (username, ip)
);

-- 变更审计（§4.7 第 3 条：操作时间与变更结果留痕）。
CREATE TABLE IF NOT EXISTS admin_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL,
    action      TEXT NOT NULL,                      -- 如 group.create / member.add
    target_type TEXT,                               -- group/member/whitelist/field/...
    target_id   TEXT,
    detail      TEXT,                               -- JSON（⛔ 脱敏：无凭据/令牌）
    result      TEXT NOT NULL,                      -- ok/rejected/error
    ip          TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_plan (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    TEXT NOT NULL,
    biz         TEXT NOT NULL,                      -- checkin/summary/report
    period_key  TEXT NOT NULL,
    phone       TEXT NOT NULL,
    session     TEXT,                               -- 打卡时段；其他业务 NULL
    status      TEXT NOT NULL,                      -- pending/sent/unsent
    reason      TEXT,                               -- unsent 原因（脱敏）
    source      TEXT NOT NULL DEFAULT 'auto',       -- auto 定时 / manual 管理员手动
    created_at  TEXT NOT NULL,
    sent_at     TEXT
);
-- 去重：同群同业务同周期同人（同时段）只计划一次；
-- session 为 NULL 时用 IFNULL 归并（SQLite 表达式唯一索引）
CREATE UNIQUE INDEX IF NOT EXISTS uq_reminder_plan
    ON reminder_plan(group_id, biz, period_key, phone, IFNULL(session, ''));
"""

PENDING_SESSION_TTL_MINUTES = 30  # §2.2：TTL 30 分钟
# 提醒冷却（用户指令第五阶段）：同群同业务同周期同人（同时段）
# 距上次提醒不足 5 分钟 -> ⛔ 不重复打扰；冷却已过可再提醒
REMINDER_COOLDOWN_MINUTES = 5
# 管理后台登录限流（§4.7）：同 用户名+IP 失败达上限即锁 LOGIN_LOCK_SECONDS
LOGIN_MAX_FAILURES = 5
LOGIN_LOCK_SECONDS = 15 * 60


def utcnow_iso() -> str:
    """本地时间 ISO8601（带时区，服务端口径 Asia/Shanghai）。"""
    from .period import TZ, now as _now
    return _now().isoformat(timespec="seconds")


def _ensure_aware(dt: datetime) -> datetime:
    """入参统一为 TZ 感知时间，防 naive/aware 混用比较炸 TypeError。

    时区集中配置（§2.3）：naive 入参一律按 Asia/Shanghai 解释。
    """
    from .period import TZ
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt


# ---------- 量子机器人实例校验器（v6，D3-B0 §1.1 字段规范） ----------
# 非法值一律 raise ValueError（admin API 转 400；⛔ 不静默修正用户输入）。

_WEBHOOK_URL_MAX = 512
_DISPLAY_NAME_MAX = 64


def normalize_webhook_url(value) -> str:
    """webhook 基址校验：scheme ∈ {http,https}；⛔ 禁 URL 内嵌 userinfo 凭据；
    去尾斜杠；≤512。http 仅测试期（API 层记 warning，本层不打印，§纪律）。"""
    if not isinstance(value, str):
        raise ValueError("webhook_base_url 必须是字符串")
    url = value.strip()
    if not url:
        raise ValueError("webhook_base_url 不能为空")
    if len(url) > _WEBHOOK_URL_MAX:
        raise ValueError("webhook_base_url 超长（>%d）" % _WEBHOOK_URL_MAX)
    if "@" in url:
        raise ValueError("webhook_base_url 禁止内嵌凭据（userinfo）")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("webhook_base_url 只支持 http/https")
    if not parsed.netloc:
        raise ValueError("webhook_base_url 缺少主机名")
    return url.rstrip("/")


def normalize_display_name(value) -> str:
    """管理员备注名校验：去前后空白后 1~64 字符，⛔ 不含换行。"""
    if not isinstance(value, str):
        raise ValueError("display_name 必须是字符串")
    name = value.strip()
    if not name:
        raise ValueError("display_name 不能为空")
    if len(name) > _DISPLAY_NAME_MAX:
        raise ValueError("display_name 超长（>%d）" % _DISPLAY_NAME_MAX)
    if "\n" in name or "\r" in name:
        raise ValueError("display_name 不允许换行")
    return name


class StateStore:
    """SQLite 状态存储（线程局部连接 + WAL，§2.9 单副本）。

    连接策略：FastAPI 同步端点在 anyio 线程池并发执行，定时提醒又是独立
    线程 —— 单连接跨线程共享会交错执行。改为**线程局部连接**（WAL 支持
    多连接并发读写，busy_timeout 兜底写冲突）；⛔ 不引连接池（单副本，
    与 §2.9 一致）。
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list = []
        self._conns_lock = threading.Lock()
        conn = self._new_conn()
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),))
        conn.commit()
        self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """schema 升级（v2 -> ... -> v6 -> v7，逐级幂等）。

        CREATE TABLE IF NOT EXISTS 不会给已存在的库加列/改结构，旧库
        （冒烟种子等）需显式迁移；每步先查 PRAGMA / 表存在性，可重复执行。
        """
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version >= 13:
            return

        # ---- v2 -> v3：submit_intent.token_id + recovery_task 查证计数 ----
        intent_cols = {r["name"] for r in
                       conn.execute("PRAGMA table_info(submit_intent)")}
        if "token_id" not in intent_cols:
            conn.execute("ALTER TABLE submit_intent ADD COLUMN token_id TEXT")
        task_cols = {r["name"] for r in
                     conn.execute("PRAGMA table_info(recovery_task)")}
        for col, ddl in (("attempts", "INTEGER NOT NULL DEFAULT 0"),
                         ("escalated", "INTEGER NOT NULL DEFAULT 0"),
                         ("last_error", "TEXT")):
            if col not in task_cols:
                conn.execute("ALTER TABLE recovery_task ADD COLUMN %s %s"
                             % (col, ddl))

        # ---- v3 -> v4 ----
        # 4.1 group_config 新列（群名/状态/打卡截止/需图/创建时间）
        gcols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(group_config)")}
        for col, ddl in (("group_name", "TEXT"),
                         ("status", "TEXT NOT NULL DEFAULT 'active'"),
                         ("checkin_deadlines",
                          "TEXT NOT NULL DEFAULT '{\"am\":\"09:00\",\"pm\":\"14:50\"}'"),
                         ("checkin_require_image",
                          "INTEGER NOT NULL DEFAULT 1"),
                         ("created_at", "TEXT")):
            if col not in gcols:
                conn.execute("ALTER TABLE group_config ADD COLUMN %s %s"
                             % (col, ddl))
        # 4.2 checkin_required -> group_member_task_config（数据保留）。
        # ⚠️ 两种形态都要处理：
        #  a) 旧库无新表 -> 直接 RENAME（含 RENAME COLUMN）
        #  b) _SCHEMA 的 CREATE IF NOT EXISTS 已在旧库上建过新结构空表
        #     （本进程 __init__ 先 executescript 后 migrate）-> 搬数据 + DROP 旧表
        has_old = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
            " AND name='checkin_required'").fetchone()["n"]
        has_new = conn.execute(
            "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table'"
            " AND name='group_member_task_config'").fetchone()["n"]
        if has_old and not has_new:
            conn.execute(
                "ALTER TABLE checkin_required RENAME TO group_member_task_config")
            conn.execute(
                "ALTER TABLE group_member_task_config"
                " RENAME COLUMN phone TO user_id")
            conn.execute(
                "ALTER TABLE group_member_task_config ADD COLUMN nickname TEXT")
            conn.execute(
                "ALTER TABLE group_member_task_config"
                " ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        elif has_old and has_new:
            conn.execute(
                "INSERT OR IGNORE INTO group_member_task_config"
                "(group_id, user_id, nickname, enabled, role, source, updated_at)"
                " SELECT group_id, phone, NULL, 1, role, source, updated_at"
                " FROM checkin_required")
            conn.execute("DROP TABLE checkin_required")
        # 4.3 export_whitelist 加 permission_type（PK 变更需重建表）。
        # 旧语义「可导出统计」= 三项业务导出权，迁移时逐条补齐三类权限。
        wcols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(export_whitelist)")}
        if "permission_type" not in wcols:
            conn.execute(
                "CREATE TABLE export_whitelist_v4 ("
                " group_id TEXT NOT NULL, user_id TEXT NOT NULL,"
                " permission_type TEXT NOT NULL, updated_at TEXT NOT NULL,"
                " PRIMARY KEY (group_id, user_id, permission_type))")
            old_rows = conn.execute(
                "SELECT group_id, phone, updated_at FROM export_whitelist"
            ).fetchall()
            for r in old_rows:
                for perm in ("EXPORT_CHECKIN", "EXPORT_SUMMARY",
                             "EXPORT_REPORT"):
                    conn.execute(
                        "INSERT OR IGNORE INTO export_whitelist_v4"
                        " VALUES(?,?,?,?)",
                        (r["group_id"], r["phone"], perm, r["updated_at"]))
            conn.execute("DROP TABLE export_whitelist")
            conn.execute(
                "ALTER TABLE export_whitelist_v4 RENAME TO export_whitelist")
        # 4.4 biz_switches 旧键 stats -> statistics（Phase 6 起统计改四导出命令）
        for r in conn.execute("SELECT group_id, biz_switches FROM group_config"
                              ).fetchall():
            try:
                switches = json.loads(r["biz_switches"] or "{}")
            except ValueError:
                continue
            if "stats" in switches and "statistics" not in switches:
                switches["statistics"] = switches.pop("stats")
                conn.execute(
                    "UPDATE group_config SET biz_switches=? WHERE group_id=?",
                    (json.dumps(switches, ensure_ascii=False), r["group_id"]))
        # 4.5 pending_session 加 ttl_minutes / payload_json（总结覆盖确认流）
        pcols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(pending_session)")}
        if "ttl_minutes" not in pcols:
            conn.execute(
                "ALTER TABLE pending_session ADD COLUMN ttl_minutes"
                " INTEGER NOT NULL DEFAULT 30")
        if "payload_json" not in pcols:
            conn.execute(
                "ALTER TABLE pending_session ADD COLUMN payload_json TEXT")

        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("4",))
        conn.commit()

        # ---- v4 -> v5（P0 群绑定 + H5 pending_verify）----
        # 5.1 group_config.bind_admins（群绑定管理员留痕；NULL = 后台建群无绑定记录）
        gcols5 = {r["name"] for r in
                  conn.execute("PRAGMA table_info(group_config)")}
        if "bind_admins" not in gcols5:
            conn.execute("ALTER TABLE group_config ADD COLUMN bind_admins TEXT")
        # 5.2 group_seen：_SCHEMA 的 CREATE IF NOT EXISTS 已在新库建表；
        #     旧库同样在本进程 __init__ 的 executescript 里建过，无需搬数据。
        #     tokens 的 pending_verify 是 state 列的新取值（⛔ 不是新列），
        #     旧行保持原状态，无需迁移。
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("5",))
        conn.commit()

        # ---- v5 -> v6（D3-B1：量子机器人实例登记表）----
        # 6.1 qmx_robot_instance：_SCHEMA 的 CREATE IF NOT EXISTS 已在新库建表；
        #     旧库同样在本进程 __init__ 的 executescript 里建过，无需搬数据。
        #     新表无历史行 —— 实例靠真实回调 T0 自然发现（⛔ 不预建）。
        #     ⛔ 非破坏性：不碰任何现有表，回滚 = 停用新代码路径（表留空挂账）。
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("6",))
        conn.commit()

        # ---- v6 -> v7（§4.11.4 D4 定夺：H5 图片上传凭证表）----
        # 7.1 h5_upload：与 v5->v6 同形态 —— _SCHEMA 的 CREATE IF NOT EXISTS
        #     已在新库建表；旧库同样在本进程 __init__ 的 executescript 里建过，
        #     无需搬数据（新功能无历史行）。⛔ 非破坏性：不碰任何现有表。
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("7",))
        conn.commit()

        # ---- v7 -> v8（2026-10-19 用户指令：白名单单权化）----
        # 8.1 export_whitelist 三权（EXPORT_CHECKIN/SUMMARY/REPORT）合并为
        #     单一 ADMIN_ALL（⛔ 推翻 v4.3 的按业务分权）。表结构不动
        #     （PK 仍是 group_id+user_id+permission_type，未来加权限类型
        #     免重建）；仅做数据合并：同一 (group_id, user_id) 多行三权
        #     -> 一行 ADMIN_ALL，updated_at 取 MAX（最近一次授权时间）。
        #     INSERT OR IGNORE：ADMIN_ALL 行已存在时保留不动（幂等）。
        # ⚠️ 同 v4 先例：temp 表聚合 -> 删旧行 -> 插新行，⛔ 不直接
        #     UPDATE（会把三行都改成 ADMIN_ALL 撞 PK 丢两行）。
        has_old_perms = conn.execute(
            "SELECT COUNT(*) AS n FROM export_whitelist WHERE permission_type"
            " IN ('EXPORT_CHECKIN','EXPORT_SUMMARY','EXPORT_REPORT')"
        ).fetchone()["n"]
        if has_old_perms:
            conn.execute(
                "CREATE TEMP TABLE wl_merge_v8 AS"
                " SELECT group_id, user_id, MAX(updated_at) AS updated_at"
                " FROM export_whitelist WHERE permission_type IN"
                " ('EXPORT_CHECKIN','EXPORT_SUMMARY','EXPORT_REPORT')"
                " GROUP BY group_id, user_id")
            conn.execute(
                "DELETE FROM export_whitelist WHERE permission_type IN"
                " ('EXPORT_CHECKIN','EXPORT_SUMMARY','EXPORT_REPORT')")
            conn.execute(
                "INSERT OR IGNORE INTO export_whitelist(group_id, user_id,"
                " permission_type, updated_at)"
                " SELECT group_id, user_id, 'ADMIN_ALL', updated_at"
                " FROM wl_merge_v8")
            conn.execute("DROP TABLE wl_merge_v8")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("8",))
        conn.commit()

        # ---- v8 -> v9（2026-10-20 用户指令：定时提醒不受手动提醒影响）----
        # 9.1 reminder_plan 加 source 列（auto 定时 / manual 手动）。
        #     旧行默认 'auto'（历史数据无法回判来源；当天旧行随周期翻篇失效）。
        #     判重口径改为：自动调度只被「自动已发」的行拦，
        #     手动提醒 ⛔ 不再吃掉当期定时提醒。
        rp_cols = {r["name"] for r in
                   conn.execute("PRAGMA table_info(reminder_plan)")}
        if "source" not in rp_cols:
            conn.execute(
                "ALTER TABLE reminder_plan ADD COLUMN source TEXT"
                " NOT NULL DEFAULT 'auto'")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("9",))
        conn.commit()

        # ---- v9 -> v10（2026-10-20 用户指令：字段删除改物理删除）----
        # 10.1 report_field_seq：字段 id 发号器。表由 _SCHEMA 的 CREATE IF
        #      NOT EXISTS 建好（新旧库都在），这里只补种：每个已有群按现有
        #      最大 field_id 起号，⛔ 不回退也不重复发号。
        #      ⛔ 非破坏性：不碰 report_field_pool 任何行。
        for r in conn.execute(
                "SELECT group_id, COALESCE(MAX(field_id), 0) AS m"
                " FROM report_field_pool GROUP BY group_id").fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO report_field_seq(group_id, last_id)"
                " VALUES(?,?)", (r["group_id"], int(r["m"])))
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("10",))
        conn.commit()

        # ---- v10 -> v11（2026-10-20 用户指令：webhook 加固）----
        # 11.1 qmx_robot_instance 加 last_callback_url / last_callback_at：
        #      存「最近一次入站回调自带的回推地址」，仅供后台待绑定行
        #      「一键填入」参考。⛔ 不是绑定、⛔ 不参与任何出站判定
        #      （出站仍只看 status + webhook_base_url）。
        #      旧库补列（新库已由 _SCHEMA 建好），无需搬数据。
        icols = {r["name"] for r in
                 conn.execute("PRAGMA table_info(qmx_robot_instance)")}
        for col in ("last_callback_url", "last_callback_at"):
            if col not in icols:
                conn.execute(
                    "ALTER TABLE qmx_robot_instance ADD COLUMN %s TEXT" % col)
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("11",))
        conn.commit()

        # ---- v11 -> v12：group_member_removed（⭐ D34-①B）----
        # 「被管理员移除」要留痕：否则 D34 的自愈会把刚删掉的人立刻补回来，
        # 移除形同虚设。旧库补表（新库已由 _SCHEMA 建好），无数据搬迁。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS group_member_removed ("
            " group_id TEXT NOT NULL, user_id TEXT NOT NULL,"
            " removed_at TEXT NOT NULL, PRIMARY KEY (group_id, user_id))")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("12",))
        conn.commit()

        # ---- v12 -> v13：member_phone_conflict（⭐ D36 撞号待办）----
        # 改号跨群撞号 / 实名填了别人的号 -> 落一条待办，后台「群成员」顶部提示，
        # 管理员核对后点「已处理」。⛔ 不用审计流水当待办（没法标记处理状态）。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS member_phone_conflict ("
            " id INTEGER PRIMARY KEY AUTOINCREMENT, group_id TEXT NOT NULL,"
            " phone TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT,"
            " created_at TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT)")
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", ("13",))
        conn.commit()

    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        with self._conns_lock:
            self._all_conns.append(conn)
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """当前线程的连接（首次使用时建立；测试可直接访问本属性）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        with self._conns_lock:
            conns, self._all_conns = self._all_conns, []
        for conn in conns:
            try:
                conn.close()
            except Exception:  # pragma: no cover - 关闭best-effort
                pass

    @contextmanager
    def tx(self):
        """显式事务（提交意图 + 回执的落盘顺序由调用方保证）。"""
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    # ---------- 令牌 ----------

    def put_token(self, record) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO tokens(token_id, phone, group_id, biz, period_key,"
            " session, created_at, expires_at, state, consumed_request_id)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record.token_id, record.phone, record.group_id, record.biz,
             record.period_key, record.session,
             record.created_at.isoformat(timespec="seconds"),
             record.expires_at.isoformat(timespec="seconds"),
             record.state, record.consumed_request_id))
        self._conn.commit()

    def get_token(self, token_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM tokens WHERE token_id=?", (token_id,)).fetchone()

    def consume_token(self, token_id: str, request_id: str) -> None:
        """成功提交后立即作废（§2.4：首次正式提交成功后立即标记完成）。"""
        self._conn.execute(
            "UPDATE tokens SET state='consumed', consumed_request_id=? WHERE token_id=?",
            (request_id, token_id))
        self._conn.commit()

    def mark_token_submitting(self, token_id: str) -> bool:
        """ACTIVE -> pending_verify（提交已发起、结论未定，§2.4 + 用户指令 P1）。

        返回 False 表示令牌不在 active（已消费 / 已提交中 / 已过期）——
        调用方据此放弃本次提交，⛔ 不因重复点击落第二条记录。
        """
        cur = self._conn.execute(
            "UPDATE tokens SET state='pending_verify' WHERE token_id=?"
            " AND state='active'", (token_id,))
        self._conn.commit()
        return cur.rowcount > 0

    def reactivate_token(self, token_id: str) -> None:
        """pending_verify -> active（明确失败 / 恢复判 B：用户可修正重试）。"""
        self._conn.execute(
            "UPDATE tokens SET state='active' WHERE token_id=?"
            " AND state='pending_verify'", (token_id,))
        self._conn.commit()

    # ---------- 提交意图 / 回执 ----------

    def put_intent(self, request_id: str, scope_key: str, biz: str, group_id: str,
                   phone: str, period_key: str, session: Optional[str],
                   payload: dict, status: str = "pending",
                   token_id: Optional[str] = None) -> None:
        now_iso = utcnow_iso()
        self._conn.execute(
            "INSERT OR REPLACE INTO submit_intent(request_id, scope_key, biz, group_id,"
            " phone, period_key, session, payload_json, token_id, status,"
            " created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (request_id, scope_key, biz, group_id, phone, period_key, session,
             json.dumps(payload, ensure_ascii=False), token_id, status,
             now_iso, now_iso))
        self._conn.commit()

    def update_intent_status(self, request_id: str, status: str) -> None:
        self._conn.execute(
            "UPDATE submit_intent SET status=?, updated_at=? WHERE request_id=?",
            (status, utcnow_iso(), request_id))
        self._conn.commit()

    def get_intent(self, request_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM submit_intent WHERE request_id=?", (request_id,)).fetchone()

    def has_successful_scope(self, scope_key: str) -> bool:
        """额度判定（§2.5.3）：业务记录范围键下是否已有成功记录。

        ⛔ 判定只认 intent.status='confirmed'（A 确认成功）与 receipt，
        ⛔ 不靠请求幂等键 —— 范围键与请求键的分离是硬约束（§2.5.0）。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM submit_intent"
            " WHERE scope_key=? AND status='confirmed'", (scope_key,)).fetchone()
        if row and row["n"] > 0:
            return True
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM receipt WHERE scope_key=?", (scope_key,)).fetchone()
        return bool(row and row["n"] > 0)

    def has_pending_scope(self, scope_key: str) -> bool:
        """挂起判定（用户指令 P1 验收项 5）：同范围是否有 pending/unknown 意图。

        C 类（unknown）期间 ⛔ 禁止同范围再发起独立提交 —— 否则 30 分钟窗口内
        同一操作可能落两条记录（pending 是落盘后未得结论的瞬态，一并挡住）。
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM submit_intent"
            " WHERE scope_key=? AND status IN ('pending','unknown')",
            (scope_key,)).fetchone()
        return bool(row and row["n"] > 0)

    def put_receipt(self, request_id: str, scope_key: str, biz: str,
                    record_id: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO receipt(request_id, scope_key, biz, record_id, confirmed_at)"
            " VALUES(?,?,?,?,?)",
            (request_id, scope_key, biz, record_id, utcnow_iso()))
        self._conn.commit()

    # ---------- C 类恢复任务 ----------

    def put_recovery_task(self, request_id: str, scope_key: str, biz: str,
                          due_at: datetime) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO recovery_task(request_id, scope_key, biz, status,"
            " due_at, created_at) VALUES(?,?,?,'pending',?,?)",
            (request_id, scope_key, biz,
             _ensure_aware(due_at).isoformat(timespec="seconds"),
             utcnow_iso()))
        self._conn.commit()

    def resolve_recovery_task(self, request_id: str, outcome: str) -> None:
        self._conn.execute(
            "UPDATE recovery_task SET status='resolved', outcome=?, resolved_at=?"
            " WHERE request_id=?",
            (outcome, utcnow_iso(), request_id))
        self._conn.commit()

    def bump_recovery_attempt(self, request_id: str, due_at: datetime,
                              last_error: Optional[str]) -> int:
        """查证得 unknown：attempts+1 并重排 due_at（指数退避由调用方算）。
        返回递增后的 attempts。"""
        self._conn.execute(
            "UPDATE recovery_task SET attempts=attempts+1, due_at=?, last_error=?"
            " WHERE request_id=?",
            (_ensure_aware(due_at).isoformat(timespec="seconds"),
             last_error, request_id))
        self._conn.commit()
        row = self._conn.execute(
            "SELECT attempts FROM recovery_task WHERE request_id=?",
            (request_id,)).fetchone()
        return int(row["attempts"]) if row else 0

    def reschedule_recovery(self, request_id: str, due_at: datetime,
                            last_error: Optional[str]) -> None:
        """查证本身失败（BackendError）：⛔ 不计 attempt（没得到结论），
        仅重排 due_at + 记原因。"""
        self._conn.execute(
            "UPDATE recovery_task SET due_at=?, last_error=? WHERE request_id=?",
            (_ensure_aware(due_at).isoformat(timespec="seconds"),
             last_error, request_id))
        self._conn.commit()

    def mark_recovery_escalated(self, request_id: str) -> None:
        """超 attempts/超龄：标记已告警（⛔ 任务仍 pending，继续恢复）。"""
        self._conn.execute(
            "UPDATE recovery_task SET escalated=1 WHERE request_id=?",
            (request_id,))
        self._conn.commit()

    def pending_recovery_tasks(self) -> Iterable[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM recovery_task WHERE status='pending'").fetchall()

    def due_recovery_tasks(self, now: Optional[datetime] = None) -> Iterable[sqlite3.Row]:
        """到期恢复任务。⛔ 不用字符串比较时间（naive/aware 后缀会错序），
        取回后在 Python 侧按 datetime 比较。"""
        from .period import TZ
        threshold = _ensure_aware(now) if now else datetime.now(TZ)
        due = []
        for row in self._conn.execute(
                "SELECT * FROM recovery_task WHERE status='pending'").fetchall():
            if _ensure_aware(datetime.fromisoformat(row["due_at"])) <= threshold:
                due.append(row)
        return due

    # ---------- 总结两轮待处理（§2.2，TTL 30 分钟） ----------

    def put_pending_session(self, group_id: str, user_id: str, biz: str,
                            arg: Optional[str], created_at: datetime,
                            ttl_minutes: int = PENDING_SESSION_TTL_MINUTES,
                            payload: Optional[dict] = None) -> None:
        scope_key = "%s|%s|%s" % (group_id, user_id, biz)
        expires_at = _ensure_aware(created_at) + timedelta(minutes=ttl_minutes)
        self._conn.execute(
            "INSERT OR REPLACE INTO pending_session(scope_key, group_id, phone, biz,"
            " arg, ttl_minutes, payload_json, created_at, expires_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (scope_key, group_id, user_id, biz, arg, ttl_minutes,
             json.dumps(payload, ensure_ascii=False) if payload else None,
             _ensure_aware(created_at).isoformat(timespec="seconds"),
             expires_at.isoformat(timespec="seconds")))
        self._conn.commit()

    def get_pending_session(self, group_id: str, user_id: str, biz: str,
                            now: Optional[datetime] = None) -> Optional[sqlite3.Row]:
        """取**未过期**的待处理会话；过期 / 不存在 -> None。

        now 可注入（测试确定性）；生产不传即用真实时钟（Asia/Shanghai）。

        ⚠️ 2026-10-17 用户指令六：过期行**不再在这里删除** —— 删除权归
        ``get_expired_pending_session``（它要把「过期」与「从未存在」
        区分开还给用户明确提示）。过期行停留无害：同一 scope 下一条命令
        的 put_pending_session 会 INSERT OR REPLACE 覆盖它。
        """
        from .period import TZ
        row = self._conn.execute(
            "SELECT * FROM pending_session WHERE scope_key=?",
            ("%s|%s|%s" % (group_id, user_id, biz),)).fetchone()
        if row is None:
            return None
        threshold = _ensure_aware(now) if now else datetime.now(TZ)
        if _ensure_aware(datetime.fromisoformat(row["expires_at"])) <= threshold:
            return None
        return row

    def peek_pending_session(self, group_id: str, user_id: str,
                             biz: str) -> Optional[sqlite3.Row]:
        """只读查看会话行（**不判过期、不删除**）。

        用途：路由层判断「是否有刚过期未消费的会话」—— 必须只看不删，
        删除权仍独家归 ``get_expired_pending_session``（否则超时话术
        永远取不到行，2026-10-17 踩过的坑）。
        """
        return self._conn.execute(
            "SELECT * FROM pending_session WHERE scope_key=?",
            ("%s|%s|%s" % (group_id, user_id, biz),)).fetchone()

    def drop_pending_session(self, group_id: str, user_id: str, biz: str) -> None:
        self._conn.execute(
            "DELETE FROM pending_session WHERE scope_key=?",
            ("%s|%s|%s" % (group_id, user_id, biz),))
        self._conn.commit()

    def get_expired_pending_session(self, group_id: str, user_id: str,
                                    biz: str,
                                    now: Optional[datetime] = None
                                    ) -> Optional[sqlite3.Row]:
        """取**已过期**的待处理会话（命中即删并返回该行；否则 None）。

        与 get_pending_session 的分工（⭐ 2026-10-17 用户指令六：
        两轮对话焦点过期要明确告知，⛔ 不静默失效）：
        - get_pending_session：过期返 None 但**不删**（「当作没有」）
        - 本方法：把「过期」与「从未存在」区分开 —— 等待超时的用户应得到
          「已超时，请重新发送命令」的明确提示，而非落入帮助文案
          （总结）或未绑定群静默分支（绑定，零反馈）；命中即删。

        now 可注入（测试确定性）；生产不传即用真实时钟（Asia/Shanghai）。
        """
        from .period import TZ
        row = self._conn.execute(
            "SELECT * FROM pending_session WHERE scope_key=?",
            ("%s|%s|%s" % (group_id, user_id, biz),)).fetchone()
        if row is None:
            return None
        threshold = _ensure_aware(now) if now else datetime.now(TZ)
        if _ensure_aware(datetime.fromisoformat(row["expires_at"])) > threshold:
            return None  # 未过期：调用方该用 get_pending_session
        self.drop_pending_session(group_id, user_id, biz)
        return row

    # ---------- 群配置 / 名单 / 字段池 / 白名单 ----------

    def upsert_group_config(self, group_id: str, **fields) -> None:
        allowed = ("group_name", "status", "biz_switches", "period_type",
                   "daily_cutoff", "checkin_deadlines", "checkin_require_image",
                   "reminder_rules", "bind_admins")
        cols, values = [], []
        for key in allowed:
            if key in fields and fields[key] is not None:
                value = fields[key]
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                cols.append(key)
                values.append(value)
        if not cols:
            return
        cols.append("updated_at")
        values.append(utcnow_iso())
        # UPSERT 的 DO UPDATE SET 用 excluded.* 形态（避免 SET 侧再吃一轮参数绑定）
        set_clause = ", ".join("%s=excluded.%s" % (c, c) for c in cols)
        self._conn.execute(
            "INSERT INTO group_config(group_id, %s) VALUES(?, %s)"
            " ON CONFLICT(group_id) DO UPDATE SET %s"
            % (",".join(cols), ",".join(["?"] * len(cols)), set_clause),
            [group_id] + values)
        self._conn.commit()

    def get_group_config(self, group_id: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM group_config WHERE group_id=?", (group_id,)).fetchone()

    # 五业务开关的权威键名（§2.12 / 用户指令第一阶段；statistics 覆盖旧 stats 键）
    BIZ_SWITCH_KEYS = ("checkin", "summary", "report", "reminder", "statistics")
    DEFAULT_SWITCHES = {k: True for k in BIZ_SWITCH_KEYS}

    def create_group(self, group_id: str, group_name: Optional[str] = None,
                     switches: Optional[dict] = None) -> None:
        """后台新建群（§4.7）：默认五业务全开 + active；已存在则只补名/改开关。"""
        merged = dict(self.DEFAULT_SWITCHES)
        if switches:
            for key in self.BIZ_SWITCH_KEYS:
                if key in switches and switches[key] is not None:
                    merged[key] = bool(switches[key])
        self.upsert_group_config(
            group_id, group_name=group_name, status="active",
            biz_switches=merged, created_at=utcnow_iso())

    def group_active(self, group_id: str) -> bool:
        """群是否处于 active（禁用的群按未绑定处理：回调抑制，§4.7）。"""
        row = self.get_group_config(group_id)
        if row is None:
            return False
        try:
            return (row["status"] or "active") == "active"
        except (IndexError, KeyError):  # pragma: no cover - 旧库无 status 列
            return True

    # ---------- 回调发现（P0 群绑定：未绑定群也记账，供后台一键绑定） ----------

    def note_group_seen(self, group_id: str, phone: Optional[str] = None,
                        name: Optional[str] = None) -> None:
        """记录一次回调可见的群（绑定判定仍只看 group_config，⛔ 本表不是授权）。"""
        now_iso = utcnow_iso()
        self._conn.execute(
            "INSERT INTO group_seen(group_id, last_phone, last_name, msg_count,"
            " first_seen, last_seen) VALUES(?,?,?,1,?,?)"
            " ON CONFLICT(group_id) DO UPDATE SET"
            " last_phone=COALESCE(excluded.last_phone, group_seen.last_phone),"
            " last_name=COALESCE(excluded.last_name, group_seen.last_name),"
            " msg_count=group_seen.msg_count+1, last_seen=excluded.last_seen",
            (group_id, phone, name, now_iso, now_iso))
        self._conn.commit()

    def list_unbound_groups(self) -> Iterable[sqlite3.Row]:
        """回调见过但没有 group_config 的群（后台「发现但未绑定」列表）。"""
        return self._conn.execute(
            "SELECT s.* FROM group_seen s LEFT JOIN group_config c"
            " ON c.group_id=s.group_id WHERE c.group_id IS NULL"
            " ORDER BY s.last_seen DESC").fetchall()

    # ---------- 量子机器人实例（v6，D3-B0 状态机；D3-B1 CRUD） ----------
    # 三门纪律：实例状态（本表）≠ 群授权（group_config.status）≠ 业务开关
    # （biz_switches）。reply 回推路径⛔ 不查本表（地址随消息 reply_url 来）。
    # 全部参数化查询；webhook_key_encrypted ⛔ 不进日志（本模块不打印行内容）。

    ROBOT_INSTANCE_STATUSES = ("WAIT_BIND", "ACTIVE", "DISABLED")

    def ensure_robot_instance(self, group_id: str) -> None:
        """T0 自动发现：首条未知 groupId 回调登记 WAIT_BIND 行。

        幂等（INSERT OR IGNORE）：重复回调零副作用，⛔ 不重置 display_name/status。
        纯记账：调用方必须 try/except 包裹——记账失败不得影响回调主流程。
        """
        now_iso = utcnow_iso()
        self._conn.execute(
            "INSERT OR IGNORE INTO qmx_robot_instance"
            "(group_id, status, created_at, updated_at) VALUES(?,?,?,?)",
            (group_id, "WAIT_BIND", now_iso, now_iso))
        self._conn.commit()

    def record_callback_url(self, group_id: str, reply_url: str) -> bool:
        """记账：最近一次入站回调自带的回推地址（⭐ v11，D30 加固）。

        用途只有一个 —— 后台「群实例绑定」待绑定行显示 + 一键填入候选值，
        省去管理员去平台后台翻地址。⛔ 不是绑定：不写 ``webhook_base_url``、
        ⛔ 不改 status、⛔ 不参与任何出站判定。

        地址校验走 ``normalize_webhook_url``（http/https、≤512、禁内嵌凭据）；
        不合法 / 为空 / 实例行不存在 -> 返回 False 且**不抛异常**
        （入站脏数据不得影响回调主流程）。
        """
        try:
            url = normalize_webhook_url(reply_url)
        except ValueError:
            return False
        cur = self._conn.execute(
            "UPDATE qmx_robot_instance SET last_callback_url=?,"
            " last_callback_at=? WHERE group_id=?",
            (url, utcnow_iso(), group_id))
        self._conn.commit()
        return cur.rowcount == 1

    def robot_push_ready(self, group_id: str) -> bool:
        """该群现在能否**主动推送**（提醒 / 导出文件）。

        唯一口径：实例 ``status='ACTIVE'`` 且 ``webhook_base_url`` 非空 ——
        与 ``platform/qmx.py`` 的出站守卫一致（那里拿不到地址就 fail-closed）。
        ⛔ 与回复（reply_url）无关：不 ready 也能正常回话。
        """
        row = self._conn.execute(
            "SELECT status, webhook_base_url FROM qmx_robot_instance"
            " WHERE group_id=?", (group_id,)).fetchone()
        if row is None:
            return False
        return row["status"] == "ACTIVE" and bool(row["webhook_base_url"])

    def get_robot_instance(self, group_id: str) -> Optional[sqlite3.Row]:
        """实例行（⭐ 2026-10-20 起附带 ``group_name``：群配置里的群名称）。

        群内两轮绑定只会把群名写进 ``group_config.group_name``，实例行的
        ``display_name``（后台备注名）仍是 NULL —— 后台列表按 display_name
        回退 group_id 时就会显示成一串数字（用户实测报障）。这里用 LEFT JOIN
        带出群名，由前端按 display_name -> group_name -> group_id 兜底，
        ⛔ 不把群名再复制一份写进实例行（否则后台改群名两边会不一致）。
        """
        return self._conn.execute(
            "SELECT i.*, g.group_name AS group_name"
            " FROM qmx_robot_instance i"
            " LEFT JOIN group_config g ON g.group_id = i.group_id"
            " WHERE i.group_id=?", (group_id,)).fetchone()

    def list_robot_instances(
            self, status: Optional[str] = None) -> Iterable[sqlite3.Row]:
        """后台列表（可按状态过滤；status 非法由调用方校验，此处仅透传）。

        ⭐ 同 ``get_robot_instance``：LEFT JOIN 带出 ``group_name`` 供前端兜底。
        """
        base = ("SELECT i.*, g.group_name AS group_name"
                " FROM qmx_robot_instance i"
                " LEFT JOIN group_config g ON g.group_id = i.group_id")
        if status:
            return self._conn.execute(
                base + " WHERE i.status=? ORDER BY i.updated_at DESC",
                (status,)).fetchall()
        return self._conn.execute(
            base + " ORDER BY i.updated_at DESC").fetchall()

    def bind_robot_instance(self, group_id: str, webhook_base_url: str,
                            display_name: Optional[str] = None,
                            webhook_key_encrypted: Optional[str] = None
                            ) -> bool:
        """T2 绑定激活：WAIT_BIND -> ACTIVE（guard = 状态必须是 WAIT_BIND）。

        返回 False = 无记录或状态不允许（API 转 404/409）；
        url / 备注名非法 -> ValueError（API 转 400，fail-closed 拒写）。
        """
        url = normalize_webhook_url(webhook_base_url)
        name = normalize_display_name(display_name) if display_name else None
        cur = self._conn.execute(
            "UPDATE qmx_robot_instance SET webhook_base_url=?,"
            " display_name=COALESCE(?, display_name),"
            " webhook_key_encrypted=COALESCE(?, webhook_key_encrypted),"
            " status='ACTIVE', updated_at=?"
            " WHERE group_id=? AND status='WAIT_BIND'",
            (url, name, webhook_key_encrypted, utcnow_iso(), group_id))
        self._conn.commit()
        return cur.rowcount == 1

    def update_robot_instance(self, group_id: str,
                              display_name: Optional[str] = None,
                              webhook_base_url: Optional[str] = None,
                              webhook_key_encrypted: Optional[str] = None
                              ) -> bool:
        """PUT 局部更新（仅 ACTIVE/WAIT_BIND 可改；⛔ 禁止清空 webhook_base_url
        —— 传空串 -> ValueError -> API 400）。返回 False = 无记录。"""
        sets, values = [], []
        if webhook_base_url is not None:
            sets.append("webhook_base_url=?")
            values.append(normalize_webhook_url(webhook_base_url))
        if display_name is not None:
            sets.append("display_name=?")
            values.append(normalize_display_name(display_name))
        if webhook_key_encrypted is not None:
            sets.append("webhook_key_encrypted=?")
            values.append(webhook_key_encrypted)
        if not sets:
            return self.get_robot_instance(group_id) is not None
        sets.append("updated_at=?")
        values.append(utcnow_iso())
        cur = self._conn.execute(
            "UPDATE qmx_robot_instance SET %s WHERE group_id=?"
            % ", ".join(sets), values + [group_id])
        self._conn.commit()
        return cur.rowcount == 1

    def set_robot_instance_status(self, group_id: str, status: str) -> bool:
        """T3 禁用（任意状态，幂等）/ T4 启用（guard = url 已设）。

        返回 False = 无记录（API 转 404）或启用时 url 未设（API 转 400）。
        """
        if status not in self.ROBOT_INSTANCE_STATUSES:
            raise ValueError("非法实例状态: %r" % (status,))
        if status == "ACTIVE":
            # 启用：仅 DISABLED -> ACTIVE；已 ACTIVE 且 url 在 = 幂等成功
            cur = self._conn.execute(
                "UPDATE qmx_robot_instance SET status='ACTIVE', updated_at=?"
                " WHERE group_id=? AND webhook_base_url IS NOT NULL"
                " AND status IN ('DISABLED','ACTIVE')",
                (utcnow_iso(), group_id))
        elif status == "DISABLED":
            cur = self._conn.execute(
                "UPDATE qmx_robot_instance SET status='DISABLED', updated_at=?"
                " WHERE group_id=?", (utcnow_iso(), group_id))
        else:  # WAIT_BIND 不对外提供（仅 T0 自动发现入口）
            raise ValueError("不允许手工设置为 WAIT_BIND")
        self._conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def normalized_switches(row) -> dict:
        """群配置行 -> 五开关归一化 dict（缺键 = False，fail-closed；旧 stats 键兼容）。"""
        switches = {}
        if row is not None:
            try:
                switches = json.loads(row["biz_switches"] or "{}")
            except (ValueError, TypeError):
                switches = {}
        if not isinstance(switches, dict):
            switches = {}
        if "stats" in switches and "statistics" not in switches:
            switches["statistics"] = switches.get("stats")
        return {key: bool(switches.get(key))
                for key in StateStore.BIZ_SWITCH_KEYS}

    # ---------- 应交人员（group_member_task_config，§4.7 / 用户指令第一阶段） ----------

    def upsert_member_task(self, group_id: str, user_id: str,
                           nickname: Optional[str] = None,
                           role: Optional[str] = None,
                           enabled: bool = True,
                           source: str = "manual") -> None:
        """新增/更新应交人员。新增 -> 进入后续任务；enabled=0 -> 停止未来任务
        （⛔ 不删历史业务记录：历史在 MySQL 侧，与本配置无关）。"""
        self._conn.execute(
            "INSERT INTO group_member_task_config(group_id, user_id, nickname,"
            " enabled, role, source, updated_at) VALUES(?,?,?,?,?,?,?)"
            " ON CONFLICT(group_id, user_id) DO UPDATE SET"
            " nickname=COALESCE(excluded.nickname, group_member_task_config.nickname),"
            " role=COALESCE(excluded.role, group_member_task_config.role),"
            " enabled=excluded.enabled, source=excluded.source,"
            " updated_at=excluded.updated_at",
            (group_id, user_id, nickname, 1 if enabled else 0, role, source,
             utcnow_iso()))
        self._conn.commit()

    def remove_member_task(self, group_id: str, user_id: str) -> None:
        """移出应交名单：停止未来任务；⛔ 历史数据不可删除（本操作只删配置行）。"""
        self._conn.execute(
            "DELETE FROM group_member_task_config WHERE group_id=? AND user_id=?",
            (group_id, user_id))
        self._conn.commit()

    # ---------- 「被移除」留痕（⭐ D34-①B，2026-10-22 用户指令） ----------
    # 目的：管理员点「移出群」后，D34 的自动补录⛔ 不许再把人还魂回来。
    # 管理员重新添加该成员时清掉留痕 = 解封。

    def mark_member_removed(self, group_id: str, user_id: str) -> None:
        """记一条「被管理员移出」痕迹（幂等）。"""
        self._conn.execute(
            "INSERT INTO group_member_removed(group_id, user_id, removed_at)"
            " VALUES(?,?,?) ON CONFLICT(group_id, user_id) DO UPDATE SET"
            " removed_at=excluded.removed_at",
            (group_id, user_id, utcnow_iso()))
        self._conn.commit()

    def is_member_removed(self, group_id: str, user_id: str) -> bool:
        """该用户是否被管理员移出过（未重新添加 = 仍在黑名单）。"""
        row = self._conn.execute(
            "SELECT 1 FROM group_member_removed WHERE group_id=? AND user_id=?",
            (group_id, user_id)).fetchone()
        return row is not None

    def clear_member_removed(self, group_id: str, user_id: str) -> None:
        """解除移除痕迹（管理员重新添加该成员时调用）。"""
        self._conn.execute(
            "DELETE FROM group_member_removed WHERE group_id=? AND user_id=?",
            (group_id, user_id))
        self._conn.commit()

    # ---------- 手机号撞号待办（⭐ D36） ----------

    def add_phone_conflict(self, group_id: str, phone: str, kind: str,
                           detail: str = "") -> int:
        """记一条撞号待办（同群同号同类型未处理时不重复堆叠）。

        kind: rename（改号撞号）/ realname（实名填了别人的号）
        """
        existing = self._conn.execute(
            "SELECT id FROM member_phone_conflict WHERE group_id=? AND phone=?"
            " AND kind=? AND resolved_at IS NULL", (group_id, phone, kind)
        ).fetchone()
        if existing:
            self._conn.execute(
                "UPDATE member_phone_conflict SET detail=?, created_at=?"
                " WHERE id=?", (detail, utcnow_iso(), existing["id"]))
            self._conn.commit()
            return int(existing["id"])
        cur = self._conn.execute(
            "INSERT INTO member_phone_conflict(group_id, phone, kind, detail,"
            " created_at) VALUES(?,?,?,?,?)",
            (group_id, phone, kind, detail, utcnow_iso()))
        self._conn.commit()
        return int(cur.lastrowid)

    def list_phone_conflicts(self, group_id: str,
                             only_open: bool = True) -> list:
        sql = ("SELECT * FROM member_phone_conflict WHERE group_id=?"
               + (" AND resolved_at IS NULL" if only_open else "")
               + " ORDER BY id DESC")
        return self._conn.execute(sql, (group_id,)).fetchall()

    def resolve_phone_conflict(self, group_id: str, conflict_id: int,
                               by: str = "admin") -> bool:
        """标记已处理（幂等）；返回是否命中未处理记录。"""
        cur = self._conn.execute(
            "UPDATE member_phone_conflict SET resolved_at=?, resolved_by=?"
            " WHERE id=? AND group_id=? AND resolved_at IS NULL",
            (utcnow_iso(), by, conflict_id, group_id))
        self._conn.commit()
        return cur.rowcount > 0

    def delete_group_cascade(self, group_id: str) -> dict:
        """彻底移除群接入（2026-10-19 用户指令「移除群聊」）。

        一次事务删净：机器人实例、群配置、应交名单、导出授权、报数字段池，
        并清理该群的调度残留（待办会话 / 提醒计划 / 未消费令牌 / 幂等意图），
        避免移除后仍出现僵尸推送或残留授权。

        ⛔ MySQL 侧历史业务记录不动（本方法只动机器人侧 SQLite）；
        ⛔ group_seen 保留 —— 群里再 @ 机器人时 T0 会重新登记 WAIT_BIND，
        满足「移除后重新接入」的语义。
        """
        counts: dict = {}
        # ⚠️ report_field_seq 故意不删：字段 id 只增不减，群移除后重新接入
        #    也不能回收旧号（MySQL report_item 按 field_id 归并历史列）。
        tables = ("export_whitelist", "report_field_pool",
                  "group_member_task_config", "reminder_plan",
                  "pending_session", "tokens", "submit_intent",
                  "qmx_robot_instance", "group_config")
        with self._conn:
            for table in tables:
                cur = self._conn.execute(
                    "DELETE FROM %s WHERE group_id=?" % table, (group_id,))
                counts[table] = cur.rowcount
        return counts

    def list_member_tasks(self, group_id: str,
                          enabled_only: bool = False) -> Iterable[sqlite3.Row]:
        sql = "SELECT * FROM group_member_task_config WHERE group_id=?"
        params: list = [group_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY user_id"
        return self._conn.execute(sql, params).fetchall()

    def get_member_task(self, group_id: str,
                        user_id: str) -> Optional[sqlite3.Row]:
        """取单个应交成员配置行（⛔ 不含业务记录）。"""
        return self._conn.execute(
            "SELECT * FROM group_member_task_config"
            " WHERE group_id=? AND user_id=?", (group_id, user_id)).fetchone()

    def is_member_paused(self, group_id: str, user_id: str) -> bool:
        """是否被「暂停考核」（enabled=0）。

        2026-10-19 用户口径：暂停考核 = 该成员在群内**不能执行业务操作**
        （打卡 / 总结 / 报数），不只是不再提醒。⛔ 不在名单里不当成暂停
        （名单外的人仍可提交，提交后由后台「一键添加」补录名单）。
        """
        row = self.get_member_task(group_id, user_id)
        if row is None:
            return False
        try:
            return not bool(row["enabled"])
        except (IndexError, KeyError):  # pragma: no cover - 旧库缺列
            return False

    def rename_member_task(self, group_id: str, old_user_id: str,
                           new_user_id: str) -> None:
        """改成员唯一标识（手机号）。⛔ 只改配置行，历史业务记录不动。

        ⭐ D31 修复 5（2026-10-21）：三件事放进**同一个显式事务**：
        1. 名单行改号
        2. 白名单授权跟着人走 —— 用 ``UPDATE OR IGNORE``：若新号在本群
           **已有同权限行**（比如换回旧手机号），直接 UPDATE 会撞
           ``(group_id, user_id, permission_type)`` 主键抛 IntegrityError，
           把改号变成 500；IGNORE 掉冲突行后再 ``DELETE`` 旧号残留，
           保证「旧号一条不剩、新号最多一条」
        3. 清旧号的 ``pending_session``（绑定输群名 / 实名两轮）——
           不清的话旧号下次发文本会被当成「上一轮的答案」吞掉
        ``with self._conn`` = 异常整体回滚，⛔ 不留「名单已改、授权没改」的半截状态。
        """
        if old_user_id == new_user_id:
            return
        now_iso = utcnow_iso()
        with self._conn:
            self._conn.execute(
                "UPDATE group_member_task_config SET user_id=?, updated_at=?"
                " WHERE group_id=? AND user_id=?",
                (new_user_id, now_iso, group_id, old_user_id))
            # 白名单授权跟着人走（冲突行忽略，下一步统一清旧号）
            self._conn.execute(
                "UPDATE OR IGNORE export_whitelist SET user_id=?, updated_at=?"
                " WHERE group_id=? AND user_id=?",
                (new_user_id, now_iso, group_id, old_user_id))
            self._conn.execute(
                "DELETE FROM export_whitelist WHERE group_id=? AND user_id=?",
                (group_id, old_user_id))
            self._conn.execute(
                "DELETE FROM pending_session WHERE group_id=? AND phone=?",
                (group_id, old_user_id))

    def rename_member_all_groups(self, old_user_id: str,
                                 new_user_id: str) -> dict:
        """⭐ D34-②B（2026-10-22 用户指令）：改号**跨群联动**。

        手机号是「同一个人的标识」，只改当前群会让这个人在别的群变成另一个人
        （导出/提醒对不上）。这里一次改完：
        1. 所有群名单行：逐群改号；**某群已有该新号**（同号两人）则跳过该群，
           记入 conflicts 让后台提示管理员人工处理，⛔ 不静默合并两个人；
        2. 所有群导出授权：跟着人走（`UPDATE OR IGNORE` 防主键冲突，再清旧号残留）；
        3. 所有群该旧号的两轮会话：清掉（否则旧号下条文本被当成"上一轮的答案"）。

        整体一个事务：异常全部回滚，⛔ 不留半截。返回
        `{"renamed": [群], "conflicts": [群]}`。
        """
        if old_user_id == new_user_id:
            return {"renamed": [], "conflicts": []}
        now_iso = utcnow_iso()
        groups = [r["group_id"] for r in self._conn.execute(
            "SELECT DISTINCT group_id FROM group_member_task_config"
            " WHERE user_id=?", (old_user_id,))]
        renamed, conflicts = [], []
        with self._conn:
            for gid in groups:
                taken = self._conn.execute(
                    "SELECT 1 FROM group_member_task_config"
                    " WHERE group_id=? AND user_id=?",
                    (gid, new_user_id)).fetchone()
                if taken:
                    conflicts.append(gid)
                    continue
                self._conn.execute(
                    "UPDATE group_member_task_config SET user_id=?, updated_at=?"
                    " WHERE group_id=? AND user_id=?",
                    (new_user_id, now_iso, gid, old_user_id))
                renamed.append(gid)
            # 授权跨群跟随（冲突行先 IGNORE，再统一清旧号残留）
            self._conn.execute(
                "UPDATE OR IGNORE export_whitelist SET user_id=?, updated_at=?"
                " WHERE user_id=?", (new_user_id, now_iso, old_user_id))
            self._conn.execute(
                "DELETE FROM export_whitelist WHERE user_id=?", (old_user_id,))
            # 会话无群维度限制：旧号在任何群留下的两轮状态都要清
            self._conn.execute(
                "DELETE FROM pending_session WHERE phone=?", (old_user_id,))
        return {"renamed": renamed, "conflicts": conflicts}

    def upsert_field(self, group_id: str, field_id: int, field_name: str,
                     unit: Optional[str], roles: list, enabled: bool,
                     sort_order: int) -> None:
        self._conn.execute(
            "INSERT INTO report_field_pool(group_id, field_id, field_name, unit, roles,"
            " enabled, sort_order, updated_at) VALUES(?,?,?,?,?,?,?,?)"
            " ON CONFLICT(group_id, field_id) DO UPDATE SET field_name=excluded.field_name,"
            " unit=excluded.unit, roles=excluded.roles, enabled=excluded.enabled,"
            " sort_order=excluded.sort_order, updated_at=excluded.updated_at",
            (group_id, field_id, field_name, unit,
             json.dumps(roles, ensure_ascii=False), 1 if enabled else 0,
             sort_order, utcnow_iso()))
        self._conn.commit()

    def list_fields(self, group_id: str, enabled_only: bool = True) -> Iterable[sqlite3.Row]:
        sql = "SELECT * FROM report_field_pool WHERE group_id=?"
        params: list = [group_id]
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY sort_order, field_id"
        return self._conn.execute(sql, params).fetchall()

    def get_field(self, group_id: str, field_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM report_field_pool WHERE group_id=? AND field_id=?",
            (group_id, field_id)).fetchone()

    def next_field_id(self, group_id: str) -> int:
        """分配下一个字段 id（2026-10-20：字段可物理删除，⛔ 不许复用旧号）。

        ⚠️ 旧的 ``MAX(field_id)+1`` 在字段被真删后会**回收**刚删掉的号：
        MySQL ``report_item`` 按 field_id 归并历史列（stats 导出），复用会把
        已删字段的历史数量并进新字段。故改用只增不减的 ``report_field_seq``。
        """
        row = self._conn.execute(
            "SELECT last_id FROM report_field_seq WHERE group_id=?",
            (group_id,)).fetchone()
        if row is None:   # 本群首次发号：按现有最大 field_id 起种（旧库 / 新群）
            mx = self._conn.execute(
                "SELECT COALESCE(MAX(field_id), 0) AS m FROM report_field_pool"
                " WHERE group_id=?", (group_id,)).fetchone()["m"]
            self._conn.execute(
                "INSERT OR IGNORE INTO report_field_seq(group_id, last_id)"
                " VALUES(?,?)", (group_id, int(mx)))
            row = self._conn.execute(
                "SELECT last_id FROM report_field_seq WHERE group_id=?",
                (group_id,)).fetchone()
        # CAS 递增：last_id 只增不减，被并发改动就重取（单进程下极少发生）
        for _ in range(20):
            last = int(row["last_id"])
            if self._conn.execute(
                    "UPDATE report_field_seq SET last_id=? WHERE group_id=?"
                    " AND last_id=?", (last + 1, group_id, last)).rowcount:
                self._conn.commit()
                return last + 1
            row = self._conn.execute(
                "SELECT last_id FROM report_field_seq WHERE group_id=?",
                (group_id,)).fetchone()
        raise RuntimeError("报数字段 id 分配失败：并发冲突")  # pragma: no cover

    def delete_field(self, group_id: str, field_id: int) -> int:
        """物理删除字段池一行（2026-10-20 用户指令）。

        历史报数记录 ⛔ 不受影响：MySQL ``report_item`` 存 field_name/unit
        快照，导出按快照出列；本表只决定「还能填哪些字段」。
        返回删除行数（0 = 字段不存在，调用方据此判 404）。
        """
        cur = self._conn.execute(
            "DELETE FROM report_field_pool WHERE group_id=? AND field_id=?",
            (group_id, field_id))
        self._conn.commit()
        return cur.rowcount

    # ---------- 导出白名单（2026-10-19 用户指令：单权化）----------
    # ⛔ 推翻 v4 按业务分权（EXPORT_CHECKIN/SUMMARY/REPORT 三权）：
    # 授权对象限本群应交成员（后台从群成员下拉选择，API 层校验）；
    # 权限只有一种 ADMIN_ALL（一键 = 全部管理员操作权限，当前即
    # 一键导出打卡/总结/报数三表）。旧三权数据由 _migrate v7->v8
    # 合并为每人一行 ADMIN_ALL。
    WHITELIST_PERMISSIONS = ("ADMIN_ALL",)

    def upsert_whitelist(self, group_id: str, user_id: str,
                         permission_type: str = "ADMIN_ALL") -> None:
        if permission_type not in self.WHITELIST_PERMISSIONS:
            raise ValueError("unknown permission_type: %r" % permission_type)
        self._conn.execute(
            "INSERT OR IGNORE INTO export_whitelist(group_id, user_id,"
            " permission_type, updated_at) VALUES(?,?,?,?)",
            (group_id, user_id, permission_type, utcnow_iso()))
        self._conn.commit()

    def remove_whitelist(self, group_id: str, user_id: str,
                         permission_type: str = "ADMIN_ALL") -> None:
        self._conn.execute(
            "DELETE FROM export_whitelist WHERE group_id=? AND user_id=?"
            " AND permission_type=?",
            (group_id, user_id, permission_type))
        self._conn.commit()

    def list_whitelist(self, group_id: str) -> Iterable[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM export_whitelist WHERE group_id=?"
            " ORDER BY user_id, permission_type", (group_id,)).fetchall()

    def has_whitelist_permission(self, group_id: str, user_id: str,
                                 permission_type: str = "ADMIN_ALL") -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM export_whitelist WHERE group_id=?"
            " AND user_id=? AND permission_type=?",
            (group_id, user_id, permission_type)).fetchone()
        return bool(row and row["n"] > 0)

    def in_whitelist(self, group_id: str, phone: str) -> bool:
        """是否在导出白名单内（单权化后即 ADMIN_ALL 一行存在）。"""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM export_whitelist WHERE group_id=?"
            " AND user_id=?", (group_id, phone)).fetchone()
        return bool(row and row["n"] > 0)

    # ---------- 入站学习人员（§2.7 组合方案 C） ----------

    def learn_user(self, phone: str, name: Optional[str],
                   platform_user_id: Optional[str]) -> None:
        now_iso = utcnow_iso()
        self._conn.execute(
            "INSERT INTO learned_user(phone, name, platform_user_id, first_seen, last_seen)"
            " VALUES(?,?,?,?,?) ON CONFLICT(phone) DO UPDATE SET"
            " name=COALESCE(excluded.name, learned_user.name),"
            " platform_user_id=COALESCE(excluded.platform_user_id, learned_user.platform_user_id),"
            " last_seen=excluded.last_seen",
            (phone, name, platform_user_id, now_iso, now_iso))
        self._conn.commit()

    def user_display_name(self, phone: str,
                          group_id: Optional[str] = None) -> Optional[str]:
        """展示名（群内 @ 通知用）：入站学习昵称优先，群成员名单昵称兜底。

        ⛔ 取不到返回 None（调用方回落脱敏手机号，⛔ 不编造姓名）。
        """
        row = self._conn.execute(
            "SELECT name FROM learned_user WHERE phone=?", (phone,)).fetchone()
        if row is not None and row["name"] and str(row["name"]).strip():
            return str(row["name"]).strip()
        if group_id:
            row = self._conn.execute(
                "SELECT nickname FROM group_member_task_config"
                " WHERE group_id=? AND user_id=?",
                (group_id, phone)).fetchone()
            if row is not None and row["nickname"] \
                    and str(row["nickname"]).strip():
                return str(row["nickname"]).strip()
        return None

    # ---------- H5 图片上传凭证（v7，§4.11.4 D4 定夺） ----------

    def put_upload(self, *, ref: str, token_id: str, group_id: str,
                   phone: str, file_name: str, mime: str, size: int,
                   rel_path: str, now: Optional[datetime] = None) -> None:
        """记一条上传凭证（ref 由服务端签发；行与令牌绑定）。"""
        now_iso = _ensure_aware(now).isoformat(timespec="seconds") \
            if now else utcnow_iso()
        self._conn.execute(
            "INSERT INTO h5_upload(ref, token_id, group_id, phone, file_name,"
            " mime, size, rel_path, created_at, consumed)"
            " VALUES(?,?,?,?,?,?,?,?,?,0)",
            (ref, token_id, group_id, phone, file_name[:128], mime,
             int(size), rel_path, now_iso))
        self._conn.commit()

    def get_upload(self, ref: str) -> Optional[sqlite3.Row]:
        if not ref or len(ref) > 64:
            return None
        return self._conn.execute(
            "SELECT * FROM h5_upload WHERE ref=?", (ref,)).fetchone()

    def mark_upload_consumed(self, ref: str) -> None:
        """提交成功后标记已引用（审计 / 清理用；⛔ 不影响安全重试放行）。"""
        self._conn.execute(
            "UPDATE h5_upload SET consumed=1 WHERE ref=?", (ref,))
        self._conn.commit()

    def list_uploads_before(self, cutoff_iso: str) -> list:
        """created_at 早于 cutoff 的上传行（清理用；只读清单）。"""
        return self._conn.execute(
            "SELECT ref, rel_path FROM h5_upload WHERE created_at < ?",
            (cutoff_iso,)).fetchall()

    def delete_upload(self, ref: str) -> None:
        self._conn.execute("DELETE FROM h5_upload WHERE ref=?", (ref,))
        self._conn.commit()

    # ---------- 安全事件 ----------

    def record_security_event(self, event_type: str, group_id: Optional[str],
                              phone: Optional[str], detail: str) -> None:
        self._conn.execute(
            "INSERT INTO security_event(event_type, group_id, phone, detail, created_at)"
            " VALUES(?,?,?,?,?)",
            (event_type, group_id, phone, detail, utcnow_iso()))
        self._conn.commit()

    # ---------- 群列表 / 提醒计划（§4.5） ----------

    def list_groups(self) -> Iterable[sqlite3.Row]:
        """全部已配置群（提醒轮询的遍历源；授权判定 = 有群配置，§3.2）。"""
        return self._conn.execute(
            "SELECT * FROM group_config ORDER BY group_id").fetchall()

    def record_reminder_plan(self, *, group_id: str, biz: str,
                             period_key: str, phone: str,
                             session: Optional[str],
                             now: Optional[datetime] = None,
                             resend_sent: bool = False,
                             source: str = "auto") -> bool:
        """记一条提醒计划（带 5 分钟冷却，用户指令第五阶段）。

        - 无记录 -> 插入 pending，返回 True
        - 已有记录且距上次活动（sent_at；未发送则 created_at）不足冷却
          -> False（⛔ 同周期同人不重复打扰）
        - 已有记录且冷却已过 -> 重置为 pending（周期内可再提醒），True

        ⭐ resend_sent（2026-10-17 修复「上午 8 次 / 下午 7 次」重复提醒）：
        - False（**自动调度**缺省）：status='sent' 的**自动**记录 ⛔ 一律不再重置
          —— 自动提醒每期每人每时段只发一次；unsent / pending 仍按 5 分钟
          冷却重试（推送失败的容错，⛔ 不是重复打扰）。
        - True（**管理后台手动提醒**）：冷却已过可重发，含 sent 行
          —— 管理员主动催，5 分钟冷却只防连点轰炸。

        ⭐ source（2026-10-20 用户指令：定时提醒不得被手动提醒吃掉）：
        自动调度遇到 status='sent' 且 source='manual' 的行，**视为本期还没
        自动提醒过** -> 直接接管重置发一次（⛔ 不受 5 分钟冷却拦）。
        否则「管理员上午手动催过一次」会把下午那场定时提醒整场吃掉
        （真实事故：2026-10-20 14:00 郑佳没被提醒，见主文档 D26）。

        ⚠️ 唯一索引仍兜底并发插入（调度线程与管理端手动触发可能并发）。
        """
        from .period import TZ
        now = _ensure_aware(now) if now else datetime.now(TZ)
        now_iso = now.isoformat(timespec="seconds")
        source = "manual" if source == "manual" else "auto"
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO reminder_plan(group_id, biz, period_key,"
            " phone, session, status, source, created_at)"
            " VALUES(?,?,?,?,?,'pending',?,?)",
            (group_id, biz, period_key, phone, session, source, now_iso))
        self._conn.commit()
        if cur.rowcount > 0:
            return True
        row = self._conn.execute(
            "SELECT * FROM reminder_plan WHERE group_id=? AND biz=?"
            " AND period_key=? AND phone=? AND IFNULL(session,'')=IFNULL(?,'')",
            (group_id, biz, period_key, phone, session)).fetchone()
        if row is None:  # pragma: no cover - 并发删除的极端窗口
            # ⚠️ D42（2026-10-26）：这里 ⛔ 不能静默 return False。
            # 返回 False = 「本期已提醒过」→ 调用方把该目标从 fresh 里剔除，
            # 消息里这个人就**凭空消失**（2026-10-25 下午高翔漏 @ 的最终落点）。
            # 插入被唯一键挡下、却按同一键查不到行，说明状态不一致（并发删除 /
            # 键拼装含空值差异），此时按「没发过」放行并留痕，宁可多发一次
            # 也不静默漏掉一个人。
            LOG.warning(
                "reminder_plan insert ignored but row missing: group=%s biz=%s "
                "period=%s phone=%s session=%s -> allow (fail-open)",
                group_id, biz, period_key,
                ("%s****%s" % (phone[:3], phone[-4:])
                 if len(phone) >= 7 else "***"), session)
            return True
        row_source = (row["source"] if "source" in row.keys() else "auto") or "auto"
        takeover = (source == "auto" and row["status"] == "sent"
                    and row_source == "manual" and not resend_sent)
        if row["status"] == "sent" and not resend_sent and not takeover:
            return False   # ⛔ 自动调度：本期已自动送达 = 不再打扰
        if not takeover:
            last_raw = (row["sent_at"]
                        if row["status"] == "sent" and row["sent_at"]
                        else row["created_at"])
            try:
                last_dt = _ensure_aware(datetime.fromisoformat(last_raw))
            except (TypeError, ValueError):  # pragma: no cover - 脏数据不当异常
                last_dt = now
            if last_dt + timedelta(minutes=REMINDER_COOLDOWN_MINUTES) > now:
                return False
        self._conn.execute(
            "UPDATE reminder_plan SET status='pending', reason=NULL,"
            " sent_at=NULL, source=?, created_at=? WHERE id=?",
            (source, now_iso, row["id"]))
        self._conn.commit()
        return True

    def mark_reminder_sent(self, group_id: str, biz: str, period_key: str,
                           phone: str, session: Optional[str],
                           now: Optional[datetime] = None) -> None:
        """标记已发送。now 可注入（调度/手动触发传本轮时钟，冷却判定才确定）。"""
        from .period import TZ
        sent_at = (_ensure_aware(now) if now
                   else datetime.now(TZ)).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE reminder_plan SET status='sent', sent_at=?"
            " WHERE group_id=? AND biz=? AND period_key=? AND phone=?"
            " AND IFNULL(session,'')=IFNULL(?,'')",
            (sent_at, group_id, biz, period_key, phone, session))
        self._conn.commit()

    def mark_reminder_unsent(self, group_id: str, biz: str, period_key: str,
                             phone: str, session: Optional[str],
                             reason: str,
                             now: Optional[datetime] = None) -> None:
        """标记未发送（如实记原因，⛔ 不伪造已发送）。now 可注入。"""
        from .period import TZ
        stamp = (_ensure_aware(now) if now
                 else datetime.now(TZ)).isoformat(timespec="seconds")
        self._conn.execute(
            "UPDATE reminder_plan SET status='unsent', reason=?, sent_at=NULL,"
            " created_at=? WHERE group_id=? AND biz=? AND period_key=? AND phone=?"
            " AND IFNULL(session,'')=IFNULL(?,'')",
            (reason[:200], stamp, group_id, biz, period_key, phone, session))
        self._conn.commit()

    # ---------- 管理后台：用户 / 会话 / 限流 / 审计（§4.7） ----------

    def count_pending_reminders(self, group_id: Optional[str] = None) -> int:
        """待提醒计划计数（仪表盘「待提醒人数」用；只读聚合）。

        group_id 为空 = 全量计数（兼容旧调用）；非空 = 只数该群。
        """
        if group_id is None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM reminder_plan WHERE status='pending'"
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM reminder_plan"
                " WHERE status='pending' AND group_id=?",
                (group_id,)).fetchone()
        return int(row["n"]) if row else 0

    def list_sent_reminders(self, group_id: str, biz: str,
                            period_key: str) -> list:
        """某群某业务某周期**已送达**的提醒（看板「已提醒」标记用；只读）。

        ⛔ 只认 ``status='sent'``：unsent 是推送失败（群里其实没收到），
        若也算「已提醒」，管理员会误以为催过而不再处理。
        打卡按 session 区分时段；总结/报数 session 为 NULL。
        """
        return self._conn.execute(
            "SELECT phone, session, sent_at FROM reminder_plan"
            " WHERE group_id=? AND biz=? AND period_key=? AND status='sent'",
            (group_id, biz, period_key)).fetchall()

    def count_admin_users(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM admin_user").fetchone()
        return int(row["n"]) if row else 0

    def create_admin_user(self, username: str, password_hash: str,
                          must_change_password: bool = True) -> int:
        now_iso = utcnow_iso()
        cur = self._conn.execute(
            "INSERT INTO admin_user(username, password_hash,"
            " must_change_password, created_at, updated_at)"
            " VALUES(?,?,?,?,?)",
            (username, password_hash, 1 if must_change_password else 0,
             now_iso, now_iso))
        self._conn.commit()
        return int(cur.lastrowid)

    def get_admin_user(self, username: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM admin_user WHERE username=?", (username,)).fetchone()

    def get_admin_user_by_id(self, user_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM admin_user WHERE id=?", (user_id,)).fetchone()

    def update_admin_password(self, user_id: int, password_hash: str,
                              must_change_password: bool = False) -> None:
        self._conn.execute(
            "UPDATE admin_user SET password_hash=?, must_change_password=?,"
            " updated_at=? WHERE id=?",
            (password_hash, 1 if must_change_password else 0, utcnow_iso(),
             user_id))
        self._conn.commit()

    def update_admin_username(self, user_id: int, username: str) -> None:
        """改管理员登录账号名（⭐ 2026-10-20 用户指令）。

        ⚠️ admin_user.username 是 UNIQUE：重名会抛 sqlite3.IntegrityError，
        由调用方（auth.change_username）先查重再改，并转成可读错误。
        会话不受影响：load_session 每次按 user_id 回读用户名。
        """
        self._conn.execute(
            "UPDATE admin_user SET username=?, updated_at=? WHERE id=?",
            (username, utcnow_iso(), user_id))
        self._conn.commit()

    def touch_admin_login(self, user_id: int) -> None:
        self._conn.execute(
            "UPDATE admin_user SET last_login_at=? WHERE id=?",
            (utcnow_iso(), user_id))
        self._conn.commit()

    # ---- 登录限流（用户名+IP） ----

    def login_failure(self, username: str, ip: str, now_iso: str,
                      lock_seconds: int) -> Optional[str]:
        """记一次失败；达到上限返回 locked_until（ISO），否则 None。"""
        row = self._conn.execute(
            "SELECT failed_count, first_failed_at FROM admin_login_attempt"
            " WHERE username=? AND ip=?", (username, ip)).fetchone()
        count = (int(row["failed_count"]) if row else 0) + 1
        self._conn.execute(
            "INSERT INTO admin_login_attempt(username, ip, failed_count,"
            " first_failed_at, locked_until) VALUES(?,?,?,?,?)"
            " ON CONFLICT(username, ip) DO UPDATE SET failed_count=?,"
            " first_failed_at=excluded.first_failed_at,"
            " locked_until=excluded.locked_until",
            (username, ip, count, now_iso, None, count))
        locked_until = None
        if count >= LOGIN_MAX_FAILURES:
            from .period import TZ
            from datetime import datetime as _dt
            locked_until = (_dt.fromisoformat(now_iso)
                            + timedelta(seconds=lock_seconds)).isoformat(
                                timespec="seconds")
            self._conn.execute(
                "UPDATE admin_login_attempt SET locked_until=? "
                "WHERE username=? AND ip=?", (locked_until, username, ip))
        self._conn.commit()
        return locked_until

    def login_locked_until(self, username: str, ip: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT locked_until FROM admin_login_attempt"
            " WHERE username=? AND ip=?", (username, ip)).fetchone()
        return row["locked_until"] if row else None

    def reset_login_failures(self, username: str, ip: str) -> None:
        self._conn.execute(
            "DELETE FROM admin_login_attempt WHERE username=? AND ip=?",
            (username, ip))
        self._conn.commit()

    # ---- 会话 ----

    def create_admin_session(self, token_hash: str, user_id: int,
                             csrf_token: str, expires_at: datetime,
                             ip: Optional[str], now_iso: str) -> None:
        self._conn.execute(
            "INSERT INTO admin_session(token_hash, user_id, csrf_token,"
            " created_at, last_seen_at, expires_at, ip)"
            " VALUES(?,?,?,?,?,?,?)",
            (token_hash, user_id, csrf_token, now_iso, now_iso,
             _ensure_aware(expires_at).isoformat(timespec="seconds"), ip))
        self._conn.commit()

    def get_admin_session(self, token_hash: str) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM admin_session WHERE token_hash=?",
            (token_hash,)).fetchone()

    def touch_admin_session(self, token_hash: str, now_iso: str) -> None:
        self._conn.execute(
            "UPDATE admin_session SET last_seen_at=? WHERE token_hash=?",
            (now_iso, token_hash))
        self._conn.commit()

    def delete_admin_session(self, token_hash: str) -> None:
        self._conn.execute(
            "DELETE FROM admin_session WHERE token_hash=?", (token_hash,))
        self._conn.commit()

    def delete_admin_sessions_for_user(self, user_id: int) -> None:
        """改密后吊销该用户全部会话（§4.7：改密后旧会话不残留）。"""
        self._conn.execute(
            "DELETE FROM admin_session WHERE user_id=?", (user_id,))
        self._conn.commit()

    # ---- 审计 ----

    def insert_admin_audit(self, username: str, action: str,
                           target_type: Optional[str], target_id: Optional[str],
                           detail: Optional[dict], result: str,
                           ip: Optional[str]) -> None:
        self._conn.execute(
            "INSERT INTO admin_audit(username, action, target_type, target_id,"
            " detail, result, ip, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (username, action, target_type, target_id,
             json.dumps(detail, ensure_ascii=False) if detail else None,
             result, ip, utcnow_iso()))
        self._conn.commit()

    def list_admin_audit(self, limit: int = 100) -> Iterable[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
