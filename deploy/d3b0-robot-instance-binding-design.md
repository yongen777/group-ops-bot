# D3-B0 阶段量子机器人实例绑定与自动发现详细设计文档

| 项 | 值 |
|---|---|
| 日期 | 2026-10-15 |
| 上游 | D3-A.5 核查报告（推荐方案 B 两步走）——本文档 = **方案 B 第一步 + 第二步的接口预留**的完整设计 |
| 性质 | **纯设计产出**：数据模型 / 状态机 / 交互流 / API 契约 / 迁移方案 |
| 红线遵守 | ⛔ 未改任何业务代码（`normalize.py` / `qmx.py` / `commands.py` 等零改动）；⛔ 未建未改任何表（零 DDL/DML）；⛔ 未开 scheduler；⛔ 未发任何真实量子消息、未对接外部 API |

---

## 0. 设计定位与三门纪律（先定边界，再谈字段）

群维度现有三张表，职责**不得合并**（§2.11 三概念纪律的延续）：

| 表 | 职责 | 状态 |
|---|---|---|
| `group_seen` | 回调发现账本（任何出现过但未授权的群） | 已有，⛔ 不动 |
| **`qmx_robot_instance`（新）** | **平台实例登记**：这个群的机器人/webhook 凭证 + 备注名 + 实例状态 | 本文档设计 |
| `group_config` | **业务授权 + 业务配置**（`group_active` 才是授权判定） | 已有，⛔ 不动 |

**三门彼此独立**（实现与测试必须按此断言）：

1. **实例状态门**（新表 `status`）：只决定将来 push/file 出站能否使用该群登记的 webhook；
2. **业务授权门**（`group_config.status=active`）：决定业务命令是否被处理——⛔ 实例 ACTIVE **不等于**业务授权；
3. **业务开关门**（`biz_switches`）：决定单业务是否可用。

⚠️ **`reply` 回推路径完全不受实例状态影响**：回推地址来自每条消息载荷的 `reply_url`（`server.py:190-193`），实例 DISABLED 不阻断回推。此条必须写入实现期测试，防止把「停用实例」误解为「断群」。

---

## 1. `qmx_robot_instance` 数据模型与状态机

### 1.1 表结构（v6 新增）

```sql
CREATE TABLE IF NOT EXISTS qmx_robot_instance (
    group_id              TEXT PRIMARY KEY,      -- 来自回调的真实群标识（≤64）
    display_name          TEXT,                  -- 管理员备注名（可空；空 = 展示回退 group_id）
    webhook_base_url      TEXT,                  -- 发送网关基础地址（绑定后不可为空）
    webhook_key_encrypted TEXT,                  -- 凭证密文信封（可空；平台无签名机制时恒空）
    status                TEXT NOT NULL DEFAULT 'WAIT_BIND'
                          CHECK (status IN ('WAIT_BIND','ACTIVE','DISABLED')),
    created_at            TEXT,
    updated_at            TEXT NOT NULL
);
```

**字段规范**：

| 字段 | 类型/约束 | 规范 |
|---|---|---|
| `group_id` | TEXT PK | 来自回调原值，⛔ 不猜不改写；≤64（与 `_MAX_GROUP_ID` 一致，`admin/api.py:30`） |
| `display_name` | TEXT NULL | 管理员备注；≤64（与 `_MAX_NAME` 一致，`admin/api.py:31`）；**可空**——空值时所有展示回退 `group_id`（honest fallback，⛔ 不生成假名） |
| `webhook_base_url` | TEXT NULL→NOT NULL 语义 | scheme ∈ {http, https}（http 仅测试期，写入时记 warning）；⛔ 禁止 URL 内嵌 userinfo 凭据；去尾斜杠；≤512 |
| `webhook_key_encrypted` | TEXT NULL | 自描述密文信封 `v1:<b64 salt>:<b64 nonce>:<b64 ct+tag>`；NULL = 未设置或平台无签名机制；⛔ 任何 API/日志不得返回或打印明文 |
| `status` | TEXT + CHECK | `WAIT_BIND` / `ACTIVE` / `DISABLED`。新表无历史行，**CHECK 从建表日起生效**（比 group_config 的纯 TEXT 更硬，属有意识加强） |
| `created_at` / `updated_at` | TEXT | 沿用 `utcnow_iso()` 约定（与 group_config 同） |

