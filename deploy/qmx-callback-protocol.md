# 量子密信回调接口协议收敛（阶段 D3-A）

| 项 | 值 |
|---|---|
| 日期 | 2026-10-14 |
| 性质 | **接口契约导出 + 假设差异表 + 平台侧现状核查**（⛔ 非联调报告） |
| 边界 | ⛔ 未改任何业务代码（含 `normalize.py` / `qmx.py`）；⛔ scheduler 保持 0；⛔ 未发送主动消息；⛔ 未接平台 |
| 阻塞判定 | **量子密信真实协议未知 → 阶段 D3（五业务真实链路）不具备开工条件**，本文档 = 解除阻塞所需的问题全集 |

---

## 1. 回调接口完整协议（当前实现导出）

### 1.1 端点与方法

| 项 | 值 | 代码证据 |
|---|---|---|
| URL | `POST http://219.151.177.33:12334/api/callback`（测试服务器生产形态；开发期同构 `http://<host>:18002/api/callback`） | `app/web/server.py:118` |
| Content-Type | `application/json`，body 必须是 JSON **object**（数组/标量 → 400） | `app/web/server.py:121-129` |
| 入站鉴权 | **无**（无签名 / token / 时间戳 / IP 限制） | `app/web/server.py:118-193` 全链路无鉴权代码 |
| 公网可达性 | ✅ 已实证（阶段 C：开发机外部视角 HTTP 200） | §8.14 阶段 C 记录 |

### 1.2 请求字段

**必需 4 项**（缺任一 → `PayloadError` → HTTP 400 + 安全事件 `payload_error`）：

| 语义字段 | 平级候选键名（取第一个非空） | 嵌套候选（`a.b.c` 路径） |
|---|---|---|
| `group_id` | `group_id` / `groupId` / `group` / `room_id` / `roomId` | `data.group_id` / `data.groupId` / `message.group_id` / `message.groupId` / `data.group.id` / `group.id` |
| `phone` | `phone` / `mobile` / `phone_number` / `phoneNumber` / `sender_phone` | `data.phone` / `data.sender.phone` / `message.sender.phone` / `sender.phone` / `data.mobile` / `message.mobile` |
| `text` | `text` / `content` / `message` / `msg` / `body` | `data.text` / `data.content` / `message.text` / `message.content` / `data.message` / `message.body` |
| `reply_url` | `reply_url` / `replyUrl` / `callback_url` / `callbackUrl` / `webhook` / `push_url` | `data.reply_url` / `data.replyUrl` / `message.reply_url` / `data.callback_url` / `message.callbackUrl` |

**可选 2 项**（取不到为 `None`，⛔ 不猜不补）：

| 语义字段 | 候选键名 |
|---|---|
| `platform_user_id` | `user_id` / `userId` / `platform_user_id` / `platformUserId` / `sender_id` + `data.*` / `message.*` / `sender.*` 嵌套 |
| `name` | `name` / `nickname` / `nick_name` / `display_name` / `sender_name` + 嵌套 |

取值规则：候选键顺序取第一个非空；数字型值转字符串；⛔ 不做语义猜测。证据：`app/normalize.py:29-36, 52-83, 87-101, 104-138`。

### 1.3 处理骨架（收到回调后的顺序）

```
① JSON 解析        失败 → 400 invalid json body + security_event(payload_error)
② normalize        缺必需字段 → 400 invalid payload + payload_error
③ note_group_seen  未绑定群也记账（后台「发现但未绑定」列表；⛔ 本表不是授权）
④ 绑定命令预解析    「绑定」命令优先于群授权（未绑定群也要能进绑定闭环）
⑤ 群授权 group_active  未绑定/已停用 → 200 {"ok":true,"replied":false} 静默
                                       + security_event(unauthorized_group)
⑥ learn_user       入站学习（授权群才走）
⑦ 命令解析 → 分派   dispatch / 两轮总结待处理优先 / 未知命令话术
⑧ handler 异常     → 500 internal error + security_event(handler_error)
⑨ best-effort 回推  POST reply_url（失败只记 warning，⛔ 不改写已完成的 A/B/C 归类）
```

