# 环境变量完整清单（group-ops 测试服务器 219.151.177.33）

> 两个服务、两份 env 文件，凭据分离：
> - 机器人：`/etc/group-ops/group-ops-bot.env`（模板 `deploy/group-ops-bot.env.template`）
> - MCP Server：`/etc/group-ops/mcp-server.env`（模板 `deploy/mcp-server.env.template`）
>
> 纪律：`root:root` + `chmod 600`；⛔ 不进 git、⛔ 不上传源码包、⛔ 不打日志。
> fail-closed：标注「必需」的变量缺失 → 对应服务**拒绝启动**（`app/config.py`）。

## 一、机器人服务（`python -m app.main`，:12334 回调 + :18003 管理后台同进程）

| 变量 | 必填 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|---|
| `GOPS_MCP_BASE_URL` | ✅ 必需 | 无 | MCP Server 地址（SSE）。同机部署 = `http://127.0.0.1:18001` | `app/config.py:75` |
| `GOPS_EXTERNAL_BASE_URL` | ✅ 必需 | 无 | H5 链接拼装 base，须公网可达。测试期 = `http://219.151.177.33:12334` | `app/config.py:76-81` |
| `GOPS_ADMIN_USERNAME` | ✅ 必需* | 无 | 管理后台初始账号（*仅首次引导时需要；已有管理员后忽略） | `app/admin/auth.py:242` |
| `GOPS_ADMIN_PASSWORD` | ✅ 必需* | 无 | 初始密码，**≥8 位**，首次登录强制改密；不可复用 | `app/admin/auth.py:243-254` |
| `GOPS_CALLBACK_PORT` | ⬜ 可选 | `18002` | 群回调 + H5 + 上传端口（服务器上经 env 覆盖为 12334） | `app/config.py:89` |
| `GOPS_ADMIN_PORT` | ⬜ 可选 | `18003` | 管理后台端口（⛔ 不申请公网映射） | `app/config.py:90` |
| `GOPS_ADMIN_HOST` | ⬜ 可选 | `0.0.0.0` | 管理后台监听地址。**公网服务器必须显式 `127.0.0.1`**（经 SSH 隧道访问） | `app/config.py:91` |
| `GOPS_DATA_DIR` | ⬜ 可选 | `data` | SQLite 状态库 + 导出文件唯一可写目录 | `app/config.py:93` |
| `GOPS_DAILY_CUTOFF_HOUR` | ⬜ 可选 | `19` | 日报/报数默认截止小时（Q-BIZ5） | `app/config.py:82` |
| `GOPS_DAILY_CUTOFF_MINUTE` | ⬜ 可选 | `0` | 日报/报数默认截止分钟 | `app/config.py:83` |
| `GOPS_MCP_TIMEOUT` | ⬜ 可选 | `30` | MCP 调用超时（秒） | `app/config.py:98` |
| `GOPS_BIND_ADMIN_PHONES` | ⬜ 可选 | 空 | 群内「绑定」命令管理员手机号（逗号分隔）。**空 = 群内绑定全员不可用**（fail-closed），只能经后台「发现但未绑定」列表绑定 | `app/config.py:86-87` |
| `GOPS_ENABLE_SCHEDULER` | ⬜ 可选 | `1` | 提醒调度线程。MCP 未就绪期设 `0`（避免每分钟不可达日志） | `app/main.py:130` |
| `GOPS_LOG_LEVEL` | ⬜ 可选 | `INFO` | 日志级别 | `app/main.py:112` |

> 注：回调监听地址由 `BotConfig.callback_host` 固定 `0.0.0.0`（局域网紫玉直连需要，2026-10-13 确认），⛔ 不提供 env 缩小。

## 二、MCP Server（`python -m mcp_server.server`，:18001 SSE）

| 变量 | 必填 | 默认值 | 说明 | 代码位置 |
|---|---|---|---|---|
| `GOPS_MYSQL_HOST` | ✅ 必需 | 无 | MySQL 地址。实测实例 = `219.151.177.33`（公网可达，§5.1.1） | `app/config.py:116` |
| `GOPS_MYSQL_PORT` | ✅ 必需 | `3306` | 实测实例端口 = `28619` | `app/config.py:117` |
| `GOPS_MYSQL_USER` | ✅ 必需 | 无 | 实测账户 = `qsm` | `app/config.py:118` |
| `GOPS_MYSQL_PASSWORD` | ✅ 必需 | 无 | ⛔ 只进内存，不日志不落盘（`mcp_server/db/pool.py`） | `app/config.py:119` |
| `GOPS_MYSQL_DATABASE` | ⬜ 可选 | `qsm` | 目标库名 | `app/config.py:120` |
| `FASTMCP_HOST` | ⬜ 可选 | `127.0.0.1` | SSE 监听地址（官方 SDK Settings，env 前缀 `FASTMCP_`）。⛔ 只听回环：机器人同机访问，⛔ 不公网暴露 | `mcp_server/server.py`（SDK 侧） |
| `FASTMCP_PORT` | ⬜ 可选 | `8000` | SSE 端口，**须设 18001** 与机器人 `GOPS_MCP_BASE_URL` 对齐 | 同上 |

**依赖版本约束（2026-10-14 实测）**：必须 `pip install 'mcp>=1.0,<2'`（⚠️ 裸 `mcp>=1.0` 会装到 2.3.0——2.x 已删除 `mcp.server.fastmcp`（FastMCP 更名 MCPServer），import 直接失败）。另：`requirements-mcp.txt` 里的 `pytest>=8.0` 是 CI 依赖，部署 venv 按最小化裁剪不装。

✅ **FASTMCP_PORT 已修复**（2026-10-14，mcp 1.30.0 实测 + 临时实例实证）：`FastMCP.__init__` 把 host/port 作**显式参数**传给 `Settings()`（pydantic init > env 优先级），仅设 env 不传参将落在 SDK 默认 **8000**。修复 = `mcp_server/server.py` 启动入口显式读取 `FASTMCP_HOST`/`FASTMCP_PORT` 传入 `FastMCP(...)`（缺省 `127.0.0.1:8000`；端口非法值 fail-closed 拒启；⛔ 未动业务逻辑）。服务器临时实例实证：带 env → `ss` 显示 `LISTEN 127.0.0.1:18001` + `GET /sse` 200；不带 env → `LISTEN 127.0.0.1:8000`（默认保持）。

## 三、两台之间的端口账（测试服务器单机）

| 端口 | 服务 | 监听地址 | 公网 |
|---|---|---|---|
| 12334 | 机器人（回调 + H5 + 上传） | 0.0.0.0 | ✅ 已映射（219.151.177.33:12334） |
| 18001 | MCP Server（SSE） | 127.0.0.1 | ⛔ 不映射 |
| 18003 | 机器人同进程 · 管理后台 | 127.0.0.1 | ⛔ 不映射（SSH 隧道） |
| 28619 | MySQL（219.151.177.33:28619，共享库 qsm） | 对端 | 公网 TCP 可达（§5.1.1） |