**为什么这三个字段进独立表而不是 `group_config` 加列**（D3-A.5 已论证，此处固化）：三个字段中两个是平台实例凭证，混入业务配置表会把凭据暴露面扩大到业务配置的备份/导出/审计通路；且加列迁移必须走 `PRAGMA table_info` 守卫 + `ALTER` 分支（`bind_admins` 先例，`state.py:427-430`），新表走 `CREATE TABLE IF NOT EXISTS` 零风险（`group_seen` 先例，`state.py:431-432`）。

### 1.2 状态机

```
                    ┌──────────────────────────────────────────┐
                    │            （无删除迁移 ⛔）              │
                    ▼                                          │
[首条未知 groupId 回调]──T0──▶ WAIT_BIND ──T2 绑定(API)──▶ ACTIVE
                                │  │                        │  ▲
                        T1 命名│  │T3 禁用(API)      T3 禁用 │  │T4 启用(API)
                                ▼  ▼                        ▼  │
                             (display_name)            DISABLED
```

| 迁移 | 触发 | 前置条件（guard） | 效果 | 留痕 |
|---|---|---|---|---|
| **T0 自动发现** | 回调骨架，`note_group_seen` 之后 | `group_id` 非空 | `INSERT OR IGNORE` 建 `WAIT_BIND` 行（幂等；重复回调不重置） | 仅 INFO 日志（高频，⛔ 不写审计表） |
| **T1 命名** | 群内固定指令 `绑定: <备注>`（§2）或 API `PUT` | 实例存在；status ∈ {WAIT_BIND, ACTIVE}；发送人是绑定管理员（群内路径） | 仅更新 `display_name`，**⛔ 不改 status** | 群内路径：`security_event(robot_instance_named)`；API 路径：`admin_audit(robot_instance.rename)` |
| **T2 绑定激活** | API `POST .../bind` | status = WAIT_BIND（否则 409）；`webhook_base_url` 通过校验 | status → ACTIVE；写入 url（+可选 key/name） | `admin_audit(robot_instance.bind)` |
| **T3 禁用** | API `POST .../disable` | 任意状态 | status → DISABLED | `admin_audit(robot_instance.disable)` |
| **T4 启用** | API `POST .../enable` | status = DISABLED；`webhook_base_url` 已设置（否则 400） | status → ACTIVE | `admin_audit(robot_instance.enable)` |

### 1.3 对指令原文的精确化（一处有意细化，请审核）

指令原文的状态流转为「管理员绑定 Webhook 凭证**并命名** → ACTIVE」。本设计将 ACTIVE 的**硬前置定义为 `webhook_base_url` 有效**（API 绑定动作在同一请求里可同时提交 `display_name`，一步完成「凭证+命名」），而群内「绑定: 备注」定位为**辅助命名通道**，⛔ 命名本身不触发状态迁移。理由：

1. **防脆弱**：若 ACTIVE 依赖「管理员记得在群里发一条命令」，忘记发命令的群将永远停在 WAIT_BIND，push 能力静默不可用；
2. **防越权面**：状态迁移只走带 CSRF + 会话 + 审计的后台 API，聊天指令只能改展示字段；
3. **不丢能力**：绑定 API 一步同时收凭证与备注名，指令描述的 happy path 完整保留。

若审核要求严格执行「必须命名才 ACTIVE」，备选方案是在 T2 增加 `display_name IS NOT NULL` 守卫——**记录在案，默认不采用**。

---

## 2. 「带备注的辅助自动发现」交互流

### 2.1 自动发现（代码集成点规范，⛔ 现在不实现）

