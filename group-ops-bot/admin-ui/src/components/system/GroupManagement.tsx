import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Users,
  Settings,
  ShieldCheck,
  BarChart3,
  Plus,
  Trash2,
  Save,
  CheckCircle2,
  AlertTriangle,
  Clock,
  Search,
  X,
  BellRing,
  UserPlus,
} from 'lucide-react';
import { api, gid } from '../../lib/api';
import { useApp } from '../../lib/store';
import type {
  AdminGrant,
  GroupConfig,
  GroupMember,
  ReportField,
} from '../../lib/types';
import { GroupSwitcher } from './GroupSwitcher';
import { ROLE_LABEL, ROLE_OPTIONS } from '../../lib/roles';

const BIZ_SWITCHES = [
  { key: 'checkin' as const, title: '考勤打卡', desc: '上下班考勤打卡与凭据记录' },
  { key: 'summary' as const, title: '今日总结', desc: '每日工作总结与要点汇报' },
  { key: 'report' as const, title: '今日报数', desc: '量化指标填报与数据采集' },
  // ⭐ D30 加固一：下面两项依赖「机器人主动推送」，需要群级 webhook 地址
  { key: 'reminder' as const, title: '定时提醒', desc: '到点群内催办未完成人员', pushDependent: true },
  { key: 'statistics' as const, title: '数据导出', desc: '管理员下载报表清单', pushDependent: true },
];

/** HH:MM -> 当天分钟数（非法返回 null） */
const toMin = (value?: string): number | null => {
  if (!value || !/^\d{2}:\d{2}$/.test(value)) return null;
  return Number(value.slice(0, 2)) * 60 + Number(value.slice(3, 5));
};

/**
 * ⭐ D32（2026-10-21，巡检 P1-1）：群成员唯一标识 = 平台手机号。
 *
 * 巡检实测「12345」能被直接加进名单 → 产生无法 @、无法实名的假成员。
 * 正则与后端 `_PHONE_RE` / 实名登记 / 群内绑定**同一条**：11 位大陆手机号。
 */
const PHONE_RE = /^1[3-9]\d{9}$/;
const PHONE_INVALID_TEXT = '手机号格式不正确';

/** ⭐ D36：手机号撞号待办（单人单号口径下的数据异常） */
interface PhoneConflict {
  id: number;
  phone: string;
  kind: 'rename' | 'realname' | string;
  detail: string;
  created_at: string;
}

/**
 * 提醒时间点边界校验（与后端 checkin.remind_at_error 同一口径）：
 * 必须满足 start < remind_at < end —— 08:00-08:30 的时段只能填 08:01~08:29。
 * 返回 null = 合法；留空（未配置）时后端按「结束前 5 分钟」补默认值。
 */
const remindAtProblem = (start: string, end: string, remindAt?: string): string | null => {
  if (!remindAt) return null;
  const s = toMin(start);
  const e = toMin(end);
  const r = toMin(remindAt);
  if (r === null) return '提醒时间格式不正确';
  if (s === null || e === null) return '时间段本身不合法';
  if (r <= s) return `提醒时间必须晚于开始时间（需 > ${start}）`;
  if (r >= e) return `提醒时间必须早于结束时间（需 < ${end}）`;
  return null;
};

