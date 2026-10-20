# 群运维机器人 · 测试服务器部署 Runbook

- 目标服务器：`219.151.177.33`（紫玉提供，测试服务器）
- 服务监听：`0.0.0.0:12334`（⛔ 不听 localhost）
- 管理后台：`127.0.0.1:18003`（仅本机，§2.10 ⛔ 不公网暴露；远程管经 SSH 隧道）
- 组件范围：**阶段 A 只部署机器人本体**（`group-ops-bot/app/`）。MCP Server
  启动即 fail-closed（缺 MySQL 配置拒启），MySQL 未批准未建表 → 写入链路不在
  本期范围，提交会如实归 C 类（unknown），⛔ 不冒充成功。
  **阶段 B**（DDL 批准后）按第六~八节加部署 MCP Server + 检查 MySQL，见
  「启动顺序」。

## 目录规划（配置 / 代码 / 数据 / 日志四隔离）

```
/opt/group-ops/
├── repo/      # 上传的代码（group-ops-bot/ 的内容，只读运行）
├── venv/      # Python 虚拟环境
└── data/      # SQLite 状态库 bot_state.db + exports/（服务唯一可写目录）
/etc/group-ops/
├── group-ops-bot.env   # 机器人环境变量（root:root 600）
└── mcp-server.env      # MCP Server 环境变量（root:root 600；⛔ 含 MySQL 密码）
日志：systemd journal（journalctl -u group-ops-bot / -u group-ops-mcp）
```

## 一、上传代码（在开发机执行，Git Bash）

```bash
scp -r /d/group-ops/group-ops-bot/app \
      /d/group-ops/group-ops-bot/mcp_server \
      /d/group-ops/group-ops-bot/requirements.txt \
      /d/group-ops/group-ops-bot/requirements-mcp.txt \
      <用户>@219.151.177.33:/tmp/group-ops-upload/
```

> ⚠️ 只传这四个条目。⛔ 不传 `.env` / 数据目录 / 冒烟库 / 测试目录。
> 本项目非 git 仓库，无凭据随代码外泄风险，但仍按白名单传。

## 二、服务器初始化（root 或 sudo，执行 deploy.sh）

```bash
sudo bash deploy.sh          # 建用户/目录/venv/装依赖/装 systemd 单元
sudo cp group-ops-bot.env.template /etc/group-ops/group-ops-bot.env
sudo chown root:root /etc/group-ops/group-ops-bot.env
sudo chmod 600 /etc/group-ops/group-ops-bot.env
# ⚠️ 编辑 env：只需 MCP 地址与对外 base URL（后台账号不设 env，见下）
sudo systemctl start group-ops-bot
```

`deploy.sh` 已包含：建 `groupops` 用户、目录、venv、`pip install -r requirements.txt`、
daemon-reload + enable（开机自启）。

## 三、环境变量完整清单

两个服务、两份 env 文件，**完整清单（变量 / 必填性 / 默认值 / 代码位置）见
`deploy/env-reference.md`**。摘要：

| 服务 | env 文件 | 必需项（缺失即拒启，fail-closed） |
|---|---|---|
| 机器人（:12334 + :18003 同进程） | `/etc/group-ops/group-ops-bot.env` | `GOPS_MCP_BASE_URL`、`GOPS_EXTERNAL_BASE_URL`、`GOPS_ADMIN_USERNAME`、`GOPS_ADMIN_PASSWORD`（首登后失效） |
| MCP Server（:18001 SSE） | `/etc/group-ops/mcp-server.env` | `GOPS_MYSQL_HOST`、`GOPS_MYSQL_PORT`、`GOPS_MYSQL_USER`、`GOPS_MYSQL_PASSWORD` |

其余可选变量（端口 / 数据目录 / 截止时间 / 绑定管理员 / 调度开关 / 日志级别 /
`FASTMCP_HOST` / `FASTMCP_PORT`）及默认值均以 env-reference.md 为准。

## 四、MySQL 配置（MCP Server 侧；机器人⛔ 不直连）

目标实例（2026-10-12 只读实测，§5.1.1）：`219.151.177.33:28619`，账户 `qsm`，
库 `qsm`（**共享库**），MySQL 5.7.43-log / utf8mb4_general_ci / CST(UTC+8)。
密码由数据库管理员提供，⛔ 不记录在任何文档，只写服务器本地 env 文件。