证据：`app/web/server.py:118-193`。

### 1.4 响应与错误码

| HTTP | 触发条件 | 响应体 | 留痕 |
|---|---|---|---|
| 200 | 正常处理（有回复） | `{"ok": true, "replied": true}` | — |
| 200 | 正常处理（无回复内容） | `{"ok": true, "replied": false}` | — |
| 200 | **未绑定 / 已停用群（静默压制）** | `{"ok": true, "replied": false}` | `security_event(unauthorized_group)` |
| 400 | body 非合法 JSON / 非 object | `{"detail": "invalid json body"}` | `security_event(payload_error)` |
| 400 | 缺必需字段 | `{"detail": "invalid payload"}` | `security_event(payload_error)` |
| 500 | 命令处理器内部异常 | `{"detail": "internal error"}` | `security_event(handler_error)` |

（关联端点：H5 打卡图片上传 `POST /api/h5/checkin/upload` → **501**，§4.11.4 通道未定夺，`app/web/server.py:214-221`。）

### 1.5 出站回推契约（现状 = D11 假设）

| 项 | 值 | 证据 |
|---|---|---|
| 调用方式 | `POST {reply_url}`，`json={"text": <回复文本>}`，超时 10s | `app/platform/qmx.py:39-55` |
| 失败处理 | 连接异常 / 非 2xx → warning 日志 + 返回 `False`（best-effort） | 同上 |
| 回推域名 | **无白名单**——reply_url 原样使用，平台给什么就发什么 | 同上（D11 文档提及"回推域名白名单"待定） |

⚠️ **载荷形态 `{"text": ...}` 是假设**：平台若要求 `msg_type` / `token` / 其他字段，回推将静默失败（只记 warning，用户侧看不到回复）——这正是"假成功"风险的一部分。

### 1.6 出站另两口（fail-closed，⛔ 未接通）

| 能力 | 现状返回 | 证据 |
|---|---|---|
| `push_text`（主动推送 / @） | `(False, "platform push capability unconfirmed (see 5.2/D10)")` | `app/platform/qmx.py:63-69` |
| `send_file`（群内文件，统计导出用） | `(False, "file send capability unconfirmed (see D12)")` | `app/platform/qmx.py:73-80` |

---

## 2. 幂等性分析（message_id 缺口的精确影响面）

**代码事实**：每次回调处理提交时，`request_id = new_request_id() = uuid4().hex`（`app/handlers/base.py:99-101, 165`）——幂等键**完全由机器人自行生成，与平台消息无任何关联**。入站侧没有任何地方读取平台消息 ID（`normalize.py` 不提取、`InboundContext` 不携带——`raw` 虽保留原载荷但无消费方）。

**平台重试同一命令时的精确后果**：

| 场景 | 后果 | 依据 |
|---|---|---|
| 打卡：首次已 A 类成功，重试到达 | 被业务范围键挡住 → B 类拒绝话术，**不会双记录** | `run_submission` 的 `has_successful_scope` 预判 |
| 打卡/总结/报数：首次提交**尚在途（pending/unknown）**时重试到达 | 两个独立 request_id 双双落库 → **重复记录** | 已知未修隐患：`run_submission` 只挡 `has_successful_scope`，**不挡 pending** |
| 总结两轮状态机 / 恢复任务 | 重复回调会重复驱动状态机与恢复流程 | `app/handlers/summary.py` / `app/recovery.py` |

**结论**：入站幂等必须依赖平台侧消息唯一 ID（`message_id` / `event_id` / `request_id` 之类）。在拿到该字段之前，任何"真实链路测试成功"都可能把"平台重试的第二次"算成一次独立成功——**这是 D3 前必须澄清的第一问题**。