export const GroupManagement: React.FC = () => {
  const { groups, selectedGroupId, selectGroup, refreshGroups } = useApp();
  const [subView, setSubView] = useState<'config' | 'members' | 'admins' | 'fields'>('config');
  const [toast, setToast] = useState<{ text: string; type: 'success' | 'error' } | null>(null);
  const [loading, setLoading] = useState(false);

  const [config, setConfig] = useState<GroupConfig | null>(null);
  const [members, setMembers] = useState<GroupMember[]>([]);
  const [grants, setGrants] = useState<AdminGrant[]>([]);
  const [fields, setFields] = useState<ReportField[]>([]);
  const [phoneConflicts, setPhoneConflicts] = useState<PhoneConflict[]>([]);

  const showToast = useCallback((text: string, type: 'success' | 'error' = 'success') => {
    setToast({ text, type });
    setTimeout(() => setToast(null), 3200);
  }, []);

  const loadConfig = useCallback(async () => {
    if (!selectedGroupId) return;
    const data = await api.get<{ ok: boolean; config: GroupConfig }>(
      `/api/admin/groups/${gid(selectedGroupId)}/config`,
    );
    setConfig(data.config);
  }, [selectedGroupId]);

  const loadMembers = useCallback(async () => {
    if (!selectedGroupId) return;
    const data = await api.get<{ ok: boolean; members: GroupMember[] }>(
      `/api/admin/groups/${gid(selectedGroupId)}/members`,
    );
    setMembers(data.members || []);
  }, [selectedGroupId]);

  /**
   * ⭐ D36（2026-10-22 用户指令）：手机号撞号待办。
   *
   * 单人单号口径下，撞号 = 数据出问题（改号撞到别群的人 / 实名填了别人的号）。
   * 后端只在操作瞬间弹一次 toast，关掉就没了，所以这里做成**持久提示条**，
   * 管理员核对完点「已处理」才消失。
   */
  const loadPhoneConflicts = useCallback(async () => {
    if (!selectedGroupId) return;
    try {
      const data = await api.get<{ conflicts: PhoneConflict[] }>(
        `/api/admin/groups/${gid(selectedGroupId)}/member-conflicts`,
      );
      setPhoneConflicts(data.conflicts || []);
    } catch {
      setPhoneConflicts([]);          // 取不到⛔ 不影响主流程
    }
  }, [selectedGroupId]);

  const loadGrants = useCallback(async () => {
    if (!selectedGroupId) return;
    const data = await api.get<{ ok: boolean; whitelist: AdminGrant[] }>(
      `/api/admin/groups/${gid(selectedGroupId)}/whitelist`,
    );
    setGrants(data.whitelist || []);
  }, [selectedGroupId]);

  const loadFields = useCallback(async () => {
    if (!selectedGroupId) return;
    const data = await api.get<{ ok: boolean; fields: ReportField[] }>(
      `/api/admin/groups/${gid(selectedGroupId)}/fields`,
    );
    setFields(data.fields || []);
  }, [selectedGroupId]);

  const loadAll = useCallback(async () => {
    if (!selectedGroupId) return;
    setLoading(true);
    try {
      await Promise.all([loadConfig(), loadMembers(), loadGrants(),
                         loadFields(), loadPhoneConflicts()]);
    } catch (err: any) {
      showToast(err?.message || '数据加载失败，请刷新页面重试', 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedGroupId, loadConfig, loadMembers, loadGrants, loadFields,
      loadPhoneConflicts, showToast]);

  /**
   * ⭐ D32（巡检 P1-3）：切到「授权群管理员」时**重新拉一次**成员与授权。
   *
   * 候选卡片 = 在岗成员 − 已授权，全部由这两份数据派生。原来只在进页面时
   * `loadAll` 拉一次：若成员是在**别处**被删的（另一个标签页/另一位管理员，
   * 或经接口直删），切过来仍会列出已删成员、点授权才被后端 400 拦住。
   * 进 Tab 即刷新，保证「看到的候选就是现在能授权的」。
   */
  const openAdminsTab = useCallback(() => {
    setSubView('admins');
    void loadMembers();
    void loadGrants();
  }, [loadMembers, loadGrants]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // ⭐ D49（2026-10-29 用户指令）：每个业务可单独配置执行日（周几）。
  // ISO 编号 1=周一 … 7=周日；默认全周 = 改动前行为。
  const ALL_DAYS = [1, 2, 3, 4, 5, 6, 7];
  const DAY_LABELS: Record<number, string> = {
    1: '一', 2: '二', 3: '三', 4: '四', 5: '五', 6: '六', 7: '日',
  };

  function normalizeDaysInput(raw?: number[]): number[] {
    const clean = (raw || []).filter((d) => d >= 1 && d <= 7);
    return clean.length
      ? Array.from(new Set(clean)).sort((a, b) => a - b)
      : [...ALL_DAYS];
  }

  function daysHint(days: number[]): string {
    if (days.length === 7) return '每天';
    if (days.length === 5 && !days.includes(6) && !days.includes(7)) return '工作日';
    if (days.length === 2 && days.includes(6) && days.includes(7)) return '仅周末';
    return days.map((d) => '周' + DAY_LABELS[d]).join('、');
  }

  function DayPicker({
    value,
    onChange,
  }: {
    value: number[];
    onChange: (v: number[]) => void;
  }) {
    const toggle = (day: number) => {
      const next = value.includes(day)
        ? value.filter((d) => d !== day)
        : [...value, day].sort((a, b) => a - b);
      if (!next.length) return;   // ⛔ 至少保留一天
      onChange(next);
    };
    return (
      <div className="flex items-center gap-1 flex-wrap">
        {ALL_DAYS.map((day) => (
          <button
            key={day}
            type="button"
            onClick={() => toggle(day)}
            title={`周${DAY_LABELS[day]}`}
            className={`w-7 h-7 rounded-lg text-[11px] font-bold border transition ${
              value.includes(day)
                ? 'bg-blue-600 text-white border-blue-600'
                : 'bg-white text-slate-400 border-slate-200 hover:border-blue-300'
            }`}
          >
            {DAY_LABELS[day]}
          </button>
        ))}
        <span className="text-[10px] text-slate-500 ml-1">{daysHint(value)}</span>
      </div>
    );
  }

  // ===================== 群配置 =====================
  const [groupNameInput, setGroupNameInput] = useState('');
  const [nameDirty, setNameDirty] = useState(false);
  const [slots, setSlots] = useState<GroupConfig['checkin_slots']>([]);
  const [reminder, setReminder] = useState({
    checkin: true,
    summary: true,
    report: true,
    summaryTime: '18:00',
    reportTime: '18:30',
    checkinDays: [1, 2, 3, 4, 5, 6, 7],
    summaryDays: [1, 2, 3, 4, 5, 6, 7],
    reportDays: [1, 2, 3, 4, 5, 6, 7],
  });

  useEffect(() => {
    if (!config) return;
    setGroupNameInput(config.group_name || '');
    setNameDirty(false);
    setSlots(config.checkin_slots || []);
    setReminder({
      checkin: config.reminder_rules?.checkin?.enabled ?? true,
      summary: config.reminder_rules?.summary?.enabled ?? true,
      report: config.reminder_rules?.report?.enabled ?? true,
      summaryTime: config.reminder_rules?.summary?.times?.[0] || '18:00',
      reportTime: config.reminder_rules?.report?.times?.[0] || '18:30',
      checkinDays: normalizeDaysInput(config.reminder_rules?.checkin?.days),
      summaryDays: normalizeDaysInput(config.reminder_rules?.summary?.days),
      reportDays: normalizeDaysInput(config.reminder_rules?.report?.days),
    });
  }, [config]);

  const saveConfig = async (patch: Record<string, unknown>, message: string) => {
    try {
      const data = await api.put<{ ok: boolean; config: GroupConfig }>(
        `/api/admin/groups/${gid(selectedGroupId)}/config`,
        patch,
      );
      setConfig(data.config);
      showToast(message);
      return true;
    } catch (err: any) {
      showToast(err?.message || '保存未成功，请稍后重试', 'error');
      return false;
    }
  };

  const toggleSwitch = async (key: keyof GroupConfig['switches']) => {
    if (!config) return;
    const next = !config.switches[key];
    const label = BIZ_SWITCHES.find((item) => item.key === key)?.title || key;
    // ⭐ D30 加固一：没钱没地址就别开灯 —— 未绑定推送地址时不许打开
    // 「定时提醒 / 数据导出」（后端 PUT 同口径 400，这层只是即时提示）
    const pushDependent = BIZ_SWITCHES.some((item) => item.key === key && item.pushDependent);
    if (next && pushDependent && config.push_ready === false) {
      showToast(`【${label}】依赖机器人主动推送，请先到「群实例绑定」填写推送地址并启用`, 'error');
      return;
    }
    await saveConfig({ switches: { [key]: next } }, `已${next ? '开启' : '关闭'}【${label}】功能`);
  };

  const saveGroupName = async () => {
    const trimmed = groupNameInput.trim();
    if (!trimmed) {
      showToast('群名称不能为空', 'error');
      return;
    }
    try {
      await api.put(`/api/admin/groups/${gid(selectedGroupId)}`, { group_name: trimmed });
      setNameDirty(false);
      await refreshGroups();
      await loadConfig();
      showToast(`群名称已更新为「${trimmed}」`);
    } catch (err: any) {
      showToast(err?.message || '保存未成功，请稍后重试', 'error');
    }
  };

  const saveSlots = async () => {
    const clean = slots.filter((slot) => slot.name.trim());
    if (clean.length === 0) {
      showToast('至少保留一个打卡时段', 'error');
      return;
    }
    // ⭐ 2026-10-20：提醒时间点必须严格落在时段内（开区间），前端先挡一道
    for (const slot of clean) {
      const problem = remindAtProblem(slot.start, slot.end, slot.remind_at);
      if (problem) {
        showToast(`「${slot.name}」${problem}`, 'error');
        return;
      }
    }
    const ordered = [...clean].sort((a, b) => a.start.localeCompare(b.start));
    const ok = await saveConfig({ checkin_deadlines: { slots: ordered } },
      '打卡时段已保存并即时生效');
    if (ok) {
      await refreshGroups();
    }
  };

  const saveReminder = async () => {
    if (reminder.summary && !reminder.summaryTime) {
      showToast('今日总结以整天为执行时间，必须设置每日提醒下发时间', 'error');
      return;
    }
    if (reminder.report && !reminder.reportTime) {
      showToast('今日报数以整天为执行时间，必须设置每日提醒下发时间', 'error');
      return;
    }
    // ⭐ 2026-10-20：打卡提醒时刻 = 各时段配置的提醒时间点（remind_at）；
    // times 仅为兼容保留字段，实际触发由 remind_at 决定（⛔ 不再用 start）
    // ⭐ D31 修复 3（2026-10-21）：后端 `_validate_reminder_rules` 的 times
    // 上限是 **5**（`test_reminder_per_biz.py` 锁死传 6 个必须 400），而
    // 打卡时段允许 1~8 个 —— 原来写 `.slice(0, 6)`，配了 6~8 个时段的群
    // 保存提醒规则必报 400。这里对齐后端上限。
    const checkinTimes = (slots.length ? slots : config?.checkin_slots || [])
      .map((slot) => slot.remind_at || slot.start)
      .slice(0, 5);
    const patch = {
      reminder_rules: {
        checkin: {
          enabled: reminder.checkin,
          times: checkinTimes.length ? checkinTimes : ['09:00'],
          days: reminder.checkinDays,
        },
        summary: {
          enabled: reminder.summary,
          times: [reminder.summaryTime],
          days: reminder.summaryDays,
        },
        report: {
          enabled: reminder.report,
          times: [reminder.reportTime],
          days: reminder.reportDays,
        },
      },
    };
    await saveConfig(patch, '结构化定时提醒调度规则已保存生效');
  };

  // ===================== 群成员 =====================
  const [memberQuery, setMemberQuery] = useState('');
  const [roleFilter, setRoleFilter] = useState('all');
  const [newName, setNewName] = useState('');
  const [newUserId, setNewUserId] = useState('');
  const [newRole, setNewRole] = useState('开发');
  const [memberError, setMemberError] = useState<string | null>(null);
  const [editingMember, setEditingMember] = useState<GroupMember | null>(null);
  const [editPhone, setEditPhone] = useState('');
  const [deletingMember, setDeletingMember] = useState<GroupMember | null>(null);

  const filteredMembers = useMemo(() => {
    const query = memberQuery.trim();
    return members.filter((m) => {
      const hit = !query
        || (m.nickname || '').includes(query)
        || m.user_id.includes(query)
        || (m.role || '').includes(query);
      const roleHit = roleFilter === 'all' || m.role === roleFilter;
      return hit && roleHit;
    });
  }, [members, memberQuery, roleFilter]);

  const grantPhones = useMemo(() => new Set(grants.map((g) => g.user_id)), [grants]);

  const addMember = async (event: React.FormEvent) => {
    event.preventDefault();
    setMemberError(null);
    const name = newName.trim();
    const userId = newUserId.trim();
    if (!name) {
      setMemberError('真实姓名是必填项，请输入成员真实姓名');
      return;
    }
    if (!PHONE_RE.test(userId)) {
      // ⭐ D32：原校验只要求 4~32 位数字/字母，把「12345」也放过
      setMemberError(PHONE_INVALID_TEXT);
      return;
    }
    if (members.some((m) => m.user_id === userId)) {
      setMemberError(`【${userId}】已存在于群成员名单中，请勿重复添加`);
      return;
    }
    try {
      await api.post(`/api/admin/groups/${gid(selectedGroupId)}/members`, {
        user_id: userId,
        nickname: name,
        role: newRole,
        enabled: true,
      });
      setNewName('');
      setNewUserId('');
      await loadMembers();
      await refreshGroups();
      showToast(`已成功将成员【${name}】(${userId}) 添加至群成员名单`);
    } catch (err: any) {
      setMemberError(err?.message || '添加未成功，请稍后重试');
    }
  };

  const toggleMember = async (member: GroupMember) => {
    try {
      await api.put(`/api/admin/groups/${gid(selectedGroupId)}/members/${gid(member.user_id)}`, {
        enabled: !member.enabled,
      });
      await loadMembers();
      await refreshGroups();
      showToast(`已${member.enabled ? '暂停' : '恢复'}成员【${member.nickname || member.user_id}】的业务考核`);
    } catch (err: any) {
      showToast(err?.message || '操作未成功', 'error');
    }
  };

  const saveMemberEdit = async () => {
    if (!editingMember) return;
    if (!editingMember.nickname.trim()) {
      showToast('真实姓名不能为空', 'error');
      return;
    }
    const nextPhone = editPhone.trim();
    // ⭐ D32：只在「号码变了」时校验 —— 历史遗留的非手机号成员改姓名时
    // ⛔ 不该被强制顺手改号（改号有独立的业务代价：历史记录仍挂旧号）
    if (nextPhone !== editingMember.user_id && !PHONE_RE.test(nextPhone)) {
      showToast(PHONE_INVALID_TEXT, 'error');
      return;
    }
    try {
      // ⚠️ 改手机号只改名单配置（+跟着人走的导出授权），
      //    历史业务记录仍挂在旧号码上，不会跟着改
      const result = await api.put<{
        ok: boolean; user_id: string;
        renamed_groups?: string[]; conflict_groups?: string[];
      }>(
        `/api/admin/groups/${gid(selectedGroupId)}/members/${gid(editingMember.user_id)}`,
        {
          nickname: editingMember.nickname.trim(),
          role: editingMember.role,
          new_user_id: nextPhone,
        },
      );
      setEditingMember(null);
      await loadMembers();
      await loadGrants();
      await loadPhoneConflicts();
      const renamed = result.user_id !== editingMember.user_id;
      const conflictCount = result.conflict_groups?.length || 0;
      if (renamed && conflictCount > 0) {
        // 某些群已存在同一新号（同号两人）-> 那些群⛔ 不合并，交人工处理
        showToast(
          `手机号已改为 ${result.user_id}，但 ${conflictCount} 个群已存在该号码，未自动合并，请人工核对`,
          'error',
        );
      } else {
        showToast(renamed
          ? `成员信息已更新（手机号改为 ${result.user_id}，已同步 ${result.renamed_groups?.length || 0} 个群）；历史业务记录仍按旧号码留存`
          : '成员信息已成功更新');
      }
    } catch (err: any) {
      showToast(err?.message || '保存未成功', 'error');
    }
  };

  /** ⭐ D36：标记撞号待办已处理（人工核对完点） */
  const resolvePhoneConflict = async (id: number) => {
    try {
      await api.post(
        `/api/admin/groups/${gid(selectedGroupId)}/member-conflicts/${id}/resolve`, {},
      );
      await loadPhoneConflicts();
      showToast('已标记为处理完毕');
    } catch (err: any) {
      showToast(err?.message || '操作未成功', 'error');
    }
  };

  const removeMember = async () => {
    if (!deletingMember) return;
    try {
      await api.del(
        `/api/admin/groups/${gid(selectedGroupId)}/members/${gid(deletingMember.user_id)}`,
      );
      showToast(`已将【${deletingMember.nickname || deletingMember.user_id}】从本群名单中移除`);
      setDeletingMember(null);
      await loadMembers();
      await loadGrants();
      await loadPhoneConflicts();
      await refreshGroups();
    } catch (err: any) {
      showToast(err?.message || '移除未成功', 'error');
    }
  };

  // ===================== 授权管理员 =====================
  const [adminQuery, setAdminQuery] = useState('');
  const [grantQuery, setGrantQuery] = useState('');
  const [grantTarget, setGrantTarget] = useState('');
  const [revokeTarget, setRevokeTarget] = useState<AdminGrant | null>(null);

  const eligibleMembers = members.filter((m) => m.enabled && !grantPhones.has(m.user_id));

  /**
   * 授权候选过滤（2026-10-20 修复「搜索框输入没反应」）。
   *
   * 原实现是拿关键字**过滤一个原生 <select> 的 option**：0 命中时下拉只剩
   * 一行占位项，输入框看起来完全没反应；而选中项被过滤掉时 select 还留着
   * 旧值（看不见却已选中，点「授权」会授权给一个不在列表里的人）。
   * 现在改成显式结果列表 + 三态空态文案（见下方 UI），并：
   * - 姓名 / 手机号 / 岗位 都参与匹配，**忽略大小写与空格**（复制来的手机号常带空格）；
   * - 选中项一旦不在结果里就立刻清空（下面的 useEffect）。
   */
  const grantKeyword = grantQuery.trim();
  const grantDigits = grantKeyword.replace(/\s+/g, '');
  const grantOptions = (() => {
    if (!grantKeyword) return eligibleMembers;
    const q = grantKeyword.toLowerCase();
    return eligibleMembers.filter((m) => (
      (m.nickname || '').toLowerCase().includes(q)
      || (grantDigits.length > 0 && m.user_id.includes(grantDigits))
      || (m.role || '').toLowerCase().includes(q)
      || (ROLE_LABEL[m.role] || '').toLowerCase().includes(q)
    ));
  })();
  const grantOptionIds = grantOptions.map((m) => m.user_id).join(',');
  const grantSelected = members.find((m) => m.user_id === grantTarget) || null;

  // ⭐ 选中项被过滤掉（或刚授权成功被移出候选）-> 立刻清空，⛔ 不留「隐形选中」
  useEffect(() => {
    if (!grantTarget) return;
    if (!grantOptionIds.split(',').includes(grantTarget)) setGrantTarget('');
  }, [grantTarget, grantOptionIds]);

  // ⭐ 切群 / 切子页 -> 清搜索词与选择：否则上一个群的搜索词会把新群列表
  //    过滤成空，看起来就是「搜索框坏了 / 列表没数据」
  useEffect(() => {
    setGrantQuery('');
    setGrantTarget('');
    setAdminQuery('');
  }, [selectedGroupId, subView]);

  const grantAdmin = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!grantTarget) {
      showToast('请选择需要授权为管理员的群成员', 'error');
      return;
    }
    try {
      await api.post(`/api/admin/groups/${gid(selectedGroupId)}/whitelist`, {
        user_id: grantTarget,
      });
      const member = members.find((m) => m.user_id === grantTarget);
      setGrantTarget('');
      await loadGrants();
      showToast(`已成功授予【${member?.nickname || grantTarget}】群管理员身份`);
    } catch (err: any) {
      showToast(err?.message || '授权未成功', 'error');
    }
  };

  const revokeAdmin = async () => {
    if (!revokeTarget) return;
    try {
      await api.del(`/api/admin/groups/${gid(selectedGroupId)}/whitelist`, {
        user_id: revokeTarget.user_id,
      });
      showToast(`已解除【${revokeTarget.nickname || revokeTarget.user_id}】的群管理员权限`);
      setRevokeTarget(null);
      await loadGrants();
    } catch (err: any) {
      showToast(err?.message || '撤销未成功', 'error');
    }
  };

  const filteredGrants = grants.filter((g) => {
    const query = adminQuery.trim();
    if (!query) return true;
    return (g.nickname || '').includes(query) || g.user_id.includes(query);
  });

  // ===================== 报数字段池 =====================
  const [fieldName, setFieldName] = useState('');
  const [fieldUnit, setFieldUnit] = useState('个');
  const [deletingField, setDeletingField] = useState<ReportField | null>(null);

  const addField = async (event: React.FormEvent) => {
    event.preventDefault();
    const name = fieldName.trim();
    if (!name) {
      showToast('请输入字段名称', 'error');
      return;
    }
    if (fields.some((f) => f.field_name.toLowerCase() === name.toLowerCase())) {
      showToast(`字段【${name}】已存在`, 'error');
      return;
    }
    try {
      await api.post(`/api/admin/groups/${gid(selectedGroupId)}/fields`, {
        field_name: name,
        unit: fieldUnit.trim() || '个',
      });
      setFieldName('');
      setFieldUnit('个');
      await loadFields();
      showToast(`已成功添加报数字段「${name}」，全员可填报`);
    } catch (err: any) {
      showToast(err?.message || '新增未成功', 'error');
    }
  };

  const toggleField = async (field: ReportField) => {
    try {
      await api.put(
        `/api/admin/groups/${gid(selectedGroupId)}/fields/${field.field_id}`,
        { enabled: !field.enabled },
      );
      await loadFields();
      showToast(`已${field.enabled ? '停用' : '启用'}报数字段【${field.field_name}】`);
    } catch (err: any) {
      showToast(err?.message || '操作未成功', 'error');
    }
  };

  const deleteField = async () => {
    if (!deletingField) return;
    try {
      await api.del(`/api/admin/groups/${gid(selectedGroupId)}/fields/${deletingField.field_id}`);
      showToast(`字段【${deletingField.field_name}】已删除（历史报数记录保留）`);
      setDeletingField(null);
      await loadFields();
    } catch (err: any) {
      showToast(err?.message || '操作未成功', 'error');
    }
  };

  return (
    <div className="space-y-5">
      <GroupSwitcher
        icon={<Users className="w-5 h-5" />}
        groups={groups}
        activeId={selectedGroupId}
        subtitle="当前群聊的基础开关、群成员架构、管理员权限与报数字段池配置"
        onSelect={(group) => selectGroup(group.group_id)}
        toast={showToast}
      />

      <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-2">
        <div className="flex items-center gap-2 overflow-x-auto p-1 bg-slate-100/80 rounded-xl text-xs font-semibold">
          <button
            onClick={() => setSubView('config')}
            className={`px-3.5 py-2 rounded-lg transition-all cursor-pointer flex items-center gap-1.5 whitespace-nowrap ${
              subView === 'config' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <Settings className="w-3.5 h-3.5" />
            <span>群配置与开关</span>
          </button>
          <button
            onClick={() => setSubView('members')}
            className={`px-3.5 py-2 rounded-lg transition-all cursor-pointer flex items-center gap-1.5 whitespace-nowrap ${
              subView === 'members' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <Users className="w-3.5 h-3.5" />
            <span>群成员 ({members.length})</span>
          </button>
          <button
            onClick={openAdminsTab}
            className={`px-3.5 py-2 rounded-lg transition-all cursor-pointer flex items-center gap-1.5 whitespace-nowrap ${
              subView === 'admins' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>授权群管理员 ({grants.length})</span>
          </button>
          <button
            onClick={() => setSubView('fields')}
            className={`px-3.5 py-2 rounded-lg transition-all cursor-pointer flex items-center gap-1.5 whitespace-nowrap ${
              subView === 'fields' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
            }`}
          >
            <BarChart3 className="w-3.5 h-3.5" />
            <span>报数字段池 ({fields.length})</span>
          </button>
        </div>
      </div>

      {toast && (
        <div
          className={`p-3 rounded-xl border text-xs flex items-center gap-2 animate-in fade-in slide-in-from-top-1 ${
            toast.type === 'error'
              ? 'bg-rose-50 border-rose-200 text-rose-800'
              : 'bg-emerald-50 border-emerald-200 text-emerald-800'
          }`}
        >
          {toast.type === 'error' ? (
            <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0" />
          ) : (
            <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
          )}
          <span className="font-medium">{toast.text}</span>
        </div>
      )}

      {loading && !config && (
        <div className="bg-white rounded-2xl border border-slate-200/90 p-16 text-center text-slate-400 text-xs">
          正在加载群配置…
        </div>
      )}

      {!selectedGroupId && !loading && (
        <div className="bg-white rounded-2xl border border-slate-200/90 p-16 text-center text-slate-400 text-xs">
          还没有群聊。请在「群实例绑定」中确认绑定，机器人进群后会自动出现。
        </div>
      )}

      {config && subView === 'config' && (
        <div className="space-y-5">
          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-3 mb-4">
              <div>
                <h3 className="font-bold text-sm text-slate-900 flex items-center gap-2">
                  <span>群名称与基本配置</span>
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  支持修改当前群聊在系统后台与机器人交互时的显示名称
                </p>
              </div>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
              <div className="md:col-span-2">
                <label className="font-semibold text-slate-700 block mb-1.5">
                  群名称 (支持即时修改并同步)
                </label>
                <div className="flex items-center gap-2">
                  <input
                    type="text"
                    value={groupNameInput}
                    onChange={(e) => {
                      setGroupNameInput(e.target.value);
                      setNameDirty(true);
                    }}
                    placeholder="输入群名称..."
                    className="flex-1 bg-slate-50 border border-slate-200 rounded-xl px-3.5 py-2 text-xs font-medium text-slate-900 outline-none focus:border-blue-500 focus:bg-white"
                  />
                  {nameDirty && (
                    <button
                      onClick={saveGroupName}
                      className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl font-semibold cursor-pointer shadow-xs transition-colors shrink-0 flex items-center gap-1"
                    >
                      <Save className="w-3.5 h-3.5" />
                      <span>保存名称</span>
                    </button>
                  )}
                </div>
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1.5">群ID (通信系统分配)</label>
                <div className="bg-slate-100 border border-slate-200/80 rounded-xl px-3.5 py-2 text-xs font-mono text-slate-600 select-all">
                  {config.group_id}
                </div>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-4">
            <div>
              <h3 className="font-bold text-sm text-slate-900">核心业务开关</h3>
              <p className="text-xs text-slate-500 mt-0.5">
                可针对该群单独开启或关闭考勤、总结、报数与导出功能，关闭后机器人不监听对应指令
              </p>
              {config.push_ready === false && (
                <p className="text-[11px] text-amber-700 mt-1.5 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 leading-relaxed">
                  ⚠ 该群尚未绑定机器人推送地址：「定时提醒」「数据导出」暂不可开启。
                  请到左侧「群实例绑定」→ 该群「确认绑定」填写推送地址并启用。
                </p>
              )}
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3 text-xs">
              {BIZ_SWITCHES.map((item) => {
                const enabled = config.switches[item.key];
                // ⭐ D30 加固一：未绑定推送地址时，「定时提醒 / 数据导出」
                // 关着的卡置灰（点了给提示），已经开着的卡打黄色告警（可关掉）
                const pushBlocked = !!item.pushDependent && config.push_ready === false;
                const lockedOff = pushBlocked && !enabled;
                return (
                  <div
                    key={item.key}
                    onClick={() => toggleSwitch(item.key)}
                    className={`p-3.5 rounded-xl border transition-all flex flex-col justify-between gap-2.5 ${
                      lockedOff
                        ? 'border-slate-200 bg-slate-100/70 opacity-60 cursor-not-allowed'
                        : enabled
                          ? 'border-blue-300 bg-blue-50/30 shadow-2xs cursor-pointer'
                          : 'border-slate-200 bg-slate-50/40 opacity-75 cursor-pointer'
                    }`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-bold text-slate-900 text-xs">{item.title}</span>
                      <div
                        className={`w-9 h-5 rounded-full transition-colors flex items-center p-0.5 ${
                          enabled ? 'bg-blue-600 justify-end' : 'bg-slate-300 justify-start'
                        }`}
                      >
                        <div className="w-4 h-4 rounded-full bg-white shadow-xs"></div>
                      </div>
                    </div>
                    <p className="text-[11px] text-slate-500 line-clamp-2 leading-relaxed">
                      {item.desc}
                    </p>
                    <div className="pt-2 border-t border-slate-100 flex items-center justify-between text-[10px]">
                      <span className={enabled ? 'text-blue-700 font-semibold' : 'text-slate-400'}>
                        {enabled ? '● 功能已开启' : '○ 功能已停用'}
                      </span>
                    </div>
                    {pushBlocked && (
                      <p className="text-[10px] leading-relaxed text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2 py-1 -mt-1">
                        {lockedOff
                          ? '⚠ 未绑定推送地址，暂不可开启'
                          : '⚠ 推送地址未绑定，当前不会真的发出去'}
                      </p>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
            <div className="lg:col-span-2 bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-4">
              <div className="flex items-center justify-between border-b border-slate-100 pb-3">
                <div>
                  <h3 className="font-bold text-sm text-slate-900">考勤打卡时段设置</h3>
                  <p className="text-xs text-slate-500 mt-0.5">
                    支持按上午、下午等多区间配置打卡有效时间段
                  </p>
                </div>
                <button
                  onClick={() => {
                    if (slots.length >= 6) {
                      showToast('打卡时间段最多支持设置 6 个区间', 'error');
                      return;
                    }
                    setSlots((prev) => {
                      // ⭐ 2026-10-20：时段 id 必须 <= 8 字符 —— checkin_record.session
                      // 是 VARCHAR(16)，而旧实现的「s + 时间戳 base36」是 9 字符且
                      // 与后端 _SLOT_ID_RE 的历史口径不一致；改用稳定短 id
                      // （slot_1 ~ slot_8，后端 MAX_SLOTS = 8），并避开已占用的编号。
                      const used = new Set(prev.map((s) => s.id));
                      let idx = prev.length + 1;
                      while (used.has(`slot_${idx}`)) idx += 1;
                      return [
                        ...prev,
                        {
                          id: `slot_${idx}`,
                          name: `时段${prev.length + 1}`,
                          start: '18:00',
                          end: '19:00',
                          remind_at: '18:55',
                        },
                      ];
                    });
                    showToast('已添加新的打卡时段，保存后生效');
                  }}
                  className="px-3 py-1.5 bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 rounded-xl text-xs font-semibold cursor-pointer transition-colors flex items-center gap-1"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>添加时段</span>
                </button>
              </div>

              <div className="space-y-2.5">
                {slots.map((slot, index) => (
                  <div
                    key={slot.id}
                    className="p-3 rounded-xl bg-slate-50 border border-slate-200/80 flex flex-wrap items-center justify-between gap-3 text-xs"
                  >
                    <div className="flex items-center gap-2">
                      <span className="w-5 h-5 rounded-full bg-blue-600 text-white text-[10px] font-bold flex items-center justify-center">
                        {index + 1}
                      </span>
                      <input
                        type="text"
                        value={slot.name}
                        onChange={(e) => {
                          const value = e.target.value;
                          setSlots((prev) => prev.map((s) => (s.id === slot.id ? { ...s, name: value } : s)));
                        }}
                        className="font-bold text-slate-800 bg-white border border-slate-200 rounded-lg px-2.5 py-1 text-xs w-28"
                      />
                    </div>

                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-slate-500 text-[11px]">有效打卡区间：</span>
                      <input
                        type="time"
                        value={slot.start}
                        onChange={(e) => {
                          const value = e.target.value;
                          setSlots((prev) => prev.map((s) => (s.id === slot.id ? { ...s, start: value } : s)));
                        }}
                        className="bg-white border border-slate-200 rounded-lg px-2 py-1 font-mono text-xs"
                      />
                      <span className="text-slate-400">至</span>
                      <input
                        type="time"
                        value={slot.end}
                        onChange={(e) => {
                          const value = e.target.value;
                          setSlots((prev) => prev.map((s) => (s.id === slot.id ? { ...s, end: value } : s)));
                        }}
                        className="bg-white border border-slate-200 rounded-lg px-2 py-1 font-mono text-xs"
                      />
                      {/* ⭐ 2026-10-20 用户方案：提醒时间点（必须 start < remind_at < end）。
                          留空 = 由后端按「结束前 5 分钟」补默认值。 */}
                      <span className="w-px h-4 bg-slate-200 mx-0.5" />
                      <span className="text-slate-500 text-[11px]">提醒时间：</span>
                      <input
                        type="time"
                        value={slot.remind_at || ''}
                        min={slot.start}
                        max={slot.end}
                        title="必须晚于开始、早于结束；留空 = 结束前 5 分钟"
                        onChange={(e) => {
                          const value = e.target.value;
                          setSlots((prev) => prev.map((s) => (s.id === slot.id ? { ...s, remind_at: value } : s)));
                        }}
                        className={`bg-white border rounded-lg px-2 py-1 font-mono text-xs ${
                          remindAtProblem(slot.start, slot.end, slot.remind_at)
                            ? 'border-rose-400 text-rose-700 bg-rose-50'
                            : 'border-slate-200'
                        }`}
                      />
                      {remindAtProblem(slot.start, slot.end, slot.remind_at) && (
                        <span className="text-rose-600 text-[10px] font-medium">
                          {remindAtProblem(slot.start, slot.end, slot.remind_at)}
                        </span>
                      )}
                      <button
                        onClick={() => {
                          if (slots.length <= 1) {
                            showToast('至少保留一个打卡时间段', 'error');
                            return;
                          }
                          setSlots((prev) => prev.filter((s) => s.id !== slot.id));
                          showToast('已删除打卡时间段，保存后生效');
                        }}
                        className="p-1.5 text-rose-500 hover:text-rose-700 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer ml-1"
                        title="删除该时段"
                      >
                        <Trash2 className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                ))}
              </div>

              <div className="flex justify-end pt-2">
                <button
                  onClick={saveSlots}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs transition-colors"
                >
                  保存打卡时段
                </button>
              </div>
            </div>

            <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-4">
              <div className="border-b border-slate-100 pb-3">
                <h3 className="font-bold text-sm text-slate-900">报送周期与照片凭证</h3>
                <p className="text-xs text-slate-500 mt-0.5">控制业务填报周期及凭证形式</p>
              </div>

              <div className="space-y-4 text-xs">
                <div>
                  <label className="font-semibold text-slate-700 block mb-1.5">
                    工作总结汇报周期
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <button
                      onClick={() => saveConfig({ period_type: 'daily' }, '总结周期已切换为每日日报')}
                      className={`p-2.5 rounded-xl border font-semibold text-center transition-all cursor-pointer ${
                        config.period_type === 'daily'
                          ? 'border-blue-500 bg-blue-50 text-blue-700'
                          : 'border-slate-200 bg-slate-50 text-slate-600'
                      }`}
                    >
                      每日日报
                    </button>
                    <button
                      onClick={() => saveConfig({ period_type: 'weekly' }, '总结周期已切换为每周周报')}
                      className={`p-2.5 rounded-xl border font-semibold text-center transition-all cursor-pointer ${
                        config.period_type === 'weekly'
                          ? 'border-blue-500 bg-blue-50 text-blue-700'
                          : 'border-slate-200 bg-slate-50 text-slate-600'
                      }`}
                    >
                      每周周报
                    </button>
                  </div>
                </div>

                <div className="pt-2 border-t border-slate-100">
                  <label className="font-semibold text-slate-700 block mb-1.5">
                    打卡拍照凭据模式
                  </label>
                  <div
                    onClick={() => saveConfig(
                      { checkin_require_image: !config.checkin_require_image },
                      config.checkin_require_image ? '已切换为免图直提打卡' : '已开启现场照片上传',
                    )}
                    className="p-3 rounded-xl border border-slate-200 bg-slate-50 hover:bg-slate-100/70 transition-all cursor-pointer space-y-1.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-semibold text-slate-800">
                        {config.checkin_require_image ? '必须上传现场照片' : '免图直提打卡'}
                      </span>
                      <span
                        className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${
                          config.checkin_require_image
                            ? 'bg-amber-100 text-amber-800'
                            : 'bg-emerald-100 text-emerald-800'
                        }`}
                      >
                        {config.checkin_require_image ? '开启中' : '免图模式'}
                      </span>
                    </div>
                    <p className="text-[11px] text-slate-500 leading-relaxed">
                      {config.checkin_require_image
                        ? '开启后群员收到带图片上传链接的打卡指令'
                        : '群员在群内发送打卡即自动完成登记，无需上传照片'}
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-4">
            <div className="border-b border-slate-100 pb-3 flex flex-col sm:flex-row sm:items-center justify-between gap-2">
              <div>
                <h3 className="font-bold text-sm text-slate-900 flex items-center gap-2">
                  <BellRing className="w-4 h-4 text-blue-600" />
                  <span>群内定时统一提醒规则 (结构化调度)</span>
                </h3>
                <p className="text-xs text-slate-500 mt-0.5">
                  标准化的时间调度器，到点由机器人自动汇总缺卡/未报名单并在群内统一催办
                </p>
              </div>

              <button
                onClick={saveReminder}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs transition-colors shrink-0 flex items-center gap-1.5"
              >
                <Save className="w-3.5 h-3.5" />
                <span>保存提醒调度</span>
              </button>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
              <div className="p-4 rounded-xl border border-slate-200 bg-slate-50/60 space-y-3 flex flex-col justify-between">
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-slate-900 text-xs flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5 text-blue-600" />
                      考勤打卡统一提醒
                    </span>
                    <input
                      type="checkbox"
                      checked={reminder.checkin}
                      onChange={(e) => setReminder({ ...reminder, checkin: e.target.checked })}
                      className="w-4 h-4 rounded accent-blue-600 cursor-pointer"
                    />
                  </div>

                  <div className="p-2.5 rounded-lg border border-slate-200 bg-white">
                    <div className="text-[10px] text-slate-500 font-semibold mb-1.5">
                      执行日（仅这些天会提醒，也仅这些天能执行打卡）
                    </div>
                    <DayPicker
                      value={reminder.checkinDays}
                      onChange={(v) => setReminder({ ...reminder, checkinDays: v })}
                    />
                  </div>

                  <div className="p-3 rounded-xl bg-blue-50/80 border border-blue-200 text-xs text-blue-950 space-y-2">
                    <div className="font-bold flex items-center gap-1.5 text-blue-900">
                      <Clock className="w-3.5 h-3.5 text-blue-600" />
                      <span>按各时段的「提醒时间点」下发</span>
                    </div>
                    <div className="space-y-1.5 font-mono text-[11px] text-blue-800">
                      {slots.map((slot) => (
                        <div
                          key={slot.id}
                          className="flex items-center justify-between bg-white/80 px-2.5 py-1 rounded-lg border border-blue-100"
                        >
                          <span>{slot.name}（{slot.start}-{slot.end}）</span>
                          <span
                            className={`font-bold ${
                              remindAtProblem(slot.start, slot.end, slot.remind_at)
                                ? 'text-rose-600'
                                : 'text-blue-900'
                            }`}
                          >
                            {slot.remind_at || '未设置（结束前 5 分钟）'}
                          </span>
                        </div>
                      ))}
                    </div>
                    <p className="text-[11px] text-blue-700 leading-relaxed border-t border-blue-200/70 pt-1.5">
                      提醒时间必须晚于时段开始、早于时段结束（例：08:00-08:30 的时段可填
                      08:01~08:29）。未填写时默认取「结束前 5 分钟」。到点仅提醒该时段内
                      仍未打卡的在岗成员，手动提醒不会影响定时提醒照常下发。
                    </p>
                  </div>
                </div>

                <div className="text-[10px] text-slate-400">
                  修改提醒时间请在上方「考勤打卡时段设置」的每个时段右侧「提醒时间」处调整，保存后立即生效。
                </div>
              </div>

              <div className="p-4 rounded-xl border border-slate-200 bg-slate-50/60 space-y-3 flex flex-col justify-between">
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-slate-900 text-xs flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5 text-amber-600" />
                      今日总结统一提醒
                    </span>
                    <input
                      type="checkbox"
                      checked={reminder.summary}
                      onChange={(e) => setReminder({ ...reminder, summary: e.target.checked })}
                      className="w-4 h-4 rounded accent-blue-600 cursor-pointer"
                    />
                  </div>

                  <div className="p-2.5 rounded-lg border border-slate-200 bg-white">
                    <div className="text-[10px] text-slate-500 font-semibold mb-1.5">
                      执行日（仅这些天会提醒，也仅这些天能执行总结）
                    </div>
                    <DayPicker
                      value={reminder.summaryDays}
                      onChange={(v) => setReminder({ ...reminder, summaryDays: v })}
                    />
                  </div>

                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-slate-700 text-xs font-bold">
                        每日下发提醒时间 <span className="text-rose-500 font-bold">*必设项</span>
                      </label>
                      <span className="text-[10px] text-amber-800 font-semibold bg-amber-100/80 px-1.5 py-0.2 rounded border border-amber-300">
                        {daysHint(reminder.summaryDays)}
                      </span>
                    </div>
                    <input
                      type="time"
                      value={reminder.summaryTime}
                      onChange={(e) => setReminder({ ...reminder, summaryTime: e.target.value })}
                      className="w-full bg-white border border-slate-200 rounded-xl p-2 font-mono text-xs focus:border-amber-500 outline-none"
                    />
                  </div>

                  <div className="p-2.5 rounded-lg bg-amber-50/70 border border-amber-200/80 text-[11px] text-amber-900">
                    <span className="font-bold block">执行日与下发时间：</span>
                    <p className="text-amber-800 leading-relaxed">
                      仅在勾选的执行日下发提醒；非执行日不提醒，也不接受成员提交总结。
                    </p>
                  </div>
                </div>

                <div className="text-[10px] text-slate-400">
                  管理员必须指定下发时间，到点自动下发未提交工作总结人员名单。
                </div>
              </div>

              <div className="p-4 rounded-xl border border-slate-200 bg-slate-50/60 space-y-3 flex flex-col justify-between">
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <span className="font-bold text-slate-900 text-xs flex items-center gap-1.5">
                      <Clock className="w-3.5 h-3.5 text-emerald-600" />
                      今日报数统一提醒
                    </span>
                    <input
                      type="checkbox"
                      checked={reminder.report}
                      onChange={(e) => setReminder({ ...reminder, report: e.target.checked })}
                      className="w-4 h-4 rounded accent-blue-600 cursor-pointer"
                    />
                  </div>

                  <div className="p-2.5 rounded-lg border border-slate-200 bg-white">
                    <div className="text-[10px] text-slate-500 font-semibold mb-1.5">
                      执行日（仅这些天会提醒，也仅这些天能执行报数）
                    </div>
                    <DayPicker
                      value={reminder.reportDays}
                      onChange={(v) => setReminder({ ...reminder, reportDays: v })}
                    />
                  </div>

                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-slate-700 text-xs font-bold">
                        每日下发提醒时间 <span className="text-rose-500 font-bold">*必设项</span>
                      </label>
                      <span className="text-[10px] text-emerald-800 font-semibold bg-emerald-100/80 px-1.5 py-0.2 rounded border border-emerald-300">
                        {daysHint(reminder.reportDays)}
                      </span>
                    </div>
                    <input
                      type="time"
                      value={reminder.reportTime}
                      onChange={(e) => setReminder({ ...reminder, reportTime: e.target.value })}
                      className="w-full bg-white border border-slate-200 rounded-xl p-2 font-mono text-xs focus:border-emerald-500 outline-none"
                    />
                  </div>

                  <div className="p-2.5 rounded-lg bg-emerald-50/70 border border-emerald-200/80 text-[11px] text-emerald-900">
                    <span className="font-bold block">执行日与下发时间：</span>
                    <p className="text-emerald-800 leading-relaxed">
                      仅在勾选的执行日下发提醒；非执行日不提醒，也不接受成员报数。
                    </p>
                  </div>
                </div>

                <div className="text-[10px] text-slate-400">
                  管理员必须指定下发时间，到点自动下发未报数人员提醒通知。
                </div>
              </div>
            </div>
          </div>
        </div>
      )}

      {subView === 'members' && (
        <div className="space-y-5">
          {phoneConflicts.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-2xl p-4 space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-bold text-amber-800">
                <AlertTriangle className="w-4 h-4 text-amber-600" />
                <span>有 {phoneConflicts.length} 条手机号撞号待处理</span>
                <span className="font-normal text-amber-700">
                  （单人单号，撞号说明数据有冲突，系统未自动合并）
                </span>
              </div>
              <div className="space-y-1.5">
                {phoneConflicts.map((c) => (
                  <div key={c.id}
                       className="flex items-start justify-between gap-3 bg-white/70 border border-amber-200 rounded-xl px-3 py-2">
                    <div className="text-[11px] text-amber-900 leading-relaxed">
                      <span className="font-mono font-semibold">{c.phone}</span>
                      <span className="mx-1.5 text-amber-400">·</span>
                      <span>{c.kind === 'realname' ? '实名填了已被占用的号' : '改号撞到别群的人'}</span>
                      <div className="text-amber-700">{c.detail}</div>
                    </div>
                    <button
                      type="button"
                      onClick={() => resolvePhoneConflict(c.id)}
                      className="shrink-0 px-2.5 py-1 text-[11px] font-semibold text-amber-800 bg-white border border-amber-300 hover:bg-amber-100 rounded-lg cursor-pointer"
                    >
                      标记已处理
                    </button>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-3">
            <h3 className="font-bold text-sm text-slate-900 flex items-center gap-1.5">
              <UserPlus className="w-4 h-4 text-blue-600" />
              <span>新增群成员</span>
            </h3>

            {memberError && (
              <div className="p-2.5 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-800 flex items-center gap-1.5">
                <AlertTriangle className="w-3.5 h-3.5 text-rose-600 shrink-0" />
                <span>{memberError}</span>
              </div>
            )}

            <form onSubmit={addMember} className="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs">
              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  真实姓名 <span className="text-rose-500 font-bold">*必填</span>
                </label>
                <input
                  type="text"
                  placeholder="例如：郑佳"
                  value={newName}
                  onChange={(e) => {
                    setNewName(e.target.value);
                    if (memberError) setMemberError(null);
                  }}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 outline-none focus:border-blue-500 focus:bg-white text-xs font-semibold text-slate-900"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  手机号码 <span className="text-rose-500 font-bold">*必填唯一标识</span>
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  maxLength={11}
                  placeholder="例如：13800000000"
                  value={newUserId}
                  onChange={(e) => {
                    setNewUserId(e.target.value);
                    if (memberError) setMemberError(null);
                  }}
                  onBlur={() => {
                    // ⭐ D32：失焦即校验（⛔ 不等提交才报错）
                    const value = newUserId.trim();
                    if (value && !PHONE_RE.test(value)) {
                      setMemberError(PHONE_INVALID_TEXT);
                    }
                  }}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 outline-none focus:border-blue-500 focus:bg-white text-xs font-mono"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">岗位角色</label>
                <select
                  value={newRole}
                  onChange={(e) => setNewRole(e.target.value)}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 outline-none focus:border-blue-500 focus:bg-white text-xs font-medium"
                >
                  {ROLE_OPTIONS.map((role) => (
                    <option key={role} value={role}>{ROLE_LABEL[role]}</option>
                  ))}
                </select>
              </div>

              <div className="flex items-end">
                <button
                  type="submit"
                  className="w-full py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs transition-colors flex items-center justify-center gap-1.5"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>添加至群成员</span>
                </button>
              </div>
            </form>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
            <div className="p-3.5 bg-slate-50/70 border-b border-slate-100 flex flex-wrap items-center justify-between gap-3 text-xs">
              <div className="relative max-w-xs w-full">
                <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  type="text"
                  placeholder="搜索群成员姓名、手机或岗位..."
                  value={memberQuery}
                  onChange={(e) => setMemberQuery(e.target.value)}
                  className="w-full bg-white border border-slate-200 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-800 outline-none focus:border-blue-500"
                />
              </div>

              <div className="flex items-center gap-2">
                <span className="text-slate-400 text-[11px]">岗位筛选：</span>
                <select
                  value={roleFilter}
                  onChange={(e) => setRoleFilter(e.target.value)}
                  className="bg-white border border-slate-200 rounded-lg px-2.5 py-1 text-xs text-slate-700 outline-none cursor-pointer"
                >
                  <option value="all">全部岗位 ({members.length})</option>
                  {ROLE_OPTIONS.map((role) => (
                    <option key={role} value={role}>{role}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead>
                  <tr className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                    <th className="py-2.5 px-4">真实姓名 (成员)</th>
                    <th className="py-2.5 px-3">手机号码 (唯一ID)</th>
                    <th className="py-2.5 px-3">岗位角色</th>
                    <th className="py-2.5 px-3">考核状态</th>
                    <th className="py-2.5 px-3">管理员权限</th>
                    <th className="py-2.5 px-4 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {filteredMembers.length === 0 ? (
                    <tr>
                      <td colSpan={6} className="py-10 text-center text-slate-400">
                        暂无匹配的群成员
                      </td>
                    </tr>
                  ) : (
                    filteredMembers.map((m) => (
                      <tr key={m.user_id} className="hover:bg-slate-50/70 transition-colors">
                        <td className="py-3 px-4 whitespace-nowrap">
                          <div className="flex items-center gap-2.5">
                            <div className="w-7 h-7 rounded-full bg-blue-600 text-white font-bold text-xs flex items-center justify-center shrink-0">
                              {(m.nickname || m.user_id).slice(0, 1)}
                            </div>
                            <div>
                              <span className="font-bold text-slate-900 text-xs">
                                {m.nickname || '未填写姓名'}
                              </span>
                            </div>
                          </div>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap font-mono font-medium text-slate-800">
                          {m.user_id}
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded-md bg-slate-100 text-slate-700 text-[11px] font-medium border border-slate-200/70">
                            {m.role || '未设置'}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span
                            className={`px-2 py-0.5 rounded-full text-[11px] font-semibold border ${
                              m.enabled
                                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                                : 'bg-slate-100 text-slate-500 border-slate-200'
                            }`}
                          >
                            {m.enabled ? '● 考核中' : '○ 已停用'}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          {grantPhones.has(m.user_id) ? (
                            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 text-[10px] font-bold">
                              <ShieldCheck className="w-3 h-3 text-blue-600" />
                              群管理员
                            </span>
                          ) : (
                            <span className="text-slate-400 text-[11px]">普通成员</span>
                          )}
                        </td>

                        <td className="py-3 px-4 text-right whitespace-nowrap">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => {
                                setEditingMember({ ...m });
                                setEditPhone(m.user_id);
                              }}
                              className="px-2 py-1 text-slate-600 hover:text-blue-600 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer text-xs"
                            >
                              编辑
                            </button>
                            <button
                              onClick={() => toggleMember(m)}
                              className="px-2 py-1 text-slate-600 hover:text-slate-900 border border-slate-200 hover:bg-slate-50 rounded-lg transition-colors cursor-pointer text-xs"
                            >
                              {m.enabled ? '暂停考核' : '恢复考核'}
                            </button>
                            <button
                              onClick={() => setDeletingMember(m)}
                              className="px-2 py-1 text-rose-600 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer text-xs"
                            >
                              移出群
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {subView === 'admins' && (
        <div className="space-y-5">
          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-3">
            <h3 className="font-bold text-sm text-slate-900 flex items-center gap-1.5">
              <ShieldCheck className="w-4 h-4 text-blue-600" />
              <span>授予群管理员身份</span>
            </h3>

            <form onSubmit={grantAdmin} className="space-y-3 text-xs">
              <div>
                <label className="font-semibold text-slate-700 block mb-1">搜索群成员</label>
                <div className="relative">
                  <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    type="text"
                    placeholder="输入姓名 / 手机号 / 岗位筛选"
                    value={grantQuery}
                    onChange={(e) => setGrantQuery(e.target.value)}
                    className="w-full bg-slate-50 border border-slate-200 rounded-xl pl-8 pr-8 py-2 outline-none focus:border-blue-500 focus:bg-white text-xs font-medium"
                  />
                  {grantQuery && (
                    <button
                      type="button"
                      onClick={() => setGrantQuery('')}
                      title="清空搜索"
                      className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-700 cursor-pointer"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>
              </div>

              {/* 候选结果列表：可点选；「没有成员 / 都授权过了 / 无匹配」三种空态分开说 */}
              <div className="border border-slate-200 rounded-xl overflow-hidden">
                <div className="px-3 py-1.5 bg-slate-50 border-b border-slate-100 flex items-center justify-between text-[11px] text-slate-500">
                  <span>
                    {grantKeyword
                      ? `匹配到 ${grantOptions.length} 位`
                      : `可授权成员 ${grantOptions.length} 位`}
                  </span>
                  {grantKeyword && (
                    <span className="font-mono text-slate-400">「{grantKeyword}」</span>
                  )}
                </div>

                {members.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[11px] text-slate-500 space-y-2">
                    <p>该群还没有应交成员，因此没有可授权的人。</p>
                    <button
                      type="button"
                      onClick={() => setSubView('members')}
                      className="px-3 py-1.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-[11px] font-semibold cursor-pointer inline-flex items-center gap-1"
                    >
                      <UserPlus className="w-3 h-3" />
                      去「群成员」页添加
                    </button>
                  </div>
                ) : eligibleMembers.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[11px] text-slate-500">
                    本群成员都已是群管理员或已停用，暂无可授权对象。
                  </div>
                ) : grantOptions.length === 0 ? (
                  <div className="px-3 py-4 text-center text-[11px] text-slate-500 space-y-2">
                    <p>
                      没有匹配「{grantKeyword}」的成员（共 {eligibleMembers.length} 位可授权）。
                    </p>
                    <button
                      type="button"
                      onClick={() => setGrantQuery('')}
                      className="px-3 py-1 border border-slate-200 hover:bg-slate-50 rounded-lg text-[11px] font-medium text-slate-700 cursor-pointer"
                    >
                      清空搜索
                    </button>
                  </div>
                ) : (
                  <ul className="max-h-56 overflow-y-auto divide-y divide-slate-100">
                    {grantOptions.map((m) => {
                      const active = m.user_id === grantTarget;
                      return (
                        <li key={m.user_id}>
                          <button
                            type="button"
                            onClick={() => setGrantTarget(active ? '' : m.user_id)}
                            className={`w-full text-left px-3 py-2 flex items-center justify-between gap-3 cursor-pointer transition-colors ${
                              active ? 'bg-blue-50' : 'hover:bg-slate-50'
                            }`}
                          >
                            <span className="flex items-center gap-2 min-w-0">
                              <span className="w-6 h-6 rounded-full bg-blue-100 text-blue-700 text-[10px] font-bold flex items-center justify-center shrink-0">
                                {(m.nickname || m.user_id).slice(0, 1)}
                              </span>
                              <span className="font-semibold text-slate-800 truncate">
                                {m.nickname || '未填写姓名'}
                              </span>
                              <span className="font-mono text-[11px] text-slate-500">
                                {m.user_id}
                              </span>
                            </span>
                            <span className="flex items-center gap-1.5 shrink-0">
                              <span className="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 border border-slate-200 text-[10px]">
                                {m.role || '成员'}
                              </span>
                              {active && <CheckCircle2 className="w-3.5 h-3.5 text-blue-600" />}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </div>

              <div className="flex items-center justify-between gap-3 pt-1">
                <span className="text-[11px] text-slate-500">
                  {grantSelected ? (
                    <>
                      已选：
                      <strong className="text-slate-800">
                        {grantSelected.nickname || grantSelected.user_id}
                      </strong>
                    </>
                  ) : (
                    '点击上方成员即可选中'
                  )}
                </span>
                <button
                  type="submit"
                  disabled={!grantTarget}
                  className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs transition-colors flex items-center justify-center gap-1.5"
                >
                  <ShieldCheck className="w-3.5 h-3.5" />
                  <span>授权为群管理员</span>
                </button>
              </div>
            </form>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
            <div className="p-3.5 bg-slate-50/70 border-b border-slate-100 flex flex-wrap items-center justify-between gap-3 text-xs">
              <div className="relative max-w-xs w-full">
                <Search className="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  type="text"
                  placeholder="搜索管理员姓名或手机号..."
                  value={adminQuery}
                  onChange={(e) => setAdminQuery(e.target.value)}
                  className="w-full bg-white border border-slate-200 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-800 outline-none focus:border-blue-500"
                />
              </div>

              <div className="text-slate-500 text-[11px]">
                当前群共拥有 <strong>{grants.length}</strong> 位群管理员
              </div>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead>
                  <tr className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                    <th className="py-2.5 px-4">管理员姓名</th>
                    <th className="py-2.5 px-3">手机号码</th>
                    <th className="py-2.5 px-3">岗位</th>
                    <th className="py-2.5 px-3">授权时间</th>
                    <th className="py-2.5 px-4 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {filteredGrants.length === 0 ? (
                    <tr>
                      <td colSpan={5} className="py-8 text-center text-slate-400">
                        暂无匹配的群管理员
                      </td>
                    </tr>
                  ) : (
                    filteredGrants.map((g) => {
                      const member = members.find((m) => m.user_id === g.user_id);
                      return (
                        <tr key={g.user_id} className="hover:bg-slate-50/70 transition-colors">
                          <td className="py-3 px-4 whitespace-nowrap">
                            <div className="flex items-center gap-2">
                              <div className="w-7 h-7 rounded-full bg-blue-100 text-blue-700 font-bold text-xs flex items-center justify-center">
                                {(g.nickname || g.user_id).slice(0, 1)}
                              </div>
                              <span className="font-bold text-slate-900">
                                {g.nickname || '未填写姓名'}
                              </span>
                            </div>
                          </td>

                          <td className="py-3 px-3 whitespace-nowrap font-mono font-medium text-slate-800">
                            {g.user_id}
                          </td>

                          <td className="py-3 px-3 whitespace-nowrap">
                            <span className="px-2 py-0.5 rounded-md bg-blue-50 text-blue-800 font-medium border border-blue-100">
                              {member?.role || '成员'}
                            </span>
                          </td>

                          <td className="py-3 px-3 whitespace-nowrap text-slate-400 font-mono text-[11px]">
                            {g.updated_at}
                          </td>

                          <td className="py-3 px-4 text-right whitespace-nowrap">
                            <button
                              onClick={() => setRevokeTarget(g)}
                              className="px-2.5 py-1 text-rose-600 hover:bg-rose-50 border border-rose-200 rounded-lg text-xs font-medium transition-colors cursor-pointer"
                            >
                              撤销权限
                            </button>
                          </td>
                        </tr>
                      );
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {subView === 'fields' && (
        <div className="space-y-5">
          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-5 space-y-3">
            <h3 className="font-bold text-sm text-slate-900 flex items-center gap-1.5">
              <Plus className="w-4 h-4 text-emerald-600" />
              <span>新增全员报数字段</span>
            </h3>

            <form onSubmit={addField} className="grid grid-cols-1 sm:grid-cols-4 gap-3 text-xs">
              <div className="sm:col-span-2">
                <label className="font-semibold text-slate-700 block mb-1">
                  指标字段名 <span className="text-emerald-600 font-bold">*必填</span>
                </label>
                <input
                  type="text"
                  placeholder="例如：客户拜访量 / 需求交付数"
                  value={fieldName}
                  onChange={(e) => setFieldName(e.target.value)}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 outline-none focus:border-emerald-500 focus:bg-white text-xs font-medium text-slate-900"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">计量单位</label>
                <input
                  type="text"
                  placeholder="个 / 次 / 项 / 万元"
                  value={fieldUnit}
                  onChange={(e) => setFieldUnit(e.target.value)}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl px-3 py-2 outline-none focus:border-emerald-500 focus:bg-white text-xs font-medium"
                />
              </div>

              <div className="flex items-end">
                <button
                  type="submit"
                  className="w-full py-2 bg-emerald-600 hover:bg-emerald-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs transition-colors flex items-center justify-center gap-1.5"
                >
                  <Plus className="w-3.5 h-3.5" />
                  <span>添加至通用字段池</span>
                </button>
              </div>
            </form>
          </div>

          <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
            <div className="p-3.5 bg-slate-50/70 border-b border-slate-100 flex items-center justify-between text-xs">
              <h4 className="font-bold text-slate-800">报数字段列表 ({fields.length})</h4>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead>
                  <tr className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                    <th className="py-2.5 px-4">字段 ID</th>
                    <th className="py-2.5 px-3">指标名称</th>
                    <th className="py-2.5 px-3">计量单位</th>
                    <th className="py-2.5 px-3">数据格式</th>
                    <th className="py-2.5 px-3">适用范围</th>
                    <th className="py-2.5 px-3">状态</th>
                    <th className="py-2.5 px-4 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {fields.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="py-10 text-center text-slate-400">
                        暂无报数字段，请先添加
                      </td>
                    </tr>
                  ) : (
                    fields.map((f) => (
                      <tr key={f.field_id} className="hover:bg-slate-50/70 transition-colors">
                        <td className="py-3 px-4 whitespace-nowrap font-mono text-[11px] text-slate-400">
                          {f.field_id}
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap font-bold text-slate-900">
                          {f.field_name}
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded bg-slate-100 font-mono text-[11px] text-slate-700 border border-slate-200/80">
                            {f.unit || '个'}
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap text-slate-500">
                          非负整数 (≥0)
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-[10px] font-semibold">
                            全群成员通用
                          </span>
                        </td>

                        <td className="py-3 px-3 whitespace-nowrap">
                          <span
                            className={`px-2 py-0.5 rounded-full text-[11px] font-bold border ${
                              f.enabled
                                ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                                : 'bg-slate-100 text-slate-500 border-slate-300'
                            }`}
                          >
                            {f.enabled ? '● 启用中' : '○ 已停用'}
                          </span>
                        </td>

                        <td className="py-3 px-4 text-right whitespace-nowrap">
                          <div className="flex items-center justify-end gap-1.5">
                            <button
                              onClick={() => toggleField(f)}
                              className="px-2 py-1 text-slate-600 hover:text-slate-900 border border-slate-200 hover:bg-slate-50 rounded-lg transition-colors cursor-pointer text-xs"
                            >
                              {f.enabled ? '停用' : '启用'}
                            </button>
                            <button
                              onClick={() => setDeletingField(f)}
                              className="px-2 py-1 text-rose-600 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer text-xs"
                            >
                              删除
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {editingMember && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-2xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <h3 className="font-bold text-sm text-slate-900">编辑群成员信息</h3>
              <button
                onClick={() => setEditingMember(null)}
                className="text-slate-400 hover:text-slate-600 cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  真实姓名 <span className="text-rose-500 font-bold">*必填</span>
                </label>
                <input
                  type="text"
                  value={editingMember.nickname}
                  onChange={(e) => setEditingMember({ ...editingMember, nickname: e.target.value })}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none font-semibold text-slate-900 focus:bg-white focus:border-blue-500"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  手机号码 (唯一ID) <span className="text-[10px] text-slate-400">可修改</span>
                </label>
                <input
                  type="text"
                  value={editPhone}
                  onChange={(e) => setEditPhone(e.target.value)}
                  placeholder="4~32 位数字/字母"
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 font-mono text-slate-800 outline-none focus:bg-white focus:border-blue-500"
                />
                <p className="text-[10px] text-amber-700 mt-1 leading-relaxed">
                  ⚠️ 改号将同步更新该成员在
                  <span className="font-semibold">所有群</span>
                  的名单与权限；历史业务记录仍挂在旧号码下，不会迁移。
                </p>
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">岗位角色</label>
                <select
                  value={ROLE_OPTIONS.includes(editingMember.role) ? editingMember.role : '开发'}
                  onChange={(e) => setEditingMember({ ...editingMember, role: e.target.value })}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:bg-white focus:border-blue-500"
                >
                  {ROLE_OPTIONS.map((role) => (
                    <option key={role} value={role}>{ROLE_LABEL[role]}</option>
                  ))}
                </select>
              </div>
            </div>

            <div className="flex justify-end gap-2 pt-2 border-t border-slate-100">
              <button
                onClick={() => setEditingMember(null)}
                className="px-4 py-2 border border-slate-200 hover:bg-slate-50 rounded-xl text-xs font-semibold text-slate-700 cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={saveMemberEdit}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs"
              >
                保存修改
              </button>
            </div>
          </div>
        </div>
      )}

      {revokeTarget && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-2xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-sm w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="w-10 h-10 rounded-full bg-rose-50 text-rose-600 flex items-center justify-center mx-auto">
              <AlertTriangle className="w-5 h-5" />
            </div>

            <div className="text-center space-y-1">
              <h3 className="font-bold text-sm text-slate-900">确认撤销群管理员身份？</h3>
              <p className="text-xs text-slate-500">
                撤销后【{revokeTarget.nickname || revokeTarget.user_id}】({revokeTarget.user_id})
                将恢复为普通成员，不再拥有群管及全量数据导出权限。
              </p>
            </div>

            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setRevokeTarget(null)}
                className="flex-1 py-2 border border-slate-200 hover:bg-slate-50 rounded-xl text-xs font-semibold text-slate-700 cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={revokeAdmin}
                className="flex-1 py-2 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs"
              >
                确认撤销
              </button>
            </div>
          </div>
        </div>
      )}

      {deletingMember && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-2xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-sm w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="w-10 h-10 rounded-full bg-rose-50 text-rose-600 flex items-center justify-center mx-auto">
              <Trash2 className="w-5 h-5" />
            </div>
            <div className="text-center space-y-1">
              <h3 className="font-bold text-sm text-slate-900">确认移出群成员？</h3>
              <p className="text-xs text-slate-500">
                移出后【{deletingMember.nickname || deletingMember.user_id}】不再参与后续任务，
                其管理员授权同时失效；历史业务记录保留。
              </p>
              <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg px-2.5 py-1.5 mt-2">
                <span className="font-semibold">后续如需恢复必须由管理员在此处手动添加，不再支持群成员 @机器人 实名录入。</span>
              </p>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setDeletingMember(null)}
                className="flex-1 py-2 border border-slate-200 hover:bg-slate-50 rounded-xl text-xs font-semibold text-slate-700 cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={removeMember}
                className="flex-1 py-2 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs"
              >
                确认移出
              </button>
            </div>
          </div>
        </div>
      )}

      {deletingField && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-2xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-sm w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="w-10 h-10 rounded-full bg-rose-50 text-rose-600 flex items-center justify-center mx-auto">
              <AlertTriangle className="w-5 h-5" />
            </div>
            <div className="text-center space-y-1">
              <h3 className="font-bold text-sm text-slate-900">确认删除该报数字段？</h3>
              <p className="text-xs text-slate-500">
                字段【{deletingField.field_name}】将从字段池中<strong className="text-rose-600">彻底删除</strong>，
                群成员和后台补录都不再能填报；已有报数记录保留字段快照，历史数据与导出不受影响。
              </p>
              <p className="text-[11px] text-slate-400 pt-1">
                只想暂时停止填报、保留字段？请用列表里的「停用」。
              </p>
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                onClick={() => setDeletingField(null)}
                className="flex-1 py-2 border border-slate-200 hover:bg-slate-50 rounded-xl text-xs font-semibold text-slate-700 cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={deleteField}
                className="flex-1 py-2 bg-rose-600 hover:bg-rose-700 text-white rounded-xl text-xs font-semibold cursor-pointer shadow-xs"
              >
                确认删除
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