```bash
sudo cp mcp-server.env.template /etc/group-ops/mcp-server.env
sudo chown root:root /etc/group-ops/mcp-server.env
sudo chmod 600 /etc/group-ops/mcp-server.env
# 编辑：把 GOPS_MYSQL_PASSWORD=REPLACE_ME 换成真实密码；确认五项：
#   GOPS_MYSQL_HOST=219.151.177.33
#   GOPS_MYSQL_PORT=28619
#   GOPS_MYSQL_USER=qsm
#   GOPS_MYSQL_PASSWORD=<管理员提供>
#   GOPS_MYSQL_DATABASE=qsm
```

⛔ 缺任一 `GOPS_MYSQL_*` → MCP Server 拒绝启动（`app/config.py load_mysql_config`），
⛔ 不存在「无库降级模式」。

## 五、数据库初始化检查清单（只读，⛔ 不执行 DDL）

```bash
export GOPS_MYSQL_PASSWORD='<mysql password>'   # ⛔ 只走环境变量，不进命令行
sudo bash mysql-check.sh                        # 默认带 --ddl 指向 ddl/mysql_qsm_v1.sql
```

脚本 `mysql_check.py`（只执行 SHOW/SELECT，内置只读硬闸）逐项输出：

| 清单项 | 判据 |
|---|---|
| 当前数据库版本 | server version + 协议版本；与实测基线 5.7.x 不一致 → 警告 |
| 是否为空库 | 目标库存在性 + `SHOW TABLES` 数量；库不存在 → 退出码 4 |
| 是否已有表 | 表清单与 DDL 六表（sys_user / checkin_record / summary_record / report_record / report_item / operation_log）逐一对照；非预期表 → HIGH 风险 |
| DDL 执行风险 | 共享库警示 + 字符集/排序规则 + 时区(CST) + `SHOW GRANTS` 权限 + 列级漂移核对（传了 --ddl 时） |

退出码：`0` 空库可申请批准 ｜ `2` 已有表需人工核对 ｜ `3` 连接/鉴权失败 ｜
`4` 库不存在 ｜ `5` 用法错误。

**DDL 执行前置（批准后逐项确认，⛔ 批准前一项都不做）**：
① 用户明确批准（含目标库）② `mysql-check.sh` 退出码 0 或人工核对通过
③ 空库基线备份一次 ④ 字符集/时区如上核对无误 ⑤ 保留期限策略另行与管理员确认。

## 六、MCP Server 部署（阶段 B；deploy-mcp.sh）

| 项 | 值 |
|---|---|
| 启动方式 | `systemd`：`group-ops-mcp.service` → `/opt/group-ops/venv/bin/python -m mcp_server.server` |
| 端口 | `18001`，**只听 127.0.0.1**（`FASTMCP_HOST=127.0.0.1`；⛔ 不映射公网） |
| 依赖 | `requirements-mcp.txt`：**`mcp>=1.0,<2`**（官方 SDK，SSE 传输；⚠️ 2.x 已删 `mcp.server.fastmcp`，`mcp>=1.0` 裸约束会装到 2.3.0 而 import 失败）+ `pymysql>=1.1`（`pytest` 为 CI 依赖，部署 venv 最小化不装） |
| 配置 | `/etc/group-ops/mcp-server.env`（第四节五项 MySQL + FASTMCP_PORT=18001） |
| 与机器人对接 | 机器人 env `GOPS_MCP_BASE_URL=http://127.0.0.1:18001`（已预置） |

```bash
sudo bash deploy-mcp.sh        # 预检→装依赖→装 unit→启动→SSE 探针
sudo bash mcp-check.sh         # 只读链路检查（见下）
```

`mcp-check.sh` 检查：unit active / `18001` 在听 / `GET /sse` 有 endpoint 事件 /
公网访问 18001 不通（回环-only 自证）/ 机器人 startup 日志里的 mcp 地址 /
18003 仍仅本地可达。

✅ **FASTMCP_PORT 已修复并实测**（2026-10-14，mcp 1.30.0）：`mcp 1.x` 的
`FastMCP.__init__` 把 host/port 作**显式参数**传给 Settings（pydantic init > env
优先级），仅设 env 不传参将永远落在 SDK 默认 **8000**。修复 = `mcp_server/server.py`
的启动入口显式读取 `FASTMCP_HOST`/`FASTMCP_PORT` 再传入 `FastMCP(...)`
（缺省 `127.0.0.1:8000`；端口非法值 fail-closed 拒启；⛔ 未动任何业务逻辑）。
服务器临时实例实证：带 env → `ss` 显示 `LISTEN 127.0.0.1:18001` + `GET /sse` 200；
不带 env → `LISTEN 127.0.0.1:8000`（默认保持）。