```
回调骨架（server.py，现有顺序）：
  normalize → note_group_seen → 【新增：ensure_robot_instance(group_id)】 → 绑定命令预解析 → 群授权 → …
```

- `ensure_robot_instance(group_id)`：`INSERT INTO qmx_robot_instance(group_id, status, created_at, updated_at) VALUES(?, 'WAIT_BIND', ?, ?) ON CONFLICT(group_id) DO NOTHING`；
- **幂等**：重复回调零副作用（⛔ 不重置 display_name / status）；
- **健壮性**：纯记账，⛔ 必须 try/except 包裹——记账失败只记日志，**不得影响回调主流程**（记账不是授权，授权判定在 group_config）；
- 与 `group_seen` 并存：group_seen 仍是发现账本（含 last_phone/msg_count），实例表只登记「群 ↔ 机器人」关系，⛔ 不重复记账字段。

### 2.2 固定指令 `绑定: <群备注名>`（严格字符串，⛔ 无 LLM、⛔ 无模糊匹配）

**匹配规范**：

| 项 | 规范 |
|---|---|
| 字面前缀 | `绑定:`（ASCII 冒号）。`normalize_text` 的 NFKC 已将全角 `：` 归一为 ASCII（`commands.py:122`），故 `绑定：XX` 自动等价，⛔ 不需要第二套前缀 |
| 集成点 | **必须置于 `commands.parse` 现有 `BIND_COMMANDS` 循环之前**。原因（实测）：现有分支 `text.startswith("绑定")` 会先把 `绑定: XX` 吞下——`rest=": XX"` 非空 → 返回 `RESULT_BAD_FORMAT, command="绑定"` → 回复绑定用法，新指令永远不可达 |
| 备注名取值 | 前缀后 `remainder.strip()`；1~64 字符；⛔ 不含换行；⛔ 不再拆分（含业务命令词也按备注存——备注是展示字段，不参与解析） |
| 空备注 | `绑定:` 后为空 → `RESULT_BAD_FORMAT` → 固定用法文案 |
| 鉴权 | 与「绑定」同源：`_is_bind_admin`（`bind.py:36-39`，`config.bind_admin_phones`；**空 = 全员不可用，fail-closed**） |
| 处理阶段 | 与 `CMD_BIND` **同在群授权之前**（未授权群也要能命名——命名是实例登记，不是业务授权）；优先于总结两轮 pending 处理 |
| 状态门 | 实例 ∈ {WAIT_BIND, ACTIVE} 可命名；DISABLED → 拒绝话术；无实例记录 → 先 `ensure` 再命名（幂等，不报错） |
| 留痕 | `security_event("robot_instance_named", group_id, phone)`（与 `group_bound` 同模式，`bind.py:73-74`） |

**回复文案（四态，唯一来源 = commands 模块文案表扩展）**：

| 状态 | 文案（设计稿） |
|---|---|
| 成功 | `已记录群备注名：{name}（群标识 {gid}）` |
| 非绑定管理员 | 复用 `NOT_ADMIN_TEXT` 同款（⛔ 不透露实例状态细节） |
| 格式错误（空备注/超长） | `用法：@机器人 发送「绑定: 群备注名」（备注名 1-64 字，仅绑定管理员可用）` |
| 实例已停用 | `本群机器人实例已停用，请先到管理后台启用` |

**边界案例表**（实现期测试直接取用）：

| 输入（归一化后） | 预期 |
|---|---|
| `绑定` | 原群绑定命令（**行为不变**） |
| `绑定:` | bad_format → 用法文案 |
| `绑定: 华东巡检群` | 命名成功，display_name=华东巡检群 |
| `绑定：华东巡检群`（全角冒号） | NFKC 后同上，成功 |
| `绑定: 打卡` | 命名为「打卡」，⛔ 不触发打卡业务 |
| `绑定: A群 打卡` | 备注名 = 「A群 打卡」（整体取值，⛔ 不拆命令） |
| `绑定:` + 65 字符 | bad_format → 用法文案 |
| 非管理员发 `绑定: X` | 无权话术 + security_event |

