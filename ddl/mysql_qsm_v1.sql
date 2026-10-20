-- =============================================================================
-- 群运维机器人 · MySQL `qsm` 建表脚本 v1
--
-- ⚠️⚠️ 本文件是**交付清单**，⛔ 未经用户明确批准不得对任何库执行 ⚠️⚠️
-- （qsm 为共享库，DDL 属高危操作；批准前只评审不执行）
--
-- 依据：D:\group-ops\项目开发方案与进度.md §4.11.2（数据模型字段设计）
-- 实测环境（2026-10-12，只读验证）：MySQL 5.7.43-log / utf8mb4_general_ci / CST(UTC+8)
-- 代码对应：mcp_server/db/dao.py（列名、表名、唯一键名必须与本文件一致）
--
-- 执行前检查清单（批准后逐项确认）：
--   [ ] 1. 已获用户明确批准（含目标库：测试库 or qsm 本体）
--   [ ] 2. 目标库当前 0 张表（2026-10-12 实测）—— 若已有表，先核对是否本脚本产物
--   [ ] 3. 已备份（空库也要导出一次基线）
--   [ ] 4. 字符集 / 排序规则与实测一致（utf8mb4 / utf8mb4_general_ci）
--   [ ] 5. 时区 CST(UTC+8)（DATETIME(3) 存服务端本地时间，⛔ 不用 TIMESTAMP 避时区漂移）
--   [ ] 6. 保留期限策略（operation_log / 图片临时文件）已与管理员确认后另行设计
-- =============================================================================

SET NAMES utf8mb4;

