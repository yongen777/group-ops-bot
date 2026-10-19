# D3-A.5 量子机器人实例模型核查报告

| 项 | 值 |
|---|---|
| 日期 | 2026-10-14 |
| 输入 | 用户确认量子密信实例模型：**一个机器人 = 一个群 = 一个 webhook 地址**；不存在一机器人多群；webhook 属群机器人实例，非全局配置 |
| 性质 | **只读核查 + 设计差异确认 + 方案评估** |
| 边界 | ⛔ 未改业务代码 / ⛔ 未改数据库 / ⛔ 未启 scheduler / ⛔ 未重启 bot / ⛔ MCP 服务未动 / ⛔ 未改 normalize.py / qmx.py |

---

## 1. 当前实现

### webhook 来源

| 环节 | 现状 | 证据 |
|---|---|---|
| 入站（平台 → 机器人） | `reply_url` **100% 来自回调载荷**（`normalize.py:32-33` 候选键 `reply_url/replyUrl/callback_url/callbackUrl/webhook/push_url` + 嵌套），每条消息自带，机器人**不存储、不配置、不校验域名** | `app/normalize.py:32-33, 119-120` |
| 全局配置 | **不存在**。`BotConfig`（`app/config.py:48-60`）无任何 webhook 字段；全仓库检索 `QMX_WEBHOOK` / `webhook` 仅两处命中：`normalize.py:33` 的候选键名 + D3-A 协议文档自身。**无 `QMX_WEBHOOK_KEY` / `QMX_WEBHOOK_URL` 环境变量，无任何全局 webhook 配置** | `app/config.py` 全文 + 全仓 grep |
| 出站（机器人 → 平台） | `qmx.reply_text(reply_url, text)` 把载荷里的 URL **原样 POST**，无白名单、无凭据头、无实例登记 | `app/platform/qmx.py:39-55` |
| 线上 SQLite | `group_config` 12 列中**无任何 webhook / robot 实例字段**；18 张表中**无 `qmx_robot_instance` 类表**（只读 PRAGMA 实证） | 服务器 `/opt/group-ops/data/bot_state.db` 只读查询 |

### groupId 处理

- 回调里的 `group_id` **原样作为群键**贯穿全链路：`note_group_seen`（记账）→ `group_active(group_config)`（授权）→ `learn_user` → `HandlerContext.group_id`（`app/web/server.py:139-169`）。
- `group_config.group_id = TEXT PRIMARY KEY`（`app/state.py:113-114`）——**多群早已是一等公民**：D2 建群、群内「绑定」命令（`app/handlers/bind.py:65-72` 按真实 group_id 建行）、后台「发现但未绑定」全部按 group_id 分行存储。
- 线上实证：`group_config` 现存 1 行 `d2-acceptance-test`（active，D2 验收群，群名已标注「非真实群」）。

### 出站回复路径

| 出口 | 地址来源 | 是否按 groupId 查 | 现状 |
|---|---|---|---|
| `reply`（入站回推） | **载荷里的 reply_url**（`server.py:190-193` `sender.reply(inbound.reply_url, reply)`） | **否**——按消息，不按群 | 真实 HTTP，载荷 `{"text":...}` ⚠️ D11 假设 |
| `push_text`（主动推送/@） | 无——`qmx.py:63-69` **接收 group_id 但完全未使用** | 否 | fail-closed 返回 `(False, "platform push capability unconfirmed")` |
| `send_file`（群内文件） | 无——`qmx.py:73-80` 同样收 group_id 不用 | 否 | fail-closed 返回 `(False, "file send capability unconfirmed")` |

调用链：`main.py:131` 把 `sender.push_text` 注入调度器 → `reminder.tick → run_group → push(group_id, text)`（`app/handlers/reminder.py:259`）；`stats.py:80` 经 `ctx.sender.send_file`。`QmxAdapter` 为 `main.py:45` 的**单实例**（无参构造）——但这是对象单例，**不是单群假设**。

### 固定单群假设？

**不存在，且相反——当前架构是「多群就绪」的**：tokens / submit_intent / receipt / recovery_task / reminder_plan / group_member_task_config / report_field_pool / export_whitelist 全部以 group_id 为键分行；`reminder.tick` 逐群轮询（`app/handlers/reminder.py:277-284`）；绑定与后台均多群形态。

**唯一的真实缺口**：没有任何地方登记「哪个群 ↔ 哪个机器人实例 ↔ 哪个 webhook」——因为回推地址随消息而来，模型上暂时不需要；但 **push / file 两口将来必须需要**（它们只有 group_id，没有地址）。

---

## 2. 量子密信实际模型（用户确认）

- 一个机器人 = 一个群 = 一个 webhook 地址
- 不存在一个机器人服务多个群的场景
- webhook 地址属于**群机器人实例**，不是全局配置

---

## 3. 差异

### P0（不解决 → D3 假成功或上线即错）

1. **push / file 出站无地址来源**：`reminder`（group_id, text）与 `stats`（group_id, path）到 `qmx.py` 后无 webhook 可发。当前靠 fail-closed 兜底；**接通 push 之前，必须先有「群 → 机器人 webhook」的登记处**，否则 scheduler 一开就是「算得出、发不出」。
2. **回推地址零登记零比对**：reply_url 现取载荷原样使用。一对一模型下，绑定时应**登记该群机器人 webhook 并与回调所见地址比对**（不符 → 拒绝 + 安全事件）。结合 D10「入站无签名」，现状下任意人拿到地址即可伪造回推目标——两个缺口叠加才是完整风险面。
3. **机器人实例无标识**：op 日志 / 排查时分不清消息来自哪个机器人实例（V1 一进程一群形态下此项影响有限，但登记表应预留 robot_id）。