---

## 3. 后端管理 API 与凭证安全设计

### 3.1 端点契约（沿用现有 admin 约定）

所有端点：`/api/admin/robot-instances` 前缀；必须过 `_identity` + `_require_password_changed` + `_require_csrf`（`admin/api.py:63,181,186`）；响应统一 `{ok: true, ...}`；错误 `{detail: ...}` + 400/404/409；全部写 `admin_audit`。

| 方法 + 路径 | 请求体 | 成功响应 | 错误码 |
|---|---|---|---|
| `GET /robot-instances?status=` | —（status 可选过滤 WAIT_BIND/ACTIVE/DISABLED） | `{ok, instances:[…掩码项…]}` | 400 非法 status |
| `GET /robot-instances/{group_id}` | — | `{ok, instance:{…掩码项…}}` | 404 无记录 |
| `POST /robot-instances/{group_id}/bind` | `{webhook_base_url, display_name?, webhook_key?}` | `{ok, instance:{…}}`（status=ACTIVE） | 404 无记录；409 已 ACTIVE（改用 PUT）；400 url 非法 |
| `PUT /robot-instances/{group_id}` | `{display_name?, webhook_base_url?, webhook_key?}` | `{ok, instance:{…}}` | 404；400 尝试清空 url / 非法值 |
| `POST /robot-instances/{group_id}/disable` | — | `{ok, instance:{…}}` | 404 |
| `POST /robot-instances/{group_id}/enable` | — | `{ok, instance:{…}}` | 404；400 url 未设置 |

**设计决策**：⛔ V1 **不支持后台手工预建实例**（无 POST 创建端点）——实例必须由真实回调发现（T0），与「用回调里的真实 group_id、不让手填」的既有纪律一致（`bind.py:7`）。预建能力记为后续选项。

**掩码载荷项**（所有响应共用）：

```json
{
  "group_id": "…",
  "display_name": "华东巡检群" | null,
  "webhook_base_url": "https://…" | null,
  "webhook_key_set": true,
  "webhook_key_masked": "****",
  "status": "WAIT_BIND",
  "created_at": "…", "updated_at": "…"
}
```

- `webhook_key_masked` **固定 `****`**（已设置时）或 `null`（未设置）——⛔ 不返回前后缀、不返回长度线索；
- `webhook_key_encrypted` 列值**任何接口不返回**；
- 审计脱敏：`_audit_safe`（`admin/api.py:815-817` 当前原样透传）必须新增 `webhook_key` 红黑名单过滤——**这是实现期必改项，本文档将其列为硬约束**（当前实现会把 key 写进审计 detail，属已知缺口，⛔ 不得带着上线）。

### 3.2 凭证安全（`webhook_key`）

**威胁模型**：SQLite 文件泄露（备份/导出/误共享）时 key 不裸奔；进程内存外不出现明文。

**方案（实现期，触发条件见下）**：

| 项 | 规范 |
|---|---|
| 算法 | AES-256-GCM（认证加密，防篡改） |
| KEK 来源 | env `GOPS_QMX_KEYSTORE_PASSPHRASE`（⛔ 不进 DB/日志/文档；与 MySQL 密码同纪律：只进 `/etc/group-ops/group-ops-bot.env` root:600） |
| 派生 | PBKDF2-HMAC-SHA256，逐行随机 salt（≥16B） |
| 信封格式 | `v1:<b64 salt>:<b64 nonce>:<b64 ct+tag>`，自描述 → 支持未来算法轮换（v2）而不改表 |
| 写入失败 | env 缺失且请求带 key → **fail-closed 拒写**（400/500 + 明确原因），⛔ 绝不明文落库 |
| 解密时机 | 仅 push/file 出站调用时（qmx.py 实现期）；⛔ 不进日志、不进 API 响应 |
| 轮换 | 换 passphrase = 逐行解密→重加密的管理动作（实现期单独设计，本文档只留信封版本位） |

