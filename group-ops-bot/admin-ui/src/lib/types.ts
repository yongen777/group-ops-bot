export interface CheckinSlot {
  id: string;
  name: string;
  start: string;
  end: string;
}

export interface ReminderRule {
  enabled: boolean;
  times: string[];
  text: string;
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
  display_name: string;
  webhook_base_url: string;
  webhook_key_set: boolean;
  status: 'WAIT_BIND' | 'ACTIVE' | 'DISABLED';
  created_at: string;
  updated_at: string;
}

export type CellStatus = 'completed' | 'backfilled' | 'overdue' | 'pending';

export interface CheckinCell {
  time: string;
  status: CellStatus;
  method: string;
  raw_status: string;
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
