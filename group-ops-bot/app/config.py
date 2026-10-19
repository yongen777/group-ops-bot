# -*- coding: utf-8 -*-
"""集中配置（§4.8 第 1 步：fail-closed）。

纪律：
- 启动期校验：必需配置缺失 / 无效 -> 启动失败，⛔ 不静默选择任何替代路径
- 时区集中在本模块引用 period.TZ_NAME，⛔ 业务代码不散落硬编码（§2.3）
- 凭据（MySQL 密码 / MCP 鉴权）只从环境变量读取，⛔ 不写文件、⛔ 不落日志
- 本模块自身不打印任何配置值
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


class ConfigError(RuntimeError):
    """配置缺失或非法（fail-closed）。"""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            "required env %s is missing; refusing to start (fail-closed)" % name)
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("env %s must be an integer, got %r" % (name, raw))


@dataclass(frozen=True)
class McpServerConfig:
    """机器人侧连接 MCP Server 的参数（MCP Server 由本项目自研，§4.11）。"""

    base_url: str            # 如 http://127.0.0.1:18001（开发期同机）
    timeout_seconds: int = 30


@dataclass(frozen=True)
class BotConfig:
    """机器人业务服务配置。"""

    callback_host: str = "0.0.0.0"     # 必须 0.0.0.0：局域网紫玉要直连（2026-10-13 确认）
    callback_port: int = 18002         # 群回调 + H5 + 上传（公网映射端口）
    admin_host: str = "0.0.0.0"        # 管理后台独立监听（§3.1 / §4.7）
    admin_port: int = 18003            # 仅内网 / VPN，⛔ 不申请公网映射
    external_base_url: str = ""        # 对外 base（H5 链接拼装用，公网入口就绪后配置）
    tz_name: str = "Asia/Shanghai"
    data_dir: str = "data"
    daily_cutoff: tuple = (19, 0)      # 日报/报数默认截止（Q-BIZ5 默认）
    pending_ttl_minutes: int = 30      # 两轮对话焦点 TTL（§2.2；2026-10-17 用户指令六）
    bind_admin_phones: tuple = ()      # 群绑定管理员手机号（P0；空 = 群内「绑定」全员不可用，只能后台绑）
    mcp: McpServerConfig = field(default=None)  # type: ignore[assignment]


def load_bot_config() -> BotConfig:
    """从环境变量加载机器人配置（fail-closed）。

    必需：GOPS_MCP_BASE_URL（MCP Server 地址）、GOPS_EXTERNAL_BASE_URL（H5 链接 base）。
    可选：GOPS_CALLBACK_PORT / GOPS_ADMIN_PORT / GOPS_ADMIN_HOST /
    GOPS_DATA_DIR / GOPS_DAILY_CUTOFF_HOUR / GOPS_DAILY_CUTOFF_MINUTE /
    GOPS_MCP_TIMEOUT / GOPS_BIND_ADMIN_PHONES /
    GOPS_PENDING_SESSION_TTL_MINUTES。
    GOPS_PENDING_SESSION_TTL_MINUTES：两轮对话焦点 TTL（默认 30，
    2026-10-17 用户指令六；过期后明确提示重发命令，⛔ 不静默失效）。
    GOPS_ADMIN_HOST：管理后台监听地址（§2.10：仅内网 / VPN，⛔ 不公网暴露）。
    公网服务器上必须显式设 127.0.0.1（经 SSH 隧道访问），默认 0.0.0.0 仅限开发机。
    GOPS_BIND_ADMIN_PHONES：群内「绑定」命令的管理员手机号（逗号分隔）。
    空 = 群内绑定全员不可用（fail-closed），只能经管理后台绑定（P0 双路径）。
    """
    mcp_base = _require("GOPS_MCP_BASE_URL")
    external_base = os.environ.get("GOPS_EXTERNAL_BASE_URL", "").strip()
    if not external_base:
        # 未配置对外 base：H5 链接无法拼装，如实失败而非拼出内网地址误发到平台
        raise ConfigError(
            "required env GOPS_EXTERNAL_BASE_URL is missing; "
            "H5 links cannot be built safely (fail-closed)")
    cutoff_h = _int("GOPS_DAILY_CUTOFF_HOUR", 19)
    cutoff_m = _int("GOPS_DAILY_CUTOFF_MINUTE", 0)
    if not (0 <= cutoff_h <= 23 and 0 <= cutoff_m <= 59):
        raise ConfigError("invalid daily cutoff time")
    # 两轮对话焦点 TTL（2026-10-17 用户指令六：绑定 / 总结等待输入的有效期）。
    # 默认 30 分钟；本地演示过期行为可临时调小（如 2），⛔ 不接受 <=0。
    pending_ttl = _int("GOPS_PENDING_SESSION_TTL_MINUTES", 30)
    if pending_ttl <= 0:
        raise ConfigError("invalid pending session ttl (must be > 0)")
    bind_raw = os.environ.get("GOPS_BIND_ADMIN_PHONES", "").strip()
    bind_phones = tuple(p.strip() for p in bind_raw.split(",") if p.strip())
    return BotConfig(
        callback_port=_int("GOPS_CALLBACK_PORT", 18002),
        admin_port=_int("GOPS_ADMIN_PORT", 18003),
        admin_host=os.environ.get("GOPS_ADMIN_HOST", "").strip() or "0.0.0.0",
        external_base_url=external_base.rstrip("/"),
        data_dir=os.environ.get("GOPS_DATA_DIR", "data").strip() or "data",
        daily_cutoff=(cutoff_h, cutoff_m),
        pending_ttl_minutes=pending_ttl,
        bind_admin_phones=bind_phones,
        mcp=McpServerConfig(
            base_url=mcp_base.rstrip("/"),
            timeout_seconds=_int("GOPS_MCP_TIMEOUT", 30),
        ),
    )


@dataclass(frozen=True)
class MysqlConfig:
    """MCP Server 侧 MySQL 配置（凭据只走环境变量，⛔ 不落盘）。"""

    host: str
    port: int
    user: str
    password: str          # 仅内存中存在；⛔ 不日志、不写文件
    database: str = "qsm"
    charset: str = "utf8mb4"


def load_mysql_config() -> MysqlConfig:
    host = _require("GOPS_MYSQL_HOST")
    port = _int("GOPS_MYSQL_PORT", 3306)
    user = _require("GOPS_MYSQL_USER")
    password = _require("GOPS_MYSQL_PASSWORD")
    database = os.environ.get("GOPS_MYSQL_DATABASE", "qsm").strip() or "qsm"
    return MysqlConfig(host=host, port=port, user=user,
                       password=password, database=database)