## 七、启动顺序

```
① MySQL（219.151.177.33:28619，外部实例，已运行——⛔ 本项目不管理不重启）
        ↓  只读检查先行：mysql-check.sh（退出码 0/2 人工核对）
② MCP Server（group-ops-mcp，:18001 回环）  ← deploy-mcp.sh
        ↓  mcp-check.sh 全过（SSE 握手 + 回环-only）
③ 机器人服务（group-ops-bot，:12334 回调 + H5）
        ↓  verify.sh / 阶段 A1~A8
④ 管理后台 —— ⛔ 不是独立服务：与机器人同进程双监听（:18003，§3.1），
   机器人起来它就在；远程访问走 SSH 隧道
```

纪律：
- 顺序倒置的后果是「提交归 C」——如实但⛔不冒充成功；MCP 未就绪时机器人仍能起
  （启动不硬依赖 MCP，⛔ 不因等 MCP 把机器人堵住）；
- 提醒调度 `GOPS_ENABLE_SCHEDULER=0` 保持关闭，直到 mcp-check.sh 全过 + push
  能力确认（§5.2），避免每分钟不可达日志；
- ⛔ 全流程不执行 DDL——建表只在用户明确批准后单独进行。

## 八、验证（执行 verify.sh / mcp-check.sh）

| 项 | 期望 |
|---|---|
| `GET /healthz`（本机 + 公网 IP） | 200 `{"status":"ok"}` |
| `GET /h5/checkin`、`GET /h5/report` | 200 HTML |
| `POST /api/callback`（合成载荷） | 200，journal 有解析记录 |
| `ss -tlnp` | `0.0.0.0:12334` 在听；18003 只绑 127.0.0.1；18001 只绑 127.0.0.1 |
| 管理后台 | `curl 127.0.0.1:18003/healthz` 200；公网访问 18003 不通 |
| MCP Server | `mcp-check.sh` PASS（SSE 握手 + 回环-only + 机器人接线） |

### 8.1 验证拆分：阶段 A（无 MySQL/MCP）与阶段 B（接真实库）

用户指令口径：部署验证分两阶段，⛔ 不因「MySQL 未就绪」把阶段 A 全堵住。

**阶段 A（当前部署即可执行，全部本地 + 公网入口证据）**：

| # | 验证项 | 判据 | 结果回填 |
|---|---|---|---|
| A1 | 服务启动 | `systemctl status` active；journal 无 fail-closed 拒启 | ☐ |
| A2 | 公网回调可达 | 开发机 `POST http://219.151.177.33:12334/api/callback` → 200（非法载荷 400 / 合法载荷 200） | ☐ |
| A3 | **P0 群绑定闭环** | 机器人进群 → 绑定管理员发「绑定」→ 群内收到「绑定成功」→ 后台「群管理」出现该群（active、五业务全关）→ 开业务后本群命令正常、**其他群仍静默** | ☐ |
| A4 | 后台「发现但未绑定」 | 未绑定群发过消息的群出现在列表，可一键绑定（409 幂等） | ☐ |
| A5 | H5 公网访问 | 手机浏览器开 `http://219.151.177.33:12334/h5/checkin` → 200 页面（token 由群内命令链接带入） | ☐ |
| A6 | 未授权群静默 | 未绑定群发业务命令 → 不回复 + `security_event(unauthorized_group)` 落库 | ☐ |
| A7 | C 类行为符合设计 | 后端不可达时提交 → 如实归 C（「结果未知」话术 + intent=unknown + 恢复任务 pending + 令牌不消费）；**⛔ 不冒充成功** | ☐ |
| A8 | 管理后台仅本地可达 | 公网访问 18003 不通；SSH 隧道内可登录（首登强制改密） | ☐ |

**阶段 B（DDL 批准 + MySQL 建表 + MCP Server 部署后才能执行）**：

