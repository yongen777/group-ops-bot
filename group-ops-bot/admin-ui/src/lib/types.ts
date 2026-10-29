export interface CheckinSlot {
  id: string;
  name: string;
  start: string;
  end: string;
  /** 提醒时间点（HH:MM）：必须满足 start < remind_at < end */
  remind_at?: string;
}

export interface ReminderRule {
  enabled: boolean;
  times: string[];
  text: string;
  /** ⭐ D49：执行日（ISO 1=周一 … 7=周日）。缺省/空 = 每天。 */
  days?: number[];
}

export interface GroupConfig {
  group_id: string;
  group_name: string;
  status: 'active' | 'disabled';
  switches: {
    checkin: boolean;
    summary: boolean;
    report: boolean;
    reminder: boolean;
    statistics: boolean;
  };
  period_type: 'daily' | 'weekly';
  daily_cutoff: string;
  checkin_deadlines: { slots: CheckinSlot[] };
  checkin_slots: CheckinSlot[];
  checkin_require_image: boolean;
  reminder_rules: {
    checkin: ReminderRule;
    summary: ReminderRule;
    report: ReminderRule;
  };
  /**
   * ⭐ D30 加固一：该群当前能否「主动推送」（定时提醒 / 导出文件）。
   * 后端口径 = 实例 ACTIVE 且 webhook_base_url 非空。
   * false 时「定时提醒 / 数据导出」开关置灰（后端 PUT 同口径拒写）。
   */
  push_ready?: boolean;
}

export interface GroupItem {
  group_id: string;
  group_name: string;
  status: 'active' | 'disabled';
  switches?: GroupConfig['switches'];
  period_type?: 'daily' | 'weekly';
  member_count?: number;
}

export interface GroupMember {
  user_id: string;
  nickname: string;
  role: string;
  enabled: boolean;
  source: string;
}

export interface AdminGrant {
  user_id: string;
  permission_type: string;
  nickname: string | null;
  updated_at: string;
}

export interface ReportField {
  field_id: number;
  field_name: string;
  unit: string;
  roles: string[];
  enabled: boolean;
  sort_order: number;
}

export interface RobotInstance {
  group_id: string;
  /** 后台备注名（管理员可改；空 = 未设置） */
  display_name: string;
  /** ⭐ 群配置里用户绑定时录入的群名称（后端 LEFT JOIN 带出；空 = 尚未绑定） */
  group_name?: string | null;
  webhook_base_url: string;
  webhook_key_set: boolean;
  status: 'WAIT_BIND' | 'ACTIVE' | 'DISABLED';
  /** ⭐ D30 加固一：能否主动推送（ACTIVE + 地址非空） */
  push_ready?: boolean;
  /** ⭐ D30 加固二：最近一次入站回调自带的回推地址（只读候选值，不是绑定） */
  last_callback_url?: string | null;
  last_callback_at?: string | null;
  created_at: string;
  updated_at: string;
}

/**
 * 未提交状态（2026-10-20 口径）：
 * - pending  未完成（周期未结束、也没催过；总结/报数当天即此态）
 * - waiting  未完成（打卡时段未到点，附 window 区间）
 * - reminded 未完成（已提醒：该人本期确实收到过提醒）
 * - overdue  逾期（待补录：周期/时段已结束仍无记录）
 */
export type CellStatus =
  | 'completed'
  | 'backfilled'
  | 'overdue'
  | 'pending'
  | 'waiting'
  | 'reminded'
  /** ⭐ D50：非执行日（该业务今天不执行，不算未完成） */
  | 'off';

export interface CheckinCell {
  time: string;
  status: CellStatus;
  method: string;
  raw_status: string;
  /** 凭证图片引用（后台鉴权接口 /api/admin/uploads/<ref>；空 = 无图） */
  image_ref?: string;
  image_name?: string;
  /** 该时段区间文案（如 08:00-12:00），「未完成（时段）」展示用 */
  window?: string;
  /** 该时段是否已推送过提醒 */
  reminded?: boolean;
}

export interface SummaryCell {
  content: string;
  words: number;
  at: string;
  entry_source: string;
  period_type: string;
}

export interface ReportItem {
  field_id: number;
  field_name: string;
  unit: string;
  quantity: number;
}

export interface ReportCell {
  items: ReportItem[];
  total: number;
  at: string;
  entry_source: string;
}

export interface BoardMember {
  user_id: string;
  nickname: string;
  role: string;
  enabled: boolean;
  in_roster: boolean;
  checkin: Record<string, CheckinCell>;
  checkin_status: CellStatus;
  summary: SummaryCell | null;
  summary_status: CellStatus;
  report: ReportCell | null;
  report_status: CellStatus;
}

export interface Board {
  ok: boolean;
  date: string;
  period_keys: { checkin: string; summary: string; report: string };
  period_type: 'daily' | 'weekly';
  group: {
    group_id: string;
    group_name: string;
    status: string;
    switches: GroupConfig['switches'];
  };
  slots: CheckinSlot[];
  /** ⭐ D50：三业务当天是否执行（false = 休息日，不计未完成） */
  scheduled?: { checkin: boolean; summary: boolean; report: boolean };
  members: BoardMember[];
  counts: {
    members: number;
    total: number;
    checkin_done: number;
    summary_done: number;
    report_done: number;
  };
  unknown: { checkin: boolean; summary: boolean; report: boolean };
}

export type BizType = 'checkin' | 'summary' | 'report';