**依赖与触发条件（重要）**：加密层需要 `cryptography` 包——**新增 venv 依赖，须单独批准**。平台签名机制尚未确认（D3-A 问题清单第 8 问），因此：

- **v6 先落表 + `webhook_key_encrypted` 可空列，加密层不实现**（零新依赖，表结构就位）；
- 平台确认签名机制后，再实现加密层 + 依赖安装 + 绑定 API 开放 key 字段。

**备选方案评估（记录在案）**：

| 备选 | 结论 | 理由 |
|---|---|---|
| key 只放 env 文件、不落库 | ❌ 否 | 一进程一群时可行为；实例模型下多群多 key 不可扩展、轮换要重启 |
| 明文落库 | ❌ 绝对禁止 | 违反项目凭据纪律（配置模块自身都不打印配置值，`config.py:8`） |
| 应用层哈希（不可逆） | ❌ 否 | 出站签名需要原文，哈希不可用 |

**文件层既有防护**（沿用，不新增）：data 目录 750 groupops、`bot_state.db`/-wal/-shm 同权限；备份须知情含密文。

---

## 4. 数据库迁移方案评估（v5 → v6）

**结论：采用非破坏性新增表方案，⛔ 不碰任何现有表。**

| 步骤 | 内容 | 先例 |
|---|---|---|
| 1 | `_SCHEMA` 增加 `CREATE TABLE IF NOT EXISTS qmx_robot_instance`（§1.1 DDL） | `group_seen` 入 v5（`state.py:431-432`） |
| 2 | `SCHEMA_VERSION = 5 → 6`（`state.py:37`） | 同款 |
| 3 | `_migrate` 增加 `v5 → v6` 分支：**仅** `UPDATE meta SET value='6'`——`executescript` 已在旧库建好新表，**无需搬数据、无需 ALTER** | `group_seen` 同款（旧库无需迁移注释） |
| 4 | 旧库升级后新表为空行——✅ 预期（发现靠 T0 自然积累） | — |

**为什么不选「group_config 加列」**：加列必须 `PRAGMA table_info` 守卫 + `ALTER TABLE ADD COLUMN` 分支（`bind_admins` 先例），且把凭证混入业务表（§1.1 末）。已知坑复盘：`executescript` 的 `CREATE IF NOT EXISTS` **不会给已存在的表加列**——本方案零加列，正落在安全区。

**实现期验收判据**（现在不执行，登记为准）：

1. 全新库：直建含新表，schema_version=6；
2. v5 旧库（可用当前服务器库文件副本验证）：原地升级后 `group_config`/`tokens` 等**行数与内容逐字节不变**；
3. 新表 CRUD + 状态机守卫单测；
4. 全量测试（现 372 项）+ 新增迁移测试全过；
5. 回滚 = 停用新代码路径（表留空挂账），**非破坏性**，无需 DROP。

---

## 5. 实现期改动清单（⛔ 设计轮不做；**D3-B1 执行状态：2026-10-15**）

