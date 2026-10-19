import React from 'react';
import { FileText, Pencil, X } from 'lucide-react';
import type { BoardMember } from '../../lib/types';

interface Props {
  member: BoardMember | null;
  onClose: () => void;
  onOpenEdit: (member: BoardMember) => void;
}

/**
 * 总结全文详情（多段式）。
 *
 * 硬约束：⛔ 不编造字段。后端只存「正文 / 提交时间 / 来源 / 周期类型」，
 * 这里就只展示这四项 —— 没有的段落（例如协同进展）不虚构。
 * 2026-10-19 用户指令：移除附件图片段（该业务本就不采集图片）。
 */
export const SummaryDetailModal: React.FC<Props> = ({
  member,
  onClose,
  onOpenEdit,
}) => {
  if (!member || !member.summary) return null;
  const s = member.summary;
  const name = member.nickname || member.user_id;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4">
      <div className="w-full max-w-3xl bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden flex flex-col max-h-[92vh]">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-start justify-between gap-3">
          <div>
            <h3 className="font-bold text-slate-900 text-sm flex items-center gap-2">
              <FileText className="w-4 h-4 text-amber-600" />
              <span>{name} · 今日工作总结全文</span>
            </h3>
            <div className="flex flex-wrap items-center gap-2 mt-1 text-[11px] text-slate-500">
              <span className="px-2 py-0.5 rounded-full bg-slate-100 text-slate-700 font-medium">
                {member.role || '成员'}
              </span>
              <span className="font-mono">{s.at || '未记录提交时间'}</span>
              <span>{s.words} 字</span>
              <span className="px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 font-medium">
                {s.entry_source === 'admin' ? '管理员代录' : '群员提交'}
              </span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg text-slate-400 hover:text-slate-700 hover:bg-slate-100 transition-colors cursor-pointer"
            title="关闭"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-5 space-y-4 overflow-y-auto">
          <section>
            <h4 className="text-xs font-bold text-slate-900 mb-1.5">工作事项与汇报正文</h4>
            <div className="p-3.5 rounded-xl bg-slate-50 border border-slate-200 text-xs text-slate-700 leading-relaxed whitespace-pre-wrap break-words">
              {s.content || '（正文为空）'}
            </div>
          </section>

          <section>
            <h4 className="text-xs font-bold text-slate-900 mb-1.5">提交与审计</h4>
            <div className="rounded-xl border border-slate-200 divide-y divide-slate-100 text-[11px]">
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-slate-500">提交时间</span>
                <span className="font-mono text-slate-800">{s.at || '—'}</span>
              </div>
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-slate-500">提交方式</span>
                <span className="text-slate-800">
                  {s.entry_source === 'admin' ? '管理员后台代录' : '群内提交'}
                </span>
              </div>
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-slate-500">统计周期</span>
                <span className="text-slate-800">
                  {s.period_type === 'weekly' ? '周报' : '日报'}
                </span>
              </div>
              <div className="flex items-center justify-between px-3 py-2">
                <span className="text-slate-500">正文字数</span>
                <span className="text-slate-800">{s.words} 字</span>
              </div>
            </div>
          </section>
        </div>

        <div className="px-5 py-3 border-t border-slate-100 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 rounded-lg border border-slate-200 text-slate-700 text-xs font-semibold hover:bg-slate-50 transition-colors cursor-pointer"
          >
            关闭
          </button>
          <button
            onClick={() => onOpenEdit(member)}
            className="inline-flex items-center gap-1 px-3.5 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-700 text-white text-xs font-semibold transition-colors cursor-pointer"
          >
            <Pencil className="w-3.5 h-3.5" />
            <span>修改这条总结</span>
          </button>
        </div>
      </div>
    </div>
  );
};
