import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Download,
  Calendar,
  Search,
  CheckCircle2,
  AlertTriangle,
  BarChart3,
  Check,
  X,
  FileSpreadsheet,
} from 'lucide-react';
import { api, gid, plainError, qs } from '../../lib/api';
import { useApp } from '../../lib/store';
import type { Board, BoardMember, CellStatus } from '../../lib/types';
import { GroupSwitcher } from './GroupSwitcher';

type ExportType = 'all' | 'checkin' | 'summary' | 'report';
type ExportFormat = 'csv' | 'xlsx';

interface ExportModalState {
  type: ExportType;
  title: string;
  biz: 'checkin' | 'summary' | 'report';
  scope: 'all' | 'single';
}

const AVATAR_COLORS = [
  'bg-blue-600', 'bg-emerald-600', 'bg-indigo-600', 'bg-purple-600',
  'bg-pink-600', 'bg-amber-600', 'bg-teal-600', 'bg-rose-500',
  'bg-cyan-600', 'bg-violet-600',
];

function avatarColor(key: string): string {
  let hash = 0;
  for (const ch of key) hash = (hash * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[hash % AVATAR_COLORS.length];
}

function todayStr(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

const STATUS_TEXT: Record<CellStatus, string> = {
  completed: '正常完成',
  backfilled: '已补录',
  overdue: '待补录',
  pending: '待补录',
};

export const Statistics: React.FC = () => {
  const { groups, selectedGroupId, selectGroup } = useApp();
  const [date, setDate] = useState(todayStr());
  const [board, setBoard] = useState<Board | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const [bizViewFilter, setBizViewFilter] = useState<'all' | 'checkin' | 'summary' | 'report'>('all');
  const [statusFilter, setStatusFilter] = useState<'all' | 'completed' | 'need_backfill'>('all');
  const [search, setSearch] = useState('');

  const [toast, setToast] = useState<string | null>(null);
  const [exportModal, setExportModal] = useState<ExportModalState | null>(null);
  const [exportFormat, setExportFormat] = useState<ExportFormat>('csv');
  const [exportRemark, setExportRemark] = useState('');
  const [exporting, setExporting] = useState(false);

  const showToast = useCallback((text: string) => {
    setToast(text);
    window.setTimeout(() => setToast(null), 3500);
  }, []);

  const load = useCallback(async () => {
    if (!selectedGroupId) return;
    setLoading(true);
    setError('');
    try {
      const data = await api.get<Board>(
        `/api/admin/groups/${gid(selectedGroupId)}/board${qs({ date })}`,
      );
      setBoard(data);
    } catch (err) {
      setBoard(null);
      setError(err instanceof Error ? plainError(err.message) : '数据暂时查不到，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, [selectedGroupId, date]);

  useEffect(() => {
    load();
  }, [load]);

  const switches = board?.group.switches;
  const unknownAny = Boolean(
    board && (board.unknown.checkin || board.unknown.summary || board.unknown.report),
  );

  /** 一天是否达标：只判定本群已开启的业务（关掉的业务不追责） */
  const isAchieved = useCallback(
    (m: BoardMember): boolean => {
      const checkinOk =
        !switches?.checkin ||
        Object.values(m.checkin || {}).every((cell) => Boolean(cell.time));
      const summaryOk = !switches?.summary || m.summary !== null;
      const reportOk = !switches?.report || m.report !== null;
      return checkinOk && summaryOk && reportOk;
    },
    [switches],
  );

  const totalCount = board?.members.length || 0;
  const completedCount = useMemo(
    () => (board ? board.members.filter((m) => isAchieved(m)).length : 0),
    [board, isAchieved],
  );
  const pendingCount = totalCount - completedCount;

  const filteredList = useMemo(() => {
    if (!board) return [];
    const keyword = search.trim();
    return board.members.filter((m) => {
      if (keyword) {
        const hit =
          (m.nickname || '').includes(keyword) ||
          m.user_id.includes(keyword) ||
          (m.role || '').includes(keyword);
        if (!hit) return false;
      }
      if (statusFilter === 'completed') return isAchieved(m);
      if (statusFilter === 'need_backfill') return !isAchieved(m);
      return true;
    });
  }, [board, search, statusFilter, isAchieved]);

  const slots = board?.slots || [];
  const rosterCount = board?.counts.members || 0;

  const openExport = (type: ExportType, title: string) => {
    const biz = type === 'all' ? 'checkin' : type;
    setExportFormat('csv');
    setExportRemark(`${date} ${title}归档`);
    setExportModal({ type, title, biz, scope: type === 'all' ? 'all' : 'single' });
  };

  /** 导出：走服务端生成文件（不受当前页面筛选影响，按该群当日全量记录生成） */
  const executeExport = async () => {
    if (!exportModal || !board) return;
    setExporting(true);
    try {
      const url = `/api/admin/stats/export${qs({
        group_id: selectedGroupId,
        date,
        biz: exportModal.biz,
        scope: exportModal.scope,
        format: exportFormat,
        remark: exportRemark.trim(),
      })}`;
      const link = document.createElement('a');
      link.href = url;
      link.rel = 'noopener';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      setExportModal(null);
      showToast(`已生成【${exportModal.title}】${exportFormat === 'csv' ? 'CSV 表格' : 'Excel 工作簿'}，请查看下载文件`);
    } catch (err) {
      showToast(err instanceof Error ? plainError(err.message) : '导出未成功，请稍后重试');
    } finally {
      setExporting(false);
    }
  };

  const renderCellValue = (
    status: CellStatus,
    time: string,
    emptyText: string,
    rawStatus?: string,
  ) => {
    if (!time) {
      return <span className="text-amber-700 font-medium">{emptyText}</span>;
    }
    const late = status === 'completed' && rawStatus === 'LATE';
    return (
      <div>
        <div className="font-mono text-slate-900 font-semibold">{time}</div>
        <span
          className={`text-[10px] ${
            status === 'backfilled' ? 'text-blue-600' : late ? 'text-sky-600' : 'text-emerald-600'
          }`}
        >
          {late ? '迟到补卡' : STATUS_TEXT[status]}
        </span>
      </div>
    );
  };

  return (
    <div className="space-y-4">
      <GroupSwitcher
        icon={<BarChart3 className="w-5 h-5 text-blue-600" />}
        groups={groups}
        activeId={selectedGroupId}
        subtitle="展示并导出全天全部用户的考勤打卡、今日总结与每日报数执行清单"
        onSelect={(g) => selectGroup(g.group_id)}
        toast={showToast}
      />

      {/* 工具栏 */}
      <div className="bg-white p-4 rounded-2xl border border-slate-200/90 shadow-2xs space-y-3">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-3 border-b border-slate-100">
          <div className="flex items-center gap-2.5">
            <h2 className="text-base font-bold text-slate-900 tracking-tight">统计中心</h2>
            <span className="px-2.5 py-0.5 rounded-full bg-slate-100 text-slate-700 text-xs font-mono font-bold border border-slate-200">
              {rosterCount} 人在籍
            </span>
          </div>

          <div className="flex flex-wrap items-center gap-2 text-xs">
            <div className="flex items-center gap-1.5 bg-slate-50 border border-slate-200 rounded-xl px-2.5 py-1.5">
              <Calendar className="w-3.5 h-3.5 text-slate-400" />
              <span className="text-slate-400 font-medium">日期：</span>
              <input
                type="date"
                value={date}
                max={todayStr()}
                onChange={(e) => setDate(e.target.value)}
                className="bg-transparent font-mono font-bold text-slate-800 outline-none cursor-pointer text-xs"
              />
            </div>

            <div className="flex items-center p-0.5 bg-slate-100 rounded-xl">
              <button
                onClick={() => setBizViewFilter('all')}
                className={`px-2.5 py-1 rounded-lg transition-colors cursor-pointer text-xs font-semibold ${
                  bizViewFilter === 'all'
                    ? 'bg-white text-blue-700 shadow-2xs font-bold'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                全部业务
              </button>
              <button
                onClick={() => setBizViewFilter('checkin')}
                className={`px-2.5 py-1 rounded-lg transition-colors cursor-pointer text-xs font-semibold ${
                  bizViewFilter === 'checkin'
                    ? 'bg-white text-blue-700 shadow-2xs font-bold'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                打卡
              </button>
              <button
                onClick={() => setBizViewFilter('summary')}
                className={`px-2.5 py-1 rounded-lg transition-colors cursor-pointer text-xs font-semibold ${
                  bizViewFilter === 'summary'
                    ? 'bg-white text-amber-700 shadow-2xs font-bold'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                总结
              </button>
              <button
                onClick={() => setBizViewFilter('report')}
                className={`px-2.5 py-1 rounded-lg transition-colors cursor-pointer text-xs font-semibold ${
                  bizViewFilter === 'report'
                    ? 'bg-white text-emerald-700 shadow-2xs font-bold'
                    : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                报数
              </button>
            </div>
          </div>
        </div>

        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 text-xs">
          <div className="flex items-center gap-2">
            <div className="relative w-48 sm:w-60">
              <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                placeholder="搜索成员姓名、手机或岗位..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full bg-slate-50 border border-slate-200 rounded-xl pl-8 pr-3 py-1.5 text-xs text-slate-800 outline-none focus:bg-white focus:border-blue-500 font-medium"
              />
            </div>

            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
              className="bg-slate-50 border border-slate-200 rounded-xl px-2.5 py-1.5 text-xs text-slate-700 outline-none cursor-pointer font-medium"
            >
              <option value="all">全部状态 ({totalCount})</option>
              <option value="completed">全勤达标 ({completedCount})</option>
              <option value="need_backfill">待补录 ({pendingCount})</option>
            </select>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={() => openExport('all', '全天全部业务执行清单')}
              disabled={!board}
              className="px-3.5 py-1.5 bg-blue-600 hover:bg-blue-700 active:scale-95 disabled:opacity-50 text-white font-bold rounded-xl shadow-xs flex items-center gap-1.5 transition-all cursor-pointer text-xs"
            >
              <Download className="w-3.5 h-3.5" />
              <span>一键导出全部业务清单 (.CSV)</span>
            </button>

            <div className="flex items-center p-0.5 bg-slate-100 rounded-xl text-xs font-semibold">
              <button
                onClick={() => openExport('checkin', '考勤打卡清单')}
                disabled={!board}
                className="px-2.5 py-1.5 hover:bg-white text-slate-700 hover:text-blue-700 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
              >
                导出打卡
              </button>
              <button
                onClick={() => openExport('summary', '今日总结清单')}
                disabled={!board}
                className="px-2.5 py-1.5 hover:bg-white text-slate-700 hover:text-amber-800 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
              >
                导出总结
              </button>
              <button
                onClick={() => openExport('report', '今日报数清单')}
                disabled={!board}
                className="px-2.5 py-1.5 hover:bg-white text-slate-700 hover:text-emerald-800 rounded-lg transition-colors cursor-pointer disabled:opacity-50"
              >
                导出报数
              </button>
            </div>
          </div>
        </div>
      </div>

      {toast && (
        <div className="p-3 bg-emerald-50 border border-emerald-200 rounded-xl text-xs text-emerald-800 flex items-center gap-2 animate-in fade-in slide-in-from-top-1">
          <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
          <span className="font-semibold">{toast}</span>
        </div>
      )}

      {error && (
        <div className="p-3 bg-rose-50 border border-rose-200 rounded-xl text-xs text-rose-700 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0" />
          <span className="font-medium">{error}</span>
        </div>
      )}

      {unknownAny && (
        <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-800 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0" />
          <span className="font-medium">
            部分业务记录暂时查不到，对应列显示为「待补录」不代表真的没交，请稍后刷新或改用导出核对。
          </span>
        </div>
      )}

      {/* 主表 */}
      <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
        <div className="overflow-x-auto text-xs">
          <table className="w-full min-w-[1120px] text-left">
            <thead>
              <tr className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                <th className="py-2.5 px-4">成员 / 岗位</th>
                <th className="py-2.5 px-3">手机号码</th>

                {bizViewFilter !== 'summary' && bizViewFilter !== 'report' &&
                  slots.map((slot) => (
                    <th key={slot.id} className="py-2.5 px-3">
                      {slot.name}打卡
                    </th>
                  ))}

                {(bizViewFilter === 'all' || bizViewFilter === 'summary') && (
                  <>
                    <th className="py-2.5 px-3">今日总结</th>
                    <th className="py-2.5 px-3">字数汇报</th>
                  </>
                )}

                {(bizViewFilter === 'all' || bizViewFilter === 'report') && (
                  <>
                    <th className="py-2.5 px-3">今日报数指标</th>
                    <th className="py-2.5 px-3">指标汇总</th>
                  </>
                )}

                <th className="py-2.5 px-4 text-right">全天达标判定</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-slate-700">
              {loading ? (
                <tr>
                  <td colSpan={12} className="py-12 text-center text-slate-400">
                    正在加载业务执行数据…
                  </td>
                </tr>
              ) : filteredList.length === 0 ? (
                <tr>
                  <td colSpan={12} className="py-12 text-center text-slate-400">
                    {board && board.members.length === 0
                      ? '该群还没有应交人员名单，请先在「群管理」中添加成员。'
                      : '未找到匹配的成员业务执行数据'}
                  </td>
                </tr>
              ) : (
                filteredList.map((m) => {
                  const reportText = (m.report?.items || [])
                    .map((item) => `${item.field_name}: ${item.quantity}${item.unit || ''}`)
                    .join(', ');
                  const achieved = isAchieved(m);
                  return (
                    <tr key={m.user_id} className="hover:bg-slate-50/70 transition-colors">
                      <td className="py-3 px-4 whitespace-nowrap">
                        <div className="flex items-center gap-2">
                          <div
                            className={`w-6 h-6 rounded-full text-white font-bold text-[10px] flex items-center justify-center shrink-0 ${avatarColor(m.user_id)}`}
                          >
                            {(m.nickname || m.user_id).slice(0, 1)}
                          </div>
                          <div>
                            <span className="font-bold text-slate-900">
                              {m.nickname || m.user_id}
                            </span>
                            <span className="ml-1 text-[10px] text-slate-500 bg-slate-100 px-1.5 py-0.2 rounded border border-slate-200">
                              {m.role || '成员'}
                            </span>
                            {!m.in_roster && (
                              <span className="ml-1 text-[10px] text-amber-700 bg-amber-50 px-1.5 py-0.2 rounded border border-amber-200">
                                不在名单
                              </span>
                            )}
                          </div>
                        </div>
                      </td>

                      <td className="py-3 px-3 whitespace-nowrap font-mono font-medium text-slate-800">
                        {m.user_id}
                      </td>

                      {bizViewFilter !== 'summary' && bizViewFilter !== 'report' &&
                        slots.map((slot) => {
                          const cell = m.checkin?.[slot.id];
                          return (
                            <td key={slot.id} className="py-3 px-3 whitespace-nowrap">
                              {cell
                                ? renderCellValue(cell.status, cell.time, `缺${slot.name}卡`, cell.raw_status)
                                : <span className="text-amber-700 font-medium">缺{slot.name}卡</span>}
                            </td>
                          );
                        })}

                      {(bizViewFilter === 'all' || bizViewFilter === 'summary') && (
                        <>
                          <td
                            className="py-3 px-3 max-w-[190px] truncate"
                            title={m.summary?.content || ''}
                          >
                            {m.summary ? (
                              <span className="text-slate-800">{m.summary.content}</span>
                            ) : (
                              <span className="text-amber-700 font-medium">未提交总结</span>
                            )}
                          </td>
                          <td className="py-3 px-3 whitespace-nowrap font-mono">
                            {m.summary && m.summary.words > 0 ? (
                              <span className="font-bold text-slate-800">
                                {m.summary.words} 字
                              </span>
                            ) : (
                              <span className="text-slate-400">0</span>
                            )}
                          </td>
                        </>
                      )}

                      {(bizViewFilter === 'all' || bizViewFilter === 'report') && (
                        <>
                          <td className="py-3 px-3 max-w-[200px]" title={reportText}>
                            {m.report ? (
                              <span className="block truncate font-mono text-[11px] text-slate-700 bg-slate-50 px-2 py-0.5 rounded border border-slate-200">
                                {reportText || '已提交'}
                              </span>
                            ) : (
                              <span className="text-amber-700 font-medium">未上报指标</span>
                            )}
                          </td>
                          <td className="py-3 px-3 whitespace-nowrap font-mono font-bold text-slate-800">
                            {m.report && m.report.total > 0 ? `${m.report.total} 项` : '-'}
                          </td>
                        </>
                      )}

                      <td className="py-3 px-4 text-right whitespace-nowrap">
                        {achieved ? (
                          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-[11px] font-bold">
                            <CheckCircle2 className="w-3 h-3 text-emerald-600" />
                            今日达标
                          </span>
                        ) : (
                          <span className="inline-flex items-center gap-1 px-2.5 py-0.5 rounded-full bg-amber-50 text-amber-800 border border-amber-300 text-[11px] font-bold">
                            <AlertTriangle className="w-3 h-3 text-amber-600" />
                            待补录
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>

        <div className="bg-slate-50/80 px-4 py-3 border-t border-slate-100 flex flex-col sm:flex-row items-start sm:items-center justify-between text-[11px] text-slate-500 gap-2">
          <div className="flex items-center gap-2">
            <span>
              已加载【{board?.group.group_name || '暂无群聊'}】{date} 全天业务清单（共{' '}
              {filteredList.length} 条有效记录）。
            </span>
            <span className="text-emerald-700 font-bold">已就绪合规导出</span>
          </div>

          <button
            onClick={() => openExport('all', '全天全部业务执行清单')}
            disabled={!board}
            className="text-blue-600 hover:text-blue-800 font-bold cursor-pointer flex items-center gap-1 disabled:opacity-50"
          >
            <Download className="w-3 h-3" />
            <span>导出全量 CSV 报表</span>
          </button>
        </div>
      </div>

      {/* 导出确认弹窗 */}
      {exportModal && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center gap-2.5">
                <div className="w-9 h-9 rounded-xl bg-blue-600 text-white flex items-center justify-center font-bold shadow-xs">
                  <FileSpreadsheet className="w-4 h-4 text-white" />
                </div>
                <div>
                  <h3 className="font-bold text-slate-900 text-base">确认导出业务数据清单</h3>
                  <span className="text-[11px] text-slate-400">
                    请核对本次导出的数据范围与群聊信息
                  </span>
                </div>
              </div>
              <button
                onClick={() => setExportModal(null)}
                className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 transition-colors cursor-pointer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            <div className="p-3.5 bg-slate-50 rounded-xl border border-slate-200 text-xs space-y-2 text-slate-700">
              <div className="flex items-center justify-between">
                <span className="text-slate-500">导出清单类型：</span>
                <span className="font-bold text-blue-700 bg-blue-50 px-2 py-0.5 rounded border border-blue-200">
                  {exportModal.title}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-500">目标群聊：</span>
                <span
                  className="font-semibold text-slate-900 truncate max-w-[200px]"
                  title={board?.group.group_name}
                >
                  {board?.group.group_name || '—'}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-500">统计日期：</span>
                <span className="font-mono font-bold text-slate-900">{date}</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-500">覆盖成员数量：</span>
                <span className="font-bold text-slate-900 font-mono">{totalCount} 人</span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-slate-500">文件格式：</span>
                <div className="flex items-center p-0.5 bg-slate-100 rounded-lg">
                  <button
                    onClick={() => setExportFormat('csv')}
                    className={`px-2 py-0.5 rounded-md text-[11px] font-semibold transition-colors cursor-pointer ${
                      exportFormat === 'csv'
                        ? 'bg-white text-blue-700 shadow-2xs'
                        : 'text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    CSV
                  </button>
                  <button
                    onClick={() => setExportFormat('xlsx')}
                    className={`px-2 py-0.5 rounded-md text-[11px] font-semibold transition-colors cursor-pointer ${
                      exportFormat === 'xlsx'
                        ? 'bg-white text-blue-700 shadow-2xs'
                        : 'text-slate-500 hover:text-slate-800'
                    }`}
                  >
                    Excel
                  </button>
                </div>
              </div>
            </div>

            <div className="space-y-1.5 text-xs">
              <label className="font-semibold text-slate-700 block">
                导出备注说明 (用于审计存证)
              </label>
              <input
                type="text"
                value={exportRemark}
                onChange={(e) => setExportRemark(e.target.value)}
                placeholder="例如：2026-10-16 每日考勤总结例行归档"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800 outline-none focus:bg-white focus:border-blue-500 font-medium"
              />
              <p className="text-[11px] text-slate-400">
                提示：文件按该群当日全量记录生成，不受页面上的搜索与状态筛选影响；备注只写入操作记录，不进入文件内容。
              </p>
            </div>

            <div className="pt-2 flex items-center justify-end gap-2.5 border-t border-slate-100">
              <button
                type="button"
                onClick={() => setExportModal(null)}
                className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-semibold text-xs cursor-pointer transition-colors"
              >
                取消
              </button>
              <button
                type="button"
                onClick={executeExport}
                disabled={exporting}
                className="px-5 py-2 bg-blue-600 hover:bg-blue-700 active:scale-95 text-white font-bold rounded-xl shadow-xs flex items-center gap-1.5 transition-all cursor-pointer text-xs disabled:opacity-60"
              >
                <Download className="w-3.5 h-3.5" />
                <span>{exporting ? '正在生成…' : '确认导出并下载'}</span>
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center gap-1.5 text-[11px] text-slate-400 px-1">
        <Check className="w-3.5 h-3.5" />
        <span>打卡列按本群已配置的时间段动态展示，时段数量随群设置变化。</span>
      </div>
    </div>
  );
};