### P1

4. **绑定闭环缺 webhook 采集**：群内「绑定」（`bind.py:65-72`）与后台绑定目前只建 group_config 业务字段；一对一模型下绑定时应同时采集/确认该群机器人的 webhook（或至少 robot_id）。
5. **group_seen 与实例无关联**：未绑定群记账只有 group_id，将来无法回答「这个群的消息是哪个机器人转来的」。

### 无差异项（已就绪，⛔ 不必改造）

- 多群数据模型（group_config PK = group_id，各业务表按群分行）
- 入站解析（载荷仍含 group_id / phone / text / reply_url，实例模型不改解析）
- 管理后台群列表 / 配置 API / SPA
- 一进程一群的运行形态（进程与群的关系不进数据模型，实例表按群分行即可表达）

---

## 4. 最小改造方案（⛔ 未执行，仅评估）

### 方案 A：`group_config` 增加 3 列

`qmx_robot_id` / `qmx_webhook_url` / `qmx_webhook_key`。

- **改动面**：`state.py` DDL 3 列 + SCHEMA_VERSION v5→v6 迁移（`PRAGMA table_info` 守卫 + `ALTER TABLE ADD COLUMN` ×3，v4→v5 加 `bind_admins` 的同款先例 `state.py:427-430`）+ `upsert_group_config` 白名单 3 键（`state.py:696-698`）+ 管理后台 config API 读写（`admin/api.py:349-390`）+ `_config_payload`（`admin/api.py:789-812`）+ SPA 表单 + 测试。
- **优点**：读写一处；后台群配置页天然承载；V1 改动面最小。
- **缺点**：`group_config` 职责变宽（业务配置与平台实例凭证混表）；`webhook_key` 属敏感凭据，与 `biz_switches` 等同表 → 同备份面、同导出面、同审计面；未来「一群多机器人」（通知机器人 + 业务机器人）无法表达，需再迁移。

### 方案 B：新增 `qmx_robot_instance` 表，与 group_config 一对一

```sql
CREATE TABLE IF NOT EXISTS qmx_robot_instance (
    group_id      TEXT PRIMARY KEY,          -- 一对一：一群一机器人实例
    robot_id      TEXT,                      -- 平台侧机器人实例标识（可后补）
    webhook_url   TEXT NOT NULL,             -- 该群机器人的 webhook 地址
    webhook_key   TEXT,                      -- 签名密钥（待平台确认机制后启用，可空）
    created_at    TEXT,
    updated_at    TEXT NOT NULL
);
```

- **改动面**：新表 DDL（`_SCHEMA` 的 `CREATE TABLE IF NOT EXISTS`，v4→v5 加 `group_seen` 的同款先例 `state.py:431-432`——**旧库无需搬数据**）+ state 层 CRUD 方法 + 管理后台新端点/区块 + 绑定流程采集 + 测试。
- **优点**：**凭据与业务配置隔离**（备份 / 导出 / 审计面分开，符合本项目凭据纪律）；`qmx.py` 的 push/file 只依赖实例表（`group_id → webhook_url` 一次查询），不依赖业务配置表；未来一群多机器人可直接把 PK 扩成 `(group_id, robot_id)`；新表迁移模式比加列模式更简单（无 ALTER 分支）。
- **缺点**：多一张表 + 绑定流程多一步采集；V1 当前用量（1 个真实群）下偏重一点。

### V1 推荐：**方案 B，拆两步走**

**第一步（D3 前置最小集）**：建 `qmx_robot_instance` 表 + 绑定路径（群内「绑定」+ 后台绑定）写入 `webhook_url`（`robot_id` / `webhook_key` 留空，待平台答复）→ 解决 P0-2（回推地址登记 + 比对）。
**第二步（push 接通时）**：`qmx.py` 的 `push_text` / `send_file` 改查实例表替换 fail-closed → 解决 P0-1。

**为什么不是 A**：三个字段里两个是**平台实例凭证**，混入 `group_config` 会把凭据暴露面扩大到业务配置的备份 / 导出 / 审计通路上；而一对一约束用 `group_id` 作 PK 即可完整表达，不存在表达损失。A 的唯一优势（改动面小）在「凭据字段可能根本不存在」的前提下也不成立——若平台最终无签名机制，实例表退化为「群 ↔ webhook 地址」登记表，依然要建。

**前置依赖**：本方案不依赖平台文档即可执行第一步（webhook_url 从首次真实回调的 reply_url 采集或后台手填）；`webhook_key` 一列必须等 D3-A 问题清单第 8 问（签名机制）答复后再启用。

---

## 5. 保持验证（本轮实测）

| 项 | 状态 |
|---|---|
| MCP 服务（group-ops-mcp） | ✅ 未动（active，NRestarts=0） |
| bot 服务（group-ops-bot） | ✅ **未重启**（active，PID 不变） |
| `GOPS_ENABLE_SCHEDULER` | ✅ **0**（env 实测） |
| `normalize.py` / `qmx.py` / 其他业务代码 | ✅ 零修改 |
| 数据库 | ✅ 零修改（仅只读 PRAGMA / SELECT；WAL 文件时间为服务自身写入，非本轮） |
| 主动消息 / 平台接入 | ✅ 零 |

---

## 6. 结论

当前实现与量子密信实例模型的**数据模型差距 = 零**（多群早已就绪），**缺口全部集中在「群 → 机器人 webhook 无登记处」**，且只卡 push/file 两个尚未接通的口。回推（reply）路径因地址随消息而来，短期不受影响，但应借绑定时机完成登记比对以关闭 P0-2。推荐方案 B 两步走，第一步可在不依赖平台文档的前提下先行。