---

## 3. 代码假设 vs 平台待确认差异表

| # | 项 | 代码现状（假设） | 平台可能形态 | 待确认问题 | 等级 | 阻塞 |
|---|---|---|---|---|---|---|
| 1 | 入站字段名 | 5+5+5+6 候选键平级匹配 | `chatId`/`sender`/`content`/`callback`/`msgId` 等任意命名 | 能否给一份真实回调 JSON 示例或字段文档？ | **P0** | D3 全链路 |
| 2 | 嵌套结构 | `data.*` / `message.*` / `sender.*` 两层兼容 | 可能更深 / 更浅 / 平铺 | 同上 | **P0** | D3 全链路 |
| 3 | `reply_url` 语义 | 逐消息回推地址，直接 POST | 是否逐消息不同？是否要带 token/签名头？域名是否固定？ | 回推怎么调？载荷格式要求？ | **P0** | 回复可见性 |
| 4 | 消息唯一 ID | **无**（§2 全析） | `message_id`/`event_id`/`request_id` 可能存在 | 有没有？字段名？ | **P0** | 幂等方案 |
| 5 | 重试语义 | 未知 | 超时时间 / 重试次数 / 间隔 | 平台多久重试一次？ | **P0** | 幂等窗口 |
| 6 | 群 ID 形态 | 字符串原样作为 `group_id`（与管理后台建的群 ID 直接对应） | 平台内部群标识（可能与群名不同） | 平台侧群标识是什么？绑定闭环怎么对应？ | **P0** | 群绑定闭环 |
| 7 | `phone` 真实性 | 当自然键存入 `sys_user.phone` | 可能是平台虚拟号 / 脱敏号 | 回调带的是真实手机号吗？ | P1 | 人员目录键 |
| 8 | 签名 / 鉴权 | **零验证**，唯一"授权"=群已绑定 | 可能 HMAC 签名 / token / IP 白名单 | 平台是否签名？机制？（D10） | P1 | 安全收口 |
| 9 | HTTP 明文 | `http://219.151.177.33:12334` 已可达 | 平台可能要求 HTTPS | 明文 HTTP 可接受吗？（B1/D9） | P1 | 生产上线 |
| 10 | 回推载荷 | `{"text": ...}` | 可能要求 `msg_type` / `token` / @ 字段 | 载荷格式要求？（D11） | **P0** | 回复可见性 |
| 11 | push / @ | fail-closed | 端点 / 鉴权 / 字段未知 | 有没有主动推送接口？（§5.2/D10） | P1 | scheduler 开启 |
| 12 | file 消息 | fail-closed | multipart / URL 引用 / 大小上限未知 | 能不能群里发 xlsx？（D12） | P2 | 统计导出 |
| 13 | 图片上传 | H5 端点 501（§4.11.4 未定夺） | 平台可能有图片消息通道 | 通道与存储载体定夺 | P2 | 打卡需图场景 |

---

## 4. 平台侧现状核查结果（本轮实测）

| 核查项 | 结果 |
|---|---|
| 量子密信平台 API 文档（仓库 / 项目文档存档） | ❌ **无**——`项目开发方案与进度.md` 与代码库均无平台侧协议文档 |
| 测试群记录（群 ID / 群名） | ❌ **无** |
| 测试账号记录 | ❌ **无** |
| 回调配置入口记录（平台侧在哪配 webhook、谁有权限） | ❌ **无** |
| 已知联系人路径 | ✅ **紫玉**——测试服务器 `219.151.177.33:12334` 提供方；历史负责公网 IP / 端口映射办理（§2.9） |
| 既有协同通道事实 | ✅ 2026-10-13：管理后台经**内网直连** `http://192.168.190.43:18003` 供产品实测（用户与紫玉同一局域网）——说明存在"产品实测"协同机制 |
| 服务器回调口公网可达 | ✅ 已实证（阶段 C，HTTP 200） |
| 旧话术状态 | ⚠️ §2.9 既定话术（line 389-393）针对"开发机 `192.168.190.43:18002` 映射"**已过时**——现形态为服务端直驻测试服务器，见 §6 更新版 |