-- -----------------------------------------------------------------------------
-- sys_user 人员目录（手机号是稳定自然键；入站消息直接可得）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `sys_user` (
  `id`                BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `phone`             VARCHAR(20)  NOT NULL COMMENT '手机号（唯一自然键）',
  `platform_user_id`  VARCHAR(64)  NULL COMMENT '量子密信侧 userId（可后续关联）',
  `name`              VARCHAR(64)  NULL COMMENT '显示名',
  `department`        VARCHAR(128) NULL COMMENT '部门',
  `source`            VARCHAR(16)  NOT NULL DEFAULT 'learned' COMMENT 'manual=后台录入 / learned=入站学习',
  `status`            TINYINT      NOT NULL DEFAULT 1 COMMENT '1 启用 / 0 停用',
  `created_time`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_time`      DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_sys_user_phone` (`phone`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='人员目录（MCP Server 内部存储，机器人不直连）';

-- -----------------------------------------------------------------------------
-- checkin_record 打卡记录
-- 唯一键 uq_checkin_scope：打卡额度由它保障（每日两时段各一次，§2.3）
-- status：NORMAL（按时）/ LATE（迟到）/ ADMIN_EDIT（后台补录，用户指令第二阶段）
-- entry_source：user / admin（补录追溯）
-- image_ref：需图模式为存储标识；免图打卡（群配置关闭需图）为 NULL
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `checkin_record` (
  `id`           BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`      BIGINT       NOT NULL COMMENT '-> sys_user.id',
  `group_id`     VARCHAR(64)  NOT NULL COMMENT '群标识',
  `period_key`   VARCHAR(32)  NOT NULL COMMENT '不透明周期串（机器人生成，自然日 YYYY-MM-DD）',
  `session`      VARCHAR(16)  NOT NULL COMMENT '打卡时段标识（am / pm / slot_N，§2.3）',
  `checkin_time` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) COMMENT '服务端时间 UTC+8',
  `image_ref`    VARCHAR(255) NULL COMMENT '存储标识（§4.11.4）；免图打卡为 NULL',
  `image_name`   VARCHAR(255) NULL COMMENT '原始文件名留档',
  `image_size`   BIGINT       NULL COMMENT '字节数留档',
  `status`       VARCHAR(16)  NOT NULL DEFAULT 'NORMAL' COMMENT 'NORMAL/LATE/ADMIN_EDIT',
  `entry_source` VARCHAR(16)  NOT NULL DEFAULT 'user' COMMENT 'user=群内提交 / admin=后台补录',
  `request_id`   VARCHAR(64)  NOT NULL COMMENT '本次请求键（§2.5.0）',
  `created_time` DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_checkin_scope` (`group_id`, `user_id`, `period_key`, `session`),
  KEY `ix_checkin_request` (`request_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='打卡记录（双时段各一次）';

-- -----------------------------------------------------------------------------
-- summary_record 日报 / 总结（覆盖语义：范围键唯一，同键不同 request -> UPDATE）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `summary_record` (
  `id`           BIGINT         NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`      BIGINT         NOT NULL COMMENT '-> sys_user.id',
  `group_id`     VARCHAR(64)    NOT NULL COMMENT '群标识',
  `period_key`   VARCHAR(32)    NOT NULL COMMENT '不透明周期串（自然日或 ISO 周 YYYY-Www）',
  `period_type`  VARCHAR(16)    NOT NULL COMMENT 'daily / weekly（冗余便于查询）',
  `content`      MEDIUMTEXT     NOT NULL COMMENT '正文',
  `request_id`   VARCHAR(64)    NOT NULL COMMENT '最后一次成功提交的请求键（覆盖会更新）',
  `entry_source` VARCHAR(16)    NOT NULL DEFAULT 'user' COMMENT 'user=群内提交 / admin=后台补录',
  `created_time` DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_time` DATETIME(3)    NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_summary_scope` (`group_id`, `user_id`, `period_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='总结记录（同周期覆盖）';

-- -----------------------------------------------------------------------------
-- report_record 报数记录（自然日一次；items 一次性写入，⛔ 不增量追加）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `report_record` (
  `id`           BIGINT      NOT NULL AUTO_INCREMENT COMMENT '主键',
  `user_id`      BIGINT      NOT NULL COMMENT '-> sys_user.id',
  `group_id`     VARCHAR(64) NOT NULL COMMENT '群标识',
  `period_key`   VARCHAR(32) NOT NULL COMMENT '不透明周期串（自然日 YYYY-MM-DD）',
  `role`         VARCHAR(32) NULL COMMENT '本次报数实际所用角色（开发/测试/产品，随记录留档）',
  `request_id`   VARCHAR(64) NOT NULL COMMENT '本次请求键',
  `entry_source` VARCHAR(16) NOT NULL DEFAULT 'user' COMMENT 'user=群内提交 / admin=后台补录',
  `created_time` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_time` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_report_scope` (`group_id`, `user_id`, `period_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='报数记录（自然日一次）';

-- -----------------------------------------------------------------------------
-- report_item 报数明细（field_id 为机器人侧字段池的不透明引用，§4.11.0 同 period_key 原则）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `report_item` (
  `id`           BIGINT        NOT NULL AUTO_INCREMENT COMMENT '主键',
  `record_id`    BIGINT        NOT NULL COMMENT '-> report_record.id',
  `field_id`     BIGINT        NOT NULL COMMENT '字段池定义 id（机器人侧群级配置，对 MCP 不透明）',
  `field_name`   VARCHAR(128)  NOT NULL COMMENT '提交时快照（字段改名不影响历史与归并）',
  `quantity`     DECIMAL(18,4) NOT NULL COMMENT '数量',
  `unit`         VARCHAR(32)   NULL COMMENT '提交时快照；单位在字段定义上维护',
  `created_time` DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `ix_report_item_record` (`record_id`),
  KEY `ix_report_item_field` (`field_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='报数明细（整批写入）';

-- -----------------------------------------------------------------------------
-- operation_log 操作日志（request_id 唯一索引 = 幂等的物理载体，§4.11.3）
-- input_json 必须脱敏（⛔ 不含令牌/凭据）；保留期限策略待定
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `operation_log` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT COMMENT '主键',
  `request_id`  VARCHAR(64)  NOT NULL COMMENT '请求幂等键（唯一索引）',
  `tool_name`   VARCHAR(64)  NOT NULL COMMENT '如 checkin.submit',
  `biz_type`    VARCHAR(16)  NOT NULL COMMENT 'checkin / summary / report / user / op',
  `group_id`    VARCHAR(64)  NULL COMMENT '便于按群查',
  `user_id`     BIGINT       NULL COMMENT '便于按人查',
  `period_key`  VARCHAR(32)  NULL COMMENT '从入参冗余，便于按周期查',
  `input_json`  MEDIUMTEXT   NULL COMMENT '完整入参（⛔ 脱敏）',
  `result_json` MEDIUMTEXT   NULL COMMENT '完整出参',
  `status`      VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending / success / rejected',
  `record_id`   BIGINT       NULL COMMENT '产生的业务记录 id',
  `error_code`  VARCHAR(64)  NULL,
  `error_msg`   VARCHAR(255) NULL,
  `created_time` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_time` DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_operation_log_request` (`request_id`),
  KEY `ix_op_log_query` (`group_id`, `created_time`),
  KEY `ix_op_log_tool` (`tool_name`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci
  COMMENT='操作日志（幂等载体 + 审计）';

-- -----------------------------------------------------------------------------
-- 执行后自检（批准并执行后运行，结果记入 §7.1）：
--   SHOW TABLES;                                   -- 期望 6 张
--   SHOW INDEX FROM checkin_record;                -- 确认 uq_checkin_scope 四列
--   SHOW INDEX FROM operation_log;                 -- 确认 uq_operation_log_request
--   SELECT @@version, @@time_zone, @@character_set_server;
-- -----------------------------------------------------------------------------
-- 【增量变更】2026-10-20：checkin_record.session VARCHAR(8) -> VARCHAR(16)
--   原因：后台新增自定义打卡时段使用 slot_N 形式标识，旧长度 8 会让
--   9 字符以上的时段 id 在写入时被 MySQL 严格模式拒绝（P0-1）。
--   本文件是「新建库」的全量 DDL（已按 VARCHAR(16) 定义）；
--   ⚠️ 已建表的环境需单独执行下面这条在线扩容（仅改元数据，不锁表数据、
--   不改任何既有值；⛔ 未经批准不得由程序自动执行）：
--
--   ALTER TABLE checkin_record MODIFY COLUMN session VARCHAR(16) NOT NULL COMMENT '打卡时段标识';
--
--   ✅ 执行记录：2026-10-20 11:05 已在线上库（qsm，219.151.177.33:28619）执行完毕，
--      `SHOW FULL COLUMNS FROM checkin_record LIKE 'session'` 实测
--      Type=varchar(16) / Comment=打卡时段标识，表内 3 行数据完好。
--      （新环境按上面注释定义建表即可，⛔ 不需要再执行本 ALTER。）
-- -----------------------------------------------------------------------------