| # | 文件 | 改动 | 对应章节 | D3-B1 状态 |
|---|---|---|---|---|
| 1 | `app/state.py` | `_SCHEMA` 加表；`SCHEMA_VERSION=6`；`_migrate` v5→v6 分支；CRUD 七法（ensure/get/list/bind/update/set_status/set_name）+ url/名校验器 | §1 §4 | ✅ 已完成 |
| 2 | `app/commands.py` | 新命令常量 + 前置 `绑定:` 分支 + 文案表扩展 | §2.2 | ✅ 已完成 |
| 3 | `app/web/server.py` | `note_group_seen` 后接 `ensure_robot_instance`（try/except 包裹）；bind 同级分派命名命令 | §2.1 §2.2 | ✅ 已完成 |
| 4 | `app/handlers/robot_name.py`（新） | 命名处理器（鉴权复用 `_is_bind_admin` 同源逻辑） | §2.2 | ✅ 已完成 |
| 5 | `app/admin/api.py` | 六个端点 + 掩码载荷 + `_audit_safe` 红黑名单（key 脱敏硬约束） | §3.1 | ✅ 已完成 |
| 6 | admin SPA（`admin/static/`） | 实例管理区块（列表/绑定表单/禁用启用） | §3.1 | ⏳ **D3-B1 未做**（用户指令任务清单只含 API 层；SPA 属下一批，后端契约已就绪可独立开发） |
| 7 | `deploy/env-reference.md` | `GOPS_QMX_KEYSTORE_PASSPHRASE` 条目（加密层实现时） | §3.2 | ⏳ **随加密层**（平台签名机制确认后；V1 key 恒空） |
| 8 | 测试 | 命令解析边界案例（§2.2 表）、状态机守卫、迁移三态、掩码断言、审计脱敏断言 | 全部 | ✅ 已完成（新增 `test_robot_instance.py` 26 项；全量 398 通过） |
| 9 | `项目开发方案与进度.md` §8.14 | 实现结果回填 | — | ✅ 已完成 |

**实现前置条件复盘**：① 本设计审核通过 ✅（用户 2026-10-15 批准进入 D3-B1）② 平台对 D3-A 十二问的答复 ⏳ **仍阻塞**——本轮按「不依赖平台文档即可先行」的第一步落地（webhook_base_url 登记 + 状态机 + 命名），`robot_id`/`webhook_key` 两列按设计恒空/留空，平台答复后不影响已落地结构 ③ 加密层单独批准 ⏳ 未触发（V1 绑定 API **拒收** webhook_key，fail-closed）。

**D3-B1 落地口径与设计的偏差（如实记录）**：
- `qmx_robot_instance` 表按设计七列落地，⛔ 未加 `qmx_robot_id` 列（设计 §1.1 定稿字段集本就不含；D3-A.5 方案 B 提及的 robot_id 留待平台答复，加列走既有 `PRAGMA table_info` 守卫先例）；
- 绑定 API 对 `webhook_key` **显式 400 拒收**（设计 §3.2「v6 先落表 + key 列可空」的 fail-closed 落地形态），审计 detail 经 `_audit_safe` 红黑名单剔除凭证键（纵深防御）；
- `set_robot_instance_status` 对 WAIT_BIND 设值 raise ValueError（⛔ 不对外提供手工回退到发现态）。

---

## 6. 风险与开放问题

| # | 项 | 等级 | 说明 |
|---|---|---|---|
| 1 | 平台签名机制未确认 | P1 | key 列恒空不影响 v6 落地；确认后才实现加密层 + 依赖 |
| 2 | 实例状态与群状态的语义混淆 | P1 | 三门纪律（§0）+ 回复话术区分 + 实现期专项测试缓解 |
| 3 | 后台无预建端点，纯发现驱动 | P2 | 已知群（如验收群）也要等一条真实回调才登记——可接受，记为后续选项 |
| 4 | `display_name` 经 NFKC 归一化存储 | P2 | 全角字母数字会变半角；备注场景可接受，文案明示 |
| 5 | 未来一群多机器人 | P2 | PK 从 `group_id` 扩为 `(group_id, robot_id)` 即可，信封与状态机不用改 |
| 6 | webhook_base_url 的 SSRF 面 | P2 | URL 由管理员经后台写入（非用户输入），风险受控；后续可选 host 白名单 |

---

## 7. 本轮边界核验

| 项 | 状态 |
|---|---|
| 业务代码（含 `normalize.py` / `qmx.py` / `commands.py` / `state.py` / `server.py`） | ✅ 零修改 |
| 数据库 DDL / DML | ✅ 零执行 |
| scheduler / 定时任务 | ✅ 未开启（`GOPS_ENABLE_SCHEDULER=0` 维持） |
| 真实量子消息 / 外部 API | ✅ 零发送零对接 |
| MCP / bot 服务 | ✅ 未重启未改动 |

本文档为设计产物（`deploy/` 侧），实现须待审核批准后按 §5 清单执行。
