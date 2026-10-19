import React, { useEffect, useMemo, useState } from 'react';
import { AlertTriangle, CheckCircle2, Pencil, X } from 'lucide-react';
import { api, gid } from '../../lib/api';
import type { BoardMember, CheckinSlot } from '../../lib/types';

type BizKey = 'checkin' | 'summary' | 'report';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  groupId: string;
  date: string;
  member: BoardMember | null;
  slots: CheckinSlot[];
  initialBiz: BizKey;
  onSaved: (message: string) => void;
}

const STATUS_OPTIONS: { value: string; label: string }[] = [
  { value: 'NORMAL', label: '正常完成' },
  { value: 'LATE', label: '迟到补卡' },
  { value: 'ADMIN_EDIT', label: '后台补录' },
];

function hhmm(value?: string): string {
  const text = (value || '').trim();
  return text.length >= 5 ? text.slice(0, 5) : '';
}

/**
 * 管理员修改既有业务记录（D17）。
 *
 * 硬约束：
 * - ⛔ 只改已有记录：没有记录的业务在这里只提示走「发起补录」
 * - 修改事由必填、入库备注可选；两者随审计一起落库
 * - 保存成功后由调用方重新拉看板（⛔ 本地乐观更新，以库为准）
 */
export const AdminRecordEditModal: React.FC<Props> = ({
  isOpen,
  onClose,
  groupId,
  date,
  member,
  slots,
  initialBiz,
  onSaved,
}) => {
  const [biz, setBiz] = useState<BizKey>(initialBiz);
  const [checkinDraft, setCheckinDraft] = useState<Record<string, { time: string; status: string }>>({});
  const [summaryText, setSummaryText] = useState('');
  const [summaryTime, setSummaryTime] = useState('');
  const [reportDraft, setReportDraft] = useState<Record<number, string>>({});
  const [reportTime, setReportTime] = useState('');
  const [reason, setReason] = useState('');
  const [remark, setRemark] = useState('');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  const hasCheckin = useMemo(
    () => slots.some((slot) => !!member?.checkin?.[slot.id]?.time),
    [slots, member],
  );

  useEffect(() => {
    if (!isOpen || !member) return;
    setBiz(initialBiz);
    const draft: Record<string, { time: string; status: string }> = {};
    slots.forEach((slot) => {
      const cell = member.checkin?.[slot.id];
      if (!cell?.time) return;
      draft[slot.id] = {
        time: hhmm(cell.time),
        status: cell.status === 'backfilled' ? 'ADMIN_EDIT' : (cell.raw_status || 'NORMAL'),
      };
    });
    setCheckinDraft(draft);
    setSummaryText(member.summary?.content || '');
    setSummaryTime(hhmm(member.summary?.at));
    const reportItems: Record<number, string> = {};
    (member.report?.items || []).forEach((item) => {
      reportItems[item.field_id] = String(item.quantity);
    });
    setReportDraft(reportItems);
    setReportTime(hhmm(member.report?.at));
    setReason('');
    setRemark('');
    setError('');
    setSaving(false);
  }, [isOpen, member, initialBiz, slots]);

  if (!isOpen || !member) return null;
  const name = member.nickname || member.user_id;

  const submit = async () => {
    setError('');
    if (!reason.trim()) {
      setError('请填写修改事由（必填），便于后续追溯。');
      return;
    }
    const body: Record<string, unknown> = {
      user_id: member.user_id,
      date,
      reason: reason.trim(),
      remark: remark.trim(),
    };

    if (biz === 'checkin') {
      const changed = Object.entries(checkinDraft).filter(([, value]) => value.time);
      if (!changed.length) {
        setError('该成员今日没有可修改的打卡记录，请改用「发起补录」。');
        return;
      }
      body.slots = changed.map(([session, value]) => ({
        session,
        time: value.time,
        status: value.status,
      }));
    } else if (biz === 'summary') {
      if (!member.summary) {
        setError('该成员本周期还没有总结记录，请改用「发起补录」。');
        return;
      }
      if (!summaryText.trim()) {
        setError('总结正文不能为空。');
        return;
      }
      body.content = summaryText.trim();
      if (summaryTime) body.time = summaryTime;
    } else {
      const items = Object.entries(reportDraft).map(([fieldId, value]) => ({
        field_id: Number(fieldId),
        quantity: value.trim() === '' ? 0 : Number(value),
      }));
      if (!items.length) {
        setError('该成员本周期还没有报数记录，请改用「发起补录」。');
        return;
      }
      if (items.some((item) => !Number.isInteger(item.quantity) || item.quantity < 0)) {
        setError('报数必须是不小于 0 的整数。');
        return;
      }
      body.items = items;
      if (reportTime) body.time = reportTime;
    }

    setSaving(true);
    try {
      const result = await api.post<{ ok: boolean; message: string }>(
        `/api/admin/groups/${gid(groupId)}/records/${biz}/edit`,
        body,
      );
      onSaved(result.message || '记录已更新并保存入库');
      onClose();
    } catch (err: any) {
      setError(err?.message || '保存未成功，请稍后重试');
    } finally {
      setSaving(false);
    }
  };

  const tabs: { key: BizKey; label: string; ready: boolean }[] = [
    { key: 'checkin', label: '打卡记录', ready: hasCheckin },
    { key: 'summary', label: '今日总结', ready: !!member.summary },
    { key: 'report', label: '今日报数', ready: !!member.report },
  ];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4">
      <div className="w-full max-w-2xl bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden flex flex-col max-h-[92vh]">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-start justify-between gap-3">
          <div>
            <h3 className="font-bold text-slate-900 text-sm flex items-center gap-2">
              <Pencil className="w-4 h-4 text-blue-600" />
              <span>修改业务记录 · {name}</span>
            </h3>
            <p className="text-[11px] text-slate-500 mt-1">
              业务日期 {date} · 修改内容直接保存入库，并记入审计（修改事由必填）
            </p>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors cursor-pointer"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="px-5 pt-3">
          <div className="flex p-0.5 bg-slate-100 rounded-xl text-xs font-medium">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => { setBiz(tab.key); setError(''); }}
                className={`flex-1 px-3 py-1.5 rounded-lg transition-all cursor-pointer ${
                  biz === tab.key ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                {tab.label}
                {!tab.ready && <span className="ml-1 text-[10px] text-slate-400">无记录</span>}
              </button>
            ))}
          </div>
        </div>

        <div className="p-5 space-y-4 overflow-y-auto">
          {biz === 'checkin' && (
            <div className="space-y-2">
              {slots.map((slot) => {
                const cell = member.checkin?.[slot.id];
                if (!cell?.time) {
                  return (
                    <div key={slot.id} className="text-[11px] text-slate-400">
                      {slot.name}：今日无打卡记录（如需补录，请关闭本窗口用「发起补录」）
                    </div>
                  );
                }
                const draft = checkinDraft[slot.id] || { time: hhmm(cell.time), status: 'NORMAL' };
                return (
                  <div key={slot.id} className="flex items-center gap-3 p-3 rounded-xl bg-slate-50 border border-slate-200">
                    <span className="text-xs font-semibold text-slate-700 w-16 shrink-0">{slot.name}</span>
                    <input
                      type="time"
                      value={draft.time}
                      onChange={(e) => setCheckinDraft((prev) => ({
                        ...prev,
                        [slot.id]: { ...draft, time: e.target.value },
                      }))}
                      className="px-2 py-1.5 rounded-lg border border-slate-200 text-xs font-mono text-slate-800 outline-none focus:border-blue-500"
                    />
                    <select
                      value={draft.status}
                      onChange={(e) => setCheckinDraft((prev) => ({
                        ...prev,
                        [slot.id]: { ...draft, status: e.target.value },
                      }))}
                      className="px-2 py-1.5 rounded-lg border border-slate-200 text-xs text-slate-800 outline-none focus:border-blue-500"
                    >
                      {STATUS_OPTIONS.map((opt) => (
                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                      ))}
                    </select>
                    <span className="text-[11px] text-slate-400 ml-auto">
                      {cell.status === 'backfilled' ? '当前：已补录' : '当前：已完成'}
                    </span>
                  </div>
                );
              })}
            </div>
          )}

          {biz === 'summary' && (
            <div className="space-y-3">
              <div>
                <label className="text-[11px] font-semibold text-slate-600 mb-1 block">总结正文</label>
                <textarea
                  value={summaryText}
                  onChange={(e) => setSummaryText(e.target.value)}
                  rows={7}
                  className="w-full px-3 py-2 rounded-xl border border-slate-200 text-xs text-slate-800 leading-relaxed outline-none focus:border-blue-500"
                />
              </div>
              <div className="flex items-center gap-3">
                <label className="text-[11px] font-semibold text-slate-600">提交时间</label>
                <input
                  type="time"
                  value={summaryTime}
                  onChange={(e) => setSummaryTime(e.target.value)}
                  className="px-2 py-1.5 rounded-lg border border-slate-200 text-xs font-mono text-slate-800 outline-none focus:border-blue-500"
                />
                <span className="text-[11px] text-slate-400">留空则保留原提交时间</span>
              </div>
            </div>
          )}

          {biz === 'report' && (
            <div className="space-y-3">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                {(member.report?.items || []).map((item) => (
                  <div key={item.field_id} className="flex items-center gap-2 p-2.5 rounded-xl bg-slate-50 border border-slate-200">
                    <span className="text-xs text-slate-700 flex-1 truncate">
                      {item.field_name}
                      {item.unit ? `（${item.unit}）` : ''}
                    </span>
                    <input
                      type="number"
                      min={0}
                      step={1}
                      value={reportDraft[item.field_id] ?? ''}
                      onChange={(e) => setReportDraft((prev) => ({
                        ...prev,
                        [item.field_id]: e.target.value,
                      }))}
                      className="w-20 px-2 py-1.5 rounded-lg border border-slate-200 text-xs font-mono text-slate-800 outline-none focus:border-blue-500"
                    />
                  </div>
                ))}
              </div>
              <div className="flex items-center gap-3">
                <label className="text-[11px] font-semibold text-slate-600">提交时间</label>
                <input
                  type="time"
                  value={reportTime}
                  onChange={(e) => setReportTime(e.target.value)}
                  className="px-2 py-1.5 rounded-lg border border-slate-200 text-xs font-mono text-slate-800 outline-none focus:border-blue-500"
                />
                <span className="text-[11px] text-slate-400">留空则保留原提交时间</span>
              </div>
            </div>
          )}

          <div className="space-y-2 pt-1 border-t border-slate-100">
            <div>
              <label className="text-[11px] font-semibold text-slate-600 mb-1 block">
                修改事由 <span className="text-rose-600">（必填）</span>
              </label>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                maxLength={200}
                placeholder="例如：本人反馈打卡时间记录有误，管理员核对后修正"
                className="w-full px-3 py-2 rounded-xl border border-slate-200 text-xs text-slate-800 outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="text-[11px] font-semibold text-slate-600 mb-1 block">入库备注（可选）</label>
              <input
                value={remark}
                onChange={(e) => setRemark(e.target.value)}
                maxLength={500}
                placeholder="补充说明，例如凭证编号 / 审批人"
                className="w-full px-3 py-2 rounded-xl border border-slate-200 text-xs text-slate-800 outline-none focus:border-blue-500"
              />
            </div>
          </div>

          {error && (
            <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-[11px] text-rose-700 flex items-start gap-2">
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100 flex items-center justify-between gap-2">
          <span className="text-[11px] text-slate-400">修改会写入审计，不会删除原有留痕</span>
          <div className="flex items-center gap-2">
            <button
              onClick={onClose}
              className="px-3.5 py-1.5 rounded-lg border border-slate-200 text-slate-700 text-xs font-semibold hover:bg-slate-50 transition-colors cursor-pointer"
            >
              取消
            </button>
            <button
              onClick={submit}
              disabled={saving}
              className="inline-flex items-center gap-1 px-4 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white text-xs font-semibold transition-colors cursor-pointer"
            >
              <CheckCircle2 className="w-3.5 h-3.5" />
              <span>{saving ? '正在保存…' : '保存入库'}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};