| # | 验证项 | 判据 |
|---|---|---|
| B1 | 打卡 A 类全链路 | 群内「打卡」→ H5 上传提交 → 「打卡成功」+ 后端 `checkin_record` 有记录 + 令牌 consumed |
| B2 | 报数 A 类全链路 | 「报数」→ H5 勾选字段填数提交 → 成功 + `report_record`/`report_item` 落账 |
| B3 | 总结 A 类全链路 | 「总结 X」→ 保存 + `summary_record` 落账；覆盖确认流行为符合设计 |
| B4 | 提醒推送 | 到点 → `reminder_plan` 落库 + push 接通后群内可见（未接通如实 unsent） |
| B5 | 统计导出 | 四命令独立导出 → xlsx 落盘 → file 消息（D12 接通后）群内可下载 |

> ⚠️ 阶段 A 全过 **只证明**：服务可公网到达、群绑定闭环成立、未授权不扰、
> 失败如实归 C。⛔ **不构成**五业务真实链路证据（那必须等阶段 B）。

## 九、访问地址

| 用途 | 地址 |
|---|---|
| 群回调（给量子密信） | `http://219.151.177.33:12334/api/callback` |
| H5 入口 base | `http://219.151.177.33:12334`（GOPS_EXTERNAL_BASE_URL） |
| 管理后台（SSH 隧道） | `ssh -L 18003:127.0.0.1:18003 <用户>@219.151.177.33` 后开 `http://127.0.0.1:18003` |
| MCP Server | `http://127.0.0.1:18001`（⛔ 仅本机；不映射公网） |

> ⚠️ **以上 HTTP 地址均为测试阶段口径**（§5.3 B1 未解除）：
> 用于测试服务器联调，⛔ 不作为正式上线地址。
> **正式平台回调需 HTTPS 域名确认项**：量子密信正式环境若要求 HTTPS 回调
> （平台侧要求 / 网络组策略），需追加：① 固定域名（A 记录 → 219.151.177.33
> 或正式 CNAME）② 证书（Let's Encrypt / 平台提供）③ 反向代理（Caddy/Nginx
> 终止 TLS 后转发 127.0.0.1:12334）④ 回调 URL 改 `https://<域名>/api/callback`、
> `GOPS_EXTERNAL_BASE_URL` 同步改。**待紫玉 / 平台侧确认后执行，⛔ 未确认前
> 不得把 HTTP 地址当作正式回调交付。**

## 十、已知限制（⛔ 不得据此宣称联调通过）

1. **HTTP 无 TLS（测试阶段口径）**：平台正式环境若要求 HTTPS 回调（§5.3 B1），
   IP:port 方案不达标 → 按「九、访问地址」下方的 **HTTPS 域名确认项** 处理，
   待紫玉 / 平台侧确认。
2. **回调来源真实性未验**（D10）：上线前必须加验证机制，当前按设计假设运行。
3. **写入链路未通**：MCP/MySQL 未部署（部署路径已备：第四~六节），DDL 未批准，
   提交归 C 类（设计行为，恢复处理器就绪）。
4. **回推载荷未实测**（D11）：`reply_text` 按 `{"text":...}` 假设，实测不符只改 `app/platform/qmx.py`。
5. ~~**图片上传 501**（§4.11.4 通道未定夺）~~ **已修复并上线**（2026-10-16 D5+D8：H5 → :12334 multipart → 服务器磁盘 → `image_ref`；魔数嗅探 + 10MB 上限 + 令牌绑定凭证防冒领 + 48h 保留清理；依赖 `python-multipart` 已入 requirements.txt）
6. **`FASTMCP_PORT` 生效性未实测**：依赖官方 SDK 版本；`mcp-check.sh` 会报告实际
   监听端口，不符时按第六节两种做法之一处理。

## 十一、部署后优先级（用户指令口径）

1. **先 P0 群绑定 + 阶段 A 验证**（第八节 A1~A8），再按阶段 B 逐业务打通；
2. ⛔ **不先优化管理后台 UI** —— 后台能用即可，业务链路优先；
3. 阶段 A 全过 + 阶段 B 就绪后，输出**部署测试报告**（逐项判据 + 实际结果 +
   未执行项及原因），回填主文档 §7.1 / §8.13。

## 十二、回滚

```bash
sudo systemctl stop group-ops-bot && sudo systemctl disable group-ops-bot
sudo systemctl stop group-ops-mcp  && sudo systemctl disable group-ops-mcp
# 代码与数据留在 /opt/group-ops，确认无误后再删
```