---

## 5. 待平台确认问题清单（按优先级，建议一次性问全）

**P0——D3 开工前置**：

1. **回调配置入口**：平台侧在哪里配置 webhook 地址？需要什么权限 / 流程？我们填 `http://219.151.177.33:12334/api/callback`（POST JSON）是否可行？
2. **真实入站载荷**：能否给一份回调 JSON 示例（或字段文档）？特别是群标识、发送者手机号、消息文本、回推地址的字段名。
3. **消息唯一 ID**：回调里有没有 `message_id` / `event_id` / `request_id` 类字段？字段名是什么？
4. **重试语义**：平台超时时间、重试次数、间隔分别是多少？
5. **回推契约**：`reply_url` 怎么调用？`POST {"text": ...}` 是否可接受？要不要 `msg_type` / token？回推域名是否固定？
6. **群 ID 对应**：平台侧群标识是什么形态？与我们在管理后台预建的群 ID 如何对应（绑定闭环）？
7. **手机号真实性**：回调携带的手机号是真实手机号还是平台虚拟号？

**P1——安全与上线前**：

8. 平台是否对回调签名？机制是什么？（D10）
9. 明文 HTTP 回调是否可接受，还是必须 HTTPS 域名？（B1/D9）
10. 平台有没有主动推送 / 艾特接口？端点 / 鉴权 / 字段？（§5.2）

**P2——能力补齐**：

11. 平台能不能在群里发文件（xlsx）？形态与大小上限？（D12）
12. 能否提供测试群 + 测试账号供联调？（⛔ 不在生产群测）

---

## 6. 建议话术（用户转发紫玉，基于服务器现状更新）

> 紫玉好，群运维机器人的服务端已经部署在测试服务器上了（公网 `219.151.177.33`，业务端口 `12334`，现在就能通）。准备开始平台回调联调，想确认几个协议细节：
>
> 1. 回调地址在平台侧哪里配置、怎么配？我们这边是 `http://219.151.177.33:12334/api/callback`（POST JSON）。明文 HTTP 可以用吗，还是必须 HTTPS 域名？
> 2. 能不能给一份回调 payload 的示例或字段文档？特别想知道：群标识、发送者手机号、消息文本、回推地址分别是什么字段名；回调里有没有 message_id 这类消息唯一 ID？
> 3. 平台的重试策略是怎样的（超时多久、重试几次）？
> 4. 回推（reply_url）的调用方式和载荷格式有什么要求？
> 5. 平台有没有主动推送 / 艾特的接口？能不能在群里发文件（xlsx）？
> 6. 有没有测试群和测试账号可以供联调？我们不在生产群里测。
>
> 辛苦啦！

---

## 7. 本轮边界与 invariant 核验（服务器实测）

| 项 | 状态 |
|---|---|
| 业务代码改动 | ⛔ 零（`normalize.py` / `qmx.py` / 处理器均未动） |
| `GOPS_ENABLE_SCHEDULER` | ✅ **0**（`/etc/group-ops/group-ops-bot.env` 实测） |
| 主动消息发送 | ⛔ 零（push_text fail-closed 原样） |
| 平台接入 / 真实回调 | ⛔ 未做（阶段 D1 的本地 synthetic 三发自测除外，已在 §8.14 记录） |
| 双服务状态 | ✅ group-ops-mcp / group-ops-bot 均 active，NRestarts=0，`/healthz` = ok |

**遗留探针痕迹**（机器人 SQLite，非授权记账，无害可查）：`d1-probe-unbound`（回调自测群）、`d2-acceptance-test`（D2 验收群，群名已标注"非真实群"）——后台「发现但未绑定」列表可见，联调前可决定保留（作验收证据）或清理。
