# 阶段 D3-C 模块五：真实量子群端到端演练手册（手动执行）

> **状态：⛔ 未执行（待平台侧资源到位）**
> 本手册是 D3-C 模块五「真实量子群测试」的执行方案。开发侧（代码 + 本地测试）已全部完成，
> 但本模块的**核心验收必须发生在真实量子密信群 + 真实平台回调链路**上，
> 依赖三项目前未到位的资源，**无法由开发侧单独完成**，⛔ 不得以任何模拟手段替代。
>
> 依据：`deploy/qmx-callback-protocol.md`（D3-A 协议收敛）+ `deploy/d3b0-robot-instance-binding-design.md`
> （D3-B0 设计）+ `deploy/d3a5-qmx-robot-instance-report.md`（D3-A.5 实例模型核查）。

## 0. 前置条件（⛔ 三项未全齐不得开始）

| # | 条件 | 现状 | 责任方 |
|---|---|---|---|
| ① | **真实测试群**（量子密信内可拉机器人入群的群） | ❌ 未提供 | 用户 / 紫玉 |
| ② | **平台回调配置**（把测试群回调指向 `http://219.151.177.33:12334/api/callback`） | ❌ 未配置（D3-A Q1 待答复） | 紫玉 |
| ③ | **D3-A 协议确认**（字段名 / 消息唯一 ID / 重试语义 / 回推载荷 / push·file 能力，十二问中 P0 七问） | ❌ 未答复 | 紫玉 |

**附加前置**（开发侧可控，执行前复查）：

- [ ] 服务器双服务 active：`systemctl status group-ops-mcp group-ops-bot`（预期 NRestarts=0）
- [ ] `GOPS_ENABLE_SCHEDULER=0`（push 能力未确认前⛔ 不得置 1）
- [ ] 管理后台可访问（SSH 隧道 → `127.0.0.1:18003`），管理员账号已改过初密
- [ ] `GOPS_BIND_ADMIN_PHONES` 已填测试群管理员手机号（空 = 群内绑定全员不可用，fail-closed）
- [ ] DDL 已批准并执行（六表在 MySQL qsm 库）——⛔ 未批准则提交全部归 C 类，演练只做到「命令可达 + 拒绝话术正确」

## 1. 演练步骤

### Step 1 — T0 自动发现（群内首次触达）

1. 把机器人拉进真实测试群；
2. 在群内 @机器人 发送任意文本（如「在吗」）；
3. **预期**：机器人回推响应（未绑定群 = 200 静默压制或绑定引导话术，以 D3-A 协议为准）；

**服务端验证**（只读）：

```bash
# SSH 到服务器后（只读 SQLite，⛔ 不写）
sqlite3 /opt/group-ops/data/state.db "SELECT group_id, first_seen_at, last_seen_at FROM group_seen ORDER BY last_seen_at DESC LIMIT 5;"
sqlite3 /opt/group-ops/data/state.db "SELECT group_id, status, created_at FROM qmx_robot_instance;"
# 预期：新群出现 group_seen 行 + qmx_robot_instance 行 status=WAIT_BIND
```

### Step 2 — 群内命名（T1）

1. 测试群管理员发送：`绑定: 华东测试群`（严格格式，全角冒号亦可）；
2. **预期**：四态话术之一——成功（`robot_instance_named` 安全事件）/ 非管理员拒绝 / 用法错误 / 实例停用；
3. **负向用例**（各发一次，验证不误触业务）：「绑定: 打卡」= 命名为「打卡」⛔ 不触发打卡业务；
4. **服务端验证**：`SELECT group_id, display_name, status FROM qmx_robot_instance;` → display_name 已更新、status **仍为 WAIT_BIND**（T1 ⛔ 不改状态）。

### Step 3 — 后台绑定切 ACTIVE（T2）

1. 浏览器 → 群实例管理页（`/robot-instances`）；
2. 找到 WAIT_BIND 行 → 点「绑定」→ 填 Webhook 基址 + 备注名 → 确认；
3. **预期**：列表刷新为 ACTIVE 徽章；`/api/admin/robot-instances` 掩码载荷 `webhook_key_set:false`、`webhook_key_masked:"****"`；
4. **负向用例**：重复绑定 → 409；非法 URL（ftp://、内嵌 userinfo）→ 400；
5. **服务端验证**：`SELECT group_id, display_name, webhook_base_url, status FROM qmx_robot_instance;`。

### Step 4 — 群配置与应交人员

1. 群列表页 → 找到测试群 → 配置 tab：五业务开关 / period_type=daily / daily_cutoff=19:00 /
   打卡多时段区间 / 需图开关；
2. 成员 tab：导入真实应交人员（user_id 以平台回调学习到的为准）；
3. **预期**：保存即持久化（GET 复验一致）；时段重叠 → 400 拒绝（双防线之 API 校验）。

### Step 5 — 五业务真实链路（逐个，⛔ 不并行混测）

| 业务 | 操作 | 预期 |
|---|---|---|
| 打卡 | 群内发打卡命令 → H5 链接 → 手机提交 | 令牌校验 → A 类落库（MySQL checkin_record）→ 回执话术 |
| 打卡负向 | 非应交人员提交 / 越权令牌 | 规范拒绝话术（单一真源）+ 安全事件留痕 |
| 总结 | 群内发总结命令 → 提交 → 再提交 | 覆盖确认流（pending_session → 确认覆盖） |
| 报数 | 群内发报数命令 → H5 勾选字段提交 | 非负整数校验；字段快照留档 |
| 提醒 | 后台手动触发 `/reminders/run` | 差集计算正确；5 分钟冷却；⛔ scheduler 仍 0 |
| 导出 | 四个导出命令分别执行 | XLSX 落盘；白名单分权 |

每业务完成后 MySQL 只读核对一条：

```sql
SELECT * FROM checkin_record WHERE group_id='<真实群ID>' ORDER BY created_at DESC LIMIT 1;
```

### Step 6 — 全量回归 + 收尾

1. 开发机跑全量测试（预期 434+ 通过，⛔ 0 回归）；
2. DDL↔DAO 只读复核对（`tests/smoke/ddl_dao_check.py`）；
3. 服务器侧 invariant 复核：双服务 NRestarts=0 / scheduler=0 / 主动消息零发送；
4. **演练数据处置**：测试群数据⛔ 不删除（留作验收证据），但需在 §8 记录群 ID 与时间窗。

## 2. 失败判据（出现任一即停演并记录）

- 回调 400/500 且非预期（→ 协议假设破裂，回 D3-A 十二问补问）；
- 回推地址不是群所属 webhook（→ 实例模型假设破裂，停演）；
- 提交落库但回执话术缺失/错误（→ 话术链路问题，记录不复跑）；
- 平台重试导致重复记录（→ D3-A 已知隐患坐实，记录为 P0 缺陷）；
- 任何真实消息发到非测试群（→ ⛔ 重大事故，立即停演上报）。

## 3. 边界纪律（全程有效）

- ⛔ 不改 normalize.py / qmx.py / 业务处理器（演练只观测，修改须单独批准）；
- ⛔ 不开 scheduler（push 未确认）；
- ⛔ 不向非测试群发任何主动消息；
- ⛔ 不执行任何 DDL/DML 手改（数据只通过业务链路产生）；
- ⛔ 不把演练中获得的密钥/Token 写入任何文档、日志、记忆。
