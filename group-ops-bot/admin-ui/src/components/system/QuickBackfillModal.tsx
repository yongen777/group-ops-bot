import React, { useEffect, useMemo, useState } from 'react';
import {
  X,
  CheckCircle2,
  Clock,
  Shield,
  FileText,
  BarChart3,
  Send,
  Calendar,
  Search,
  UserCheck,
} from 'lucide-react';
import { api, gid } from '../../lib/api';
import type {
  BizType,
  BoardMember,
  CheckinSlot,
  ReportField,
} from '../../lib/types';

export interface BackfillTarget {
  memberId?: string;
  bizType?: BizType;
}

interface QuickBackfillModalProps {
  isOpen: boolean;
  onClose: () => void;
  groupId: string;
  defaultDate: string;
  periodType: 'daily' | 'weekly';
  members: BoardMember[];
  fields: ReportField[];
  slots: CheckinSlot[];
  target: BackfillTarget;
  onDone: (message: string) => void;
}

const BIZ_LABEL: Record<BizType, string> = {
  checkin: '考勤打卡',
  summary: '今日总结',
  report: '今日报数',
};

function todayStr(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function nowClockStr(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

export const QuickBackfillModal: React.FC<QuickBackfillModalProps> = ({
  isOpen,
  onClose,
  groupId,
  defaultDate,
  periodType,
  members,
  fields,
  slots,
  target,
  onDone,
}) => {
  const [selectedId, setSelectedId] = useState('');
  const [isSearchingMember, setIsSearchingMember] = useState(false);
  const [memberQuery, setMemberQuery] = useState('');
  const [bizType, setBizType] = useState<BizType>('summary');
  const [backfillDate, setBackfillDate] = useState(defaultDate || todayStr());
  const [slotId, setSlotId] = useState<string>('');
  const [nowClock, setNowClock] = useState(nowClockStr());
  const [summaryText, setSummaryText] = useState('');
  const [metricCounts, setMetricCounts] = useState<Record<string, number>>({});
  const [fieldQuery, setFieldQuery] = useState('');
  const [reason, setReason] = useState('因外勤或网络中断，经本人确认后由管理员在后台代录');
  const [note, setNote] = useState('已核验对应上报凭证');
  const [submitting, setSubmitting] = useState(false);
  const [successNotice, setSuccessNotice] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    const fallback = target.memberId
      || members.find((m) => m.enabled)?.user_id
      || members[0]?.user_id
      || '';
    setSelectedId(fallback);
    setBizType(target.bizType || 'summary');
    setBackfillDate(defaultDate || todayStr());
    setSlotId(slots[0]?.id || '');
    setSummaryText('');
    setMetricCounts({});
    setFieldQuery('');
    setMemberQuery('');
    setIsSearchingMember(false);
    setSuccessNotice(null);
    setErrorText(null);
  }, [isOpen, target.memberId, target.bizType, defaultDate, members, slots]);

  // 补录时间 = 提交时的实时时间（2026-10-19 用户指令：⛔ 不再让管理员手填）
  useEffect(() => {
    if (!isOpen) return;
    setNowClock(nowClockStr());
    const timer = window.setInterval(() => setNowClock(nowClockStr()), 1000);
    return () => window.clearInterval(timer);
  }, [isOpen]);

  const selectedMember = members.find((m) => m.user_id === selectedId)
    || members[0];
  const enabledMembers = members.filter((m) => m.enabled);

  const filteredMembers = useMemo(() => {
    const query = memberQuery.trim().toLowerCase();
    if (!query) return enabledMembers;
    return enabledMembers.filter(
      (m) => (m.nickname || '').toLowerCase().includes(query)
        || m.user_id.includes(query)
        || (m.role || '').toLowerCase().includes(query),
    );
  }, [enabledMembers, memberQuery]);

  const filteredFields = useMemo(() => {
    const query = fieldQuery.trim().toLowerCase();
    if (!query) return fields.filter((f) => f.enabled);
    return fields.filter(
      (f) => f.enabled
        && (f.field_name.toLowerCase().includes(query)
          || (f.unit || '').toLowerCase().includes(query)),
    );
  }, [fields, fieldQuery]);

  const totalQuantity = useMemo(
    () => Object.values(metricCounts).reduce((sum, v) => sum + (v || 0), 0),
    [metricCounts],
  );

  if (!isOpen) return null;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!selectedMember) {
      setErrorText('群成员名单为空，请先在群成员页添加应交人员');
      return;
    }
    setErrorText(null);
    setSubmitting(true);
    const base = `/api/admin/groups/${gid(groupId)}/backfill`;
    try {
      if (bizType === 'checkin') {
        const targets = slotId === '__all__' ? slots : slots.filter((s) => s.id === slotId);
        if (targets.length === 0) {
          throw new Error('请选择要补录的打卡时段');
        }
        for (const slot of targets) {
          await api.post(`${base}/checkin`, {
            user_id: selectedMember.user_id,
            date: backfillDate,
            session: slot.id,
            status: 'ADMIN_EDIT',
            // ⛔ 不传 checkin_time：由服务端记录实时时间（NOW）
          });
        }
      } else if (bizType === 'summary') {
        if (!summaryText.trim()) {
          throw new Error('请填写补录的工作总结正文');
        }
        await api.post(`${base}/summary`, {
          user_id: selectedMember.user_id,
          date: backfillDate,
          period_type: periodType,
          content: summaryText.trim(),
        });
      } else {
        const items = Object.entries(metricCounts)
          .filter(([, value]) => value > 0)
          .map(([fieldId, quantity]) => ({
            field_id: Number(fieldId),
            quantity,
          }));
        if (items.length === 0) {
          throw new Error('请至少填写一个报数字段的数量');
        }
        await api.post(`${base}/report`, {
          user_id: selectedMember.user_id,
          date: backfillDate,
          role: selectedMember.role || '成员',
          items,
        });
      }
      const message = `已成功为【${selectedMember.nickname || selectedMember.user_id}】完成【${BIZ_LABEL[bizType]}】补录`;
      setSuccessNotice(message);
      onDone(message);
      setTimeout(() => {
        setSuccessNotice(null);
        onClose();
      }, 900);
    } catch (err: any) {
      setErrorText(err?.message || '补录未成功，请稍后重试');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl max-w-lg w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 max-h-[92vh] flex flex-col">
        <div className="flex items-center justify-between border-b border-slate-100 pb-3.5 shrink-0">
          <div className="flex items-center gap-2.5">
            <div className="w-9 h-9 rounded-xl bg-amber-500 text-white flex items-center justify-center font-bold shadow-xs">
              <Clock className="w-4 h-4" />
            </div>
            <div>
              <h3 className="font-bold text-slate-900 text-base">管理员快捷补录业务</h3>
              <p className="text-[11px] text-slate-400 mt-0.5">
                支持多群聊、海量成员快速检索代录 · 自动流转为「已补录」存证
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-colors cursor-pointer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {successNotice && (
          <div className="my-2 p-3 bg-emerald-50 border border-emerald-200 rounded-xl text-emerald-800 text-xs flex items-center gap-2 animate-in fade-in">
            <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
            <span className="font-semibold">{successNotice}</span>
          </div>
        )}
        {errorText && (
          <div className="my-2 p-3 bg-rose-50 border border-rose-200 rounded-xl text-rose-800 text-xs flex items-center gap-2 animate-in fade-in">
            <X className="w-4 h-4 text-rose-600 shrink-0" />
            <span className="font-semibold">{errorText}</span>
          </div>
        )}

        <form onSubmit={submit} className="flex-1 overflow-y-auto py-3 space-y-4 text-xs pr-1">
          <div>
            <div className="flex items-center justify-between mb-1.5">
              <label className="font-bold text-slate-800">
                选择补录成员 <span className="text-slate-400 font-normal">（支持按姓名/手机搜索）</span>
              </label>
              {!isSearchingMember && (
                <button
                  type="button"
                  onClick={() => setIsSearchingMember(true)}
                  className="text-blue-600 hover:text-blue-700 font-semibold text-[11px] cursor-pointer"
                >
                  更换成员
                </button>
              )}
            </div>

            {!isSearchingMember ? (
              <div className="p-3 rounded-xl border border-blue-200 bg-blue-50/40 flex items-center justify-between gap-3">
                <div className="flex items-center gap-2.5">
                  <div className="w-8 h-8 rounded-full bg-blue-600 text-white font-bold flex items-center justify-center text-xs shrink-0 shadow-xs">
                    {(selectedMember?.nickname || selectedMember?.user_id || '?').slice(0, 1)}
                  </div>
                  <div>
                    <div className="flex items-center gap-1.5">
                      <span className="font-bold text-slate-900 text-xs">
                        {selectedMember?.nickname || selectedMember?.user_id || '未选择'}
                      </span>
                      <span className="text-[10px] text-slate-600 bg-white px-1.5 py-0.2 rounded border border-slate-200">
                        {selectedMember?.role || '成员'}
                      </span>
                    </div>
                    <span className="text-[10px] text-slate-400 font-mono">
                      {selectedMember?.user_id || ''}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => setIsSearchingMember(true)}
                  className="px-2.5 py-1 text-xs border border-blue-200 bg-white text-blue-700 hover:bg-blue-50 rounded-lg font-medium cursor-pointer transition-colors shadow-2xs"
                >
                  重选成员
                </button>
              </div>
            ) : (
              <div className="space-y-2 p-3 rounded-xl border border-slate-200 bg-slate-50 animate-in fade-in">
                <div className="relative">
                  <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    type="text"
                    autoFocus
                    placeholder="输入成员姓名、手机号码或岗位搜索..."
                    value={memberQuery}
                    onChange={(e) => setMemberQuery(e.target.value)}
                    className="w-full bg-white border border-slate-200 rounded-lg pl-8 pr-7 py-1.5 text-xs outline-none focus:border-blue-500 text-slate-800"
                  />
                  {memberQuery && (
                    <button
                      type="button"
                      onClick={() => setMemberQuery('')}
                      className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600"
                    >
                      <X className="w-3.5 h-3.5" />
                    </button>
                  )}
                </div>

                <div className="max-h-40 overflow-y-auto space-y-1 pr-1 divide-y divide-slate-100 bg-white rounded-lg border border-slate-200/80 p-1">
                  {filteredMembers.length === 0 ? (
                    <div className="py-4 text-center text-slate-400 text-xs">未找到匹配成员</div>
                  ) : (
                    filteredMembers.map((m) => (
                      <button
                        type="button"
                        key={m.user_id}
                        onClick={() => {
                          setSelectedId(m.user_id);
                          setIsSearchingMember(false);
                          setMemberQuery('');
                        }}
                        className={`w-full text-left p-2 rounded-md transition-colors flex items-center justify-between cursor-pointer ${
                          selectedId === m.user_id
                            ? 'bg-blue-50 text-blue-800 font-bold'
                            : 'hover:bg-slate-50 text-slate-700'
                        }`}
                      >
                        <div className="flex items-center gap-2">
                          <span className="font-semibold">{m.nickname || m.user_id}</span>
                          <span className="text-[10px] text-slate-400 font-mono">({m.user_id})</span>
                          <span className="text-[10px] text-slate-500 bg-slate-100 px-1 py-0.2 rounded">
                            {m.role || '成员'}
                          </span>
                        </div>
                        {selectedId === m.user_id && (
                          <UserCheck className="w-3.5 h-3.5 text-blue-600" />
                        )}
                      </button>
                    ))
                  )}
                </div>

                <div className="flex justify-between items-center text-[10px] text-slate-400 px-1">
                  <span>共检索出 {filteredMembers.length} 名群成员</span>
                  <button
                    type="button"
                    onClick={() => setIsSearchingMember(false)}
                    className="text-slate-500 hover:text-slate-700 underline"
                  >
                    收起列表
                  </button>
                </div>
              </div>
            )}
          </div>

          <div>
            <label className="font-bold text-slate-800 block mb-1.5">选择补录业务板块</label>
            <div className="grid grid-cols-3 gap-2">
              <button
                type="button"
                onClick={() => setBizType('checkin')}
                className={`p-2.5 rounded-xl border text-center transition-all cursor-pointer ${
                  bizType === 'checkin'
                    ? 'border-blue-600 bg-blue-50/70 text-blue-900 font-bold shadow-2xs'
                    : 'border-slate-200 hover:border-slate-300 text-slate-700 bg-white'
                }`}
              >
                <div className="flex items-center justify-center gap-1.5 mb-0.5">
                  <Clock className="w-3.5 h-3.5 text-blue-600" />
                  <span>考勤打卡</span>
                </div>
                <div className="text-[10px] text-slate-400 font-normal">上下班考勤补卡</div>
              </button>

              <button
                type="button"
                onClick={() => setBizType('summary')}
                className={`p-2.5 rounded-xl border text-center transition-all cursor-pointer ${
                  bizType === 'summary'
                    ? 'border-amber-600 bg-amber-50/70 text-amber-900 font-bold shadow-2xs'
                    : 'border-slate-200 hover:border-slate-300 text-slate-700 bg-white'
                }`}
              >
                <div className="flex items-center justify-center gap-1.5 mb-0.5">
                  <FileText className="w-3.5 h-3.5 text-amber-600" />
                  <span>今日总结</span>
                </div>
                <div className="text-[10px] text-slate-400 font-normal">工作总结代录存证</div>
              </button>

              <button
                type="button"
                onClick={() => setBizType('report')}
                className={`p-2.5 rounded-xl border text-center transition-all cursor-pointer ${
                  bizType === 'report'
                    ? 'border-emerald-600 bg-emerald-50/70 text-emerald-900 font-bold shadow-2xs'
                    : 'border-slate-200 hover:border-slate-300 text-slate-700 bg-white'
                }`}
              >
                <div className="flex items-center justify-center gap-1.5 mb-0.5">
                  <BarChart3 className="w-3.5 h-3.5 text-emerald-600" />
                  <span>今日报数</span>
                </div>
                <div className="text-[10px] text-slate-400 font-normal">动态量化指标填报</div>
              </button>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="font-semibold text-slate-700 block mb-1">补录所属日期</label>
              <div className="flex items-center gap-2 p-2 bg-slate-50 border border-slate-200 rounded-xl">
                <Calendar className="w-3.5 h-3.5 text-slate-400" />
                <input
                  type="date"
                  value={backfillDate}
                  onChange={(e) => setBackfillDate(e.target.value)}
                  className="bg-transparent outline-none w-full text-slate-800 font-medium font-mono text-xs cursor-pointer"
                />
              </div>
            </div>
            <div>
              <label className="font-semibold text-slate-700 block mb-1">经办管理员</label>
              <div className="p-2 bg-slate-100 border border-slate-200 rounded-xl text-slate-700 font-medium flex items-center gap-1.5">
                <Shield className="w-3.5 h-3.5 text-blue-600" />
                <span>当前登录账号</span>
              </div>
            </div>
          </div>

          {bizType === 'checkin' && (
            <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 space-y-3">
              <span className="font-bold text-slate-800 block">考勤打卡补录参数</span>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="text-slate-600 block mb-1">补打卡时段</label>
                  <select
                    value={slotId}
                    onChange={(e) => setSlotId(e.target.value)}
                    className="w-full bg-white border border-slate-200 rounded-xl p-2 outline-none font-medium cursor-pointer"
                  >
                    {slots.map((slot) => (
                      <option key={slot.id} value={slot.id}>
                        {slot.name}（{slot.start}-{slot.end}）
                      </option>
                    ))}
                    <option value="__all__">全部时段</option>
                  </select>
                </div>
                <div>
                  <label className="text-slate-600 block mb-1 font-semibold text-slate-700">
                    补录时间（实时）
                  </label>
                  <div className="w-full bg-slate-100 border border-slate-200 rounded-xl p-2 font-mono text-slate-700 flex items-center gap-1.5">
                    <Clock className="w-3.5 h-3.5 text-slate-400" />
                    <span>{nowClock}</span>
                  </div>
                </div>
              </div>
              <div className="text-[11px] text-slate-500">
                补录时间由系统在提交瞬间记录，⛔ 无需也无法手工修改；
                凭据形式：免图直提（管理员补录按豁免机制直接入库）。
              </div>
            </div>
          )}

          {bizType === 'summary' && (
            <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 space-y-2">
              <div className="flex items-center justify-between">
                <span className="font-bold text-slate-800">今日工作总结汇报正文</span>
                <span className="text-[10px] text-slate-400 font-mono">
                  {summaryText.length} 字
                </span>
              </div>
              <textarea
                rows={4}
                value={summaryText}
                onChange={(e) => setSummaryText(e.target.value)}
                placeholder="请输入补录的工作总结汇报正文..."
                className="w-full bg-white border border-slate-200 rounded-xl p-3 text-xs text-slate-800 outline-none focus:border-blue-500 focus:bg-white resize-y"
              />
            </div>
          )}

          {bizType === 'report' && (
            <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 space-y-3">
              <div className="flex items-center justify-between">
                <span className="font-bold text-slate-800">字段池指标量化数值填报</span>
                <span className="text-[11px] font-mono text-emerald-700 font-bold bg-emerald-50 px-2 py-0.5 rounded border border-emerald-200">
                  当前合计: {totalQuantity} 项
                </span>
              </div>

              <div className="flex items-center gap-2">
                <div className="relative flex-1">
                  <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
                  <input
                    type="text"
                    placeholder="快速检索指标字段..."
                    value={fieldQuery}
                    onChange={(e) => setFieldQuery(e.target.value)}
                    className="w-full bg-white border border-slate-200 rounded-lg pl-7 pr-3 py-1 text-xs outline-none focus:border-blue-500"
                  />
                </div>
              </div>

              <div className="max-h-48 overflow-y-auto space-y-2 pr-1">
                {filteredFields.length === 0 ? (
                  <div className="py-4 text-center text-slate-400">
                    字段池暂无可填报字段，请先在群管理中添加
                  </div>
                ) : (
                  filteredFields.map((f) => (
                    <div
                      key={f.field_id}
                      className="p-2 bg-white rounded-lg border border-slate-200/90 flex items-center justify-between gap-3"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-slate-800 text-xs">{f.field_name}</span>
                        <span className="text-[10px] text-slate-400 bg-slate-100 px-1.5 py-0.2 rounded font-mono">
                          单位: {f.unit || '个'}
                        </span>
                      </div>

                      <div className="flex items-center gap-1.5">
                        <input
                          type="number"
                          min="0"
                          value={metricCounts[f.field_id] ?? 0}
                          onChange={(e) => {
                            const value = Math.max(0, parseInt(e.target.value, 10) || 0);
                            setMetricCounts((prev) => ({ ...prev, [f.field_id]: value }));
                          }}
                          className="w-20 bg-slate-50 border border-slate-200 rounded-lg px-2 py-1 text-center font-bold text-blue-700 outline-none focus:bg-white focus:border-blue-500 font-mono text-xs"
                        />
                        <span className="text-slate-500 text-xs w-5 font-medium">{f.unit || '个'}</span>
                      </div>
                    </div>
                  ))
                )}
              </div>
            </div>
          )}

          <div className="space-y-2.5">
            <div>
              <label className="font-semibold text-slate-700 block mb-1">补录原因 (记录存证)</label>
              <input
                type="text"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="例如：因机房演练网络中断，已电话确认"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2 outline-none focus:bg-white focus:border-blue-500 text-xs"
              />
            </div>
            <div>
              <label className="font-semibold text-slate-700 block mb-1">备注核验说明 (可选)</label>
              <input
                type="text"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="例如：已在群聊截图留底并核准工单"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2 outline-none focus:bg-white focus:border-blue-500 text-xs"
              />
            </div>
            <p className="text-[10px] text-slate-400">
              补录原因与核验说明随操作一并记入后台审计，⛔ 不写入业务记录正文。
            </p>
          </div>

          <div className="pt-2 flex items-center justify-end gap-2 shrink-0 border-t border-slate-100">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-medium cursor-pointer transition-colors"
            >
              取消
            </button>
            <button
              type="submit"
              disabled={submitting}
              className="px-5 py-2 bg-blue-600 hover:bg-blue-700 active:scale-95 disabled:bg-slate-300 text-white font-bold rounded-xl shadow-xs flex items-center gap-1.5 transition-all cursor-pointer"
            >
              <Send className="w-3.5 h-3.5" />
              <span>{submitting ? '提交补录中...' : '确认补录并存证'}</span>
            </button>
          </div>
        </form>
      </div>
    </div>
  );
};
