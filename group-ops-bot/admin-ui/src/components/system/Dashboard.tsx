import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Bot,
  CheckCircle2,
  AlertTriangle,
  Search,
  Clock,
  FileText,
  BarChart3,
  Layers,
  BellRing,
  Pencil,
  Eye,
  UserPlus,
} from 'lucide-react';
import { api, gid } from '../../lib/api';
import { useApp } from '../../lib/store';
import type {
  Board,
  BoardMember,
  BizType,
  CellStatus,
  ReportField,
} from '../../lib/types';
import { GroupSwitcher } from './GroupSwitcher';
import {
  QuickBackfillModal,
  type BackfillTarget,
} from './QuickBackfillModal';
import { ImagePreviewModal, type ImagePreviewData } from './ImagePreviewModal';
import { SummaryDetailModal } from './SummaryDetailModal';
import { AdminRecordEditModal } from './AdminRecordEditModal';
import { AddMemberModal } from './AddMemberModal';

function todayStr(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/** 凭证缩略图：加载失败（超过保留期）时退回文字，⛔ 不留破图 */
const ProofThumb: React.FC<{ refId: string; onOpen: () => void }> = ({ refId, onOpen }) => {
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => setFailed(false), [refId]);
  if (failed) {
    return <span className="text-slate-400 text-[11px]">凭证已过保留期</span>;
  }
  return (
    <button
      onClick={onOpen}
      className="flex items-center gap-2 group cursor-pointer"
      title="点击查看凭证大图"
    >
      <span className="relative w-8 h-8 rounded-lg overflow-hidden border border-slate-200 group-hover:ring-2 group-hover:ring-blue-500 transition-all shrink-0 bg-slate-100 flex items-center justify-center">
        <img
          src={`/api/admin/uploads/${encodeURIComponent(refId)}`}
          alt="打卡凭证"
          onError={() => setFailed(true)}
          className="w-full h-full object-cover"
        />
        <span className="absolute inset-0 bg-black/25 opacity-0 group-hover:opacity-100 flex items-center justify-center transition-opacity">
          <Eye className="w-3.5 h-3.5 text-white" />
        </span>
      </span>
      <span className="text-[11px] text-blue-600 group-hover:text-blue-800 font-medium">
        查看大图
      </span>
    </button>
  );
};

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

export const Dashboard: React.FC = () => {
  const { groups, selectedGroupId, selectGroup } = useApp();
  const [board, setBoard] = useState<Board | null>(null);
  const [fields, setFields] = useState<ReportField[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [toast, setToast] = useState<string | null>(null);
  const [subTab, setSubTab] = useState<'matrix' | 'checkin' | 'summary' | 'report'>('matrix');
  const [search, setSearch] = useState('');
  const [filterTab, setFilterTab] = useState<'all' | 'need_backfill' | 'completed'>('all');
  const [modalOpen, setModalOpen] = useState(false);
  const [backfillTarget, setBackfillTarget] = useState<BackfillTarget>({});
  // D17：凭证图片预览 / 总结全文 / 管理员修改 / 一键添加成员
  const [imagePreview, setImagePreview] = useState<ImagePreviewData | null>(null);
  const [summaryDetail, setSummaryDetail] = useState<BoardMember | null>(null);
  const [editTarget, setEditTarget] = useState<{
    member: BoardMember;
    biz: 'checkin' | 'summary' | 'report';
  } | null>(null);
  const [addMemberTarget, setAddMemberTarget] = useState<BoardMember | null>(null);
  const [reminding, setReminding] = useState(false);

  const date = todayStr();

  const showToast = useCallback((message: string) => {
    setToast(message);
    setTimeout(() => setToast(null), 3200);
  }, []);

  const load = useCallback(async () => {
    if (!selectedGroupId) return;
    setLoading(true);
    setError('');
    try {
      const [boardData, fieldData] = await Promise.all([
        api.get<Board>(`/api/admin/groups/${gid(selectedGroupId)}/board?date=${date}`),
        api.get<{ ok: boolean; fields: ReportField[] }>(
          `/api/admin/groups/${gid(selectedGroupId)}/fields`,
        ),
      ]);
      setBoard(boardData);
      setFields(fieldData.fields || []);
    } catch (err: any) {
      setError(err?.message || '数据暂时查不到，请稍后重试');
      setBoard(null);
    } finally {
      setLoading(false);
    }
  }, [selectedGroupId, date]);

  useEffect(() => {
    load();
  }, [load]);

  const slots = board?.slots || [];
  const members = board?.members || [];
  const counts = board?.counts;

  const overallDone = useCallback(
    (member: BoardMember) => slots.every((slot) => member.checkin[slot.id]?.time)
      && !!member.summary && !!member.report,
    [slots],
  );

  const pendingMembers = useMemo(
    () => members.filter((m) => !overallDone(m)),
    [members, overallDone],
  );

  const completedMembers = useMemo(
    () => members.filter((m) => overallDone(m)),
    [members, overallDone],
  );

  const matches = useCallback(
    (m: BoardMember) => {
      const query = search.trim();
      if (!query) return true;
      return (m.nickname || '').includes(query)
        || m.user_id.includes(query)
        || (m.role || '').includes(query);
    },
    [search],
  );

  const openBackfill = (member?: BoardMember, biz?: BizType) => {
    if (member) {
      const missingBiz: BizType = slots.some((s) => !member.checkin[s.id]?.time)
        ? 'checkin'
        : !member.summary
          ? 'summary'
          : 'report';
      setBackfillTarget({ memberId: member.user_id, bizType: biz || missingBiz });
    } else {
      setBackfillTarget({});
    }
    setModalOpen(true);
  };

  // 提醒业务：向今日未达标成员定向催办（复用后台手动提醒链路，⛔ 不伪造结果）
  const handleRemindPending = async () => {
    if (!selectedGroupId) return;
    if (pendingMembers.length === 0) {
      showToast('当前群聊今日全员皆已达标，暂无需要提醒的成员');
      return;
    }
    setReminding(true);
    try {
      const result = await api.post<{ ok: boolean; message: string }>(
        `/api/admin/groups/${gid(selectedGroupId)}/reminders/run`,
        {},
      );
      showToast(result.message || '已发起提醒');
      load();
    } catch (err: any) {
      showToast(err?.message || '提醒未发出，请稍后重试');
    } finally {
      setReminding(false);
    }
  };

  const openCheckinImage = (member: BoardMember, slotId: string, slotName: string) => {
    const cell = member.checkin?.[slotId];
    if (!cell?.image_ref) {
      showToast('该时段没有上传凭证图片');
      return;
    }
    const name = member.nickname || member.user_id;
    setImagePreview({
      url: `/api/admin/uploads/${encodeURIComponent(cell.image_ref)}`,
      title: `${name} - ${slotName}打卡凭证`,
      memberName: name,
      time: cell.time,
      type: '考勤打卡照片',
      note: `提报人 ${name}（${member.role || '成员'}）上传的打卡现场照片`,
    });
  };

  const statusCell = (status: CellStatus, member: BoardMember, biz: BizType) => (
    renderStatusBadge(status, member, biz)
  );

  const renderStatusBadge = (status: CellStatus,
    member?: BoardMember,
    biz?: BizType,
    rawStatus?: string,
  ) => {
    if (status === 'completed') {
      // 迟到补卡仍算达标，但标签如实区分（⛔ 不把 LATE 说成正常完成）
      const late = rawStatus === 'LATE';
      return (
        <span
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium select-none border ${
            late
              ? 'bg-sky-50 text-sky-700 border-sky-200'
              : 'bg-emerald-50 text-emerald-700 border-emerald-200'
          }`}
        >
          <span
            className={`w-1.5 h-1.5 rounded-full ${late ? 'bg-sky-500' : 'bg-emerald-500'}`}
          ></span>
          {late ? '迟到补卡' : '正常完成'}
        </span>
      );
    }
    if (status === 'backfilled') {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-50 text-blue-700 border border-blue-200 text-[11px] font-medium select-none">
          <CheckCircle2 className="w-3 h-3 text-blue-600" />
          已补录
        </span>
      );
    }
    return (
      <button
        onClick={(e) => {
          e.stopPropagation();
          openBackfill(member, biz);
        }}
        className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-amber-50 hover:bg-amber-100 text-amber-800 border border-amber-300 border-dashed text-[11px] font-semibold transition-colors cursor-pointer"
        title="点击直接为其补录"
      >
        <AlertTriangle className="w-3 h-3 text-amber-600" />
        <span>{status === 'overdue' ? '逾期（待补录）' : '待补录'}</span>
      </button>
    );
  };

  const memberCell = (m: BoardMember) => (
    <div className="flex items-center gap-2">
      <div
        className={`w-6 h-6 rounded-full text-white font-bold text-[10px] flex items-center justify-center shrink-0 ${avatarColor(m.user_id)}`}
      >
        {(m.nickname || m.user_id).slice(0, 1)}
      </div>
      <div>
        <div className="flex items-center gap-1.5">
          <span className="text-slate-900 font-bold">{m.nickname || m.user_id}</span>
          <span className="text-[10px] text-slate-400 bg-slate-100 px-1.5 py-0.2 rounded">
            {m.role || '成员'}
          </span>
          {!m.in_roster && (
            <button
              onClick={(e) => {
                e.stopPropagation();
                setAddMemberTarget(m);
              }}
              className="inline-flex items-center gap-1 px-1.5 py-0.2 rounded bg-emerald-50 hover:bg-emerald-100 text-emerald-700 border border-emerald-200 text-[10px] font-semibold cursor-pointer"
              title="该成员不在名单中，点击填写姓名与岗位角色后录入"
            >
              <UserPlus className="w-3 h-3" />
              <span>未在名单 · 一键添加</span>
            </button>
          )}
        </div>
        <div className="text-[10px] text-slate-400 font-mono">{m.user_id}</div>
      </div>
    </div>
  );

  const checkinDone = counts?.checkin_done ?? 0;
  const summaryDone = counts?.summary_done ?? 0;
  const reportDone = counts?.report_done ?? 0;
  const total = counts?.total ?? 0;
  const percent = (done: number) => (total ? Math.round((done / total) * 100) : 0);

  return (
    <div className="space-y-5">
      <GroupSwitcher
        icon={<Bot className="w-5 h-5" />}
        groups={groups}
        activeId={selectedGroupId}
        subtitle="展示当前群聊成员的打卡、总结与报数实时完成情况"
        onSelect={(group) => selectGroup(group.group_id)}
        toast={showToast}
        actions={
          <button
            onClick={() => openBackfill()}
            className="px-3.5 py-2 text-xs bg-amber-500 hover:bg-amber-600 text-white rounded-xl font-semibold shadow-xs flex items-center gap-1.5 transition-all cursor-pointer"
          >
            <Clock className="w-3.5 h-3.5" />
            <span>发起补录业务</span>
          </button>
        }
      />

      {toast && (
        <div className="p-3 bg-emerald-50 border border-emerald-200 rounded-xl text-xs text-emerald-800 flex items-center gap-2 animate-in fade-in slide-in-from-top-1">
          <CheckCircle2 className="w-4 h-4 text-emerald-600 shrink-0" />
          <span className="font-semibold">{toast}</span>
        </div>
      )}

      {error && (
        <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-xs text-amber-800 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4 text-amber-600 shrink-0" />
          <span className="font-semibold">{error}</span>
        </div>
      )}

      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div
          onClick={() => setSubTab('checkin')}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            subTab === 'checkin'
              ? 'bg-blue-50/40 border-blue-500 shadow-sm'
              : 'bg-white border-slate-200/90 shadow-2xs hover:border-emerald-300'
          }`}
        >
          <div className="flex items-start justify-between">
            <div>
              <span className="text-xs font-medium text-slate-500">① 今日打卡业务</span>
              <div className="flex items-baseline gap-1 mt-1.5">
                <span className="text-2xl font-black text-slate-900 tracking-tight">
                  {checkinDone} / {total}
                </span>
                <span className="ml-2 text-[11px] font-bold text-emerald-600 bg-emerald-50 px-2 py-0.5 rounded-full border border-emerald-100">
                  {percent(checkinDone)}%
                </span>
              </div>
              <span className="text-[11px] text-slate-500 mt-1 block">
                {checkinDone} 人已完成 ·{' '}
                {total - checkinDone > 0 ? (
                  <span className="text-amber-700 font-semibold">{total - checkinDone} 人待补录</span>
                ) : (
                  <span className="text-emerald-700">全员打卡完成</span>
                )}
              </span>
            </div>
            <div className="relative w-12 h-12 shrink-0">
              <svg className="w-full h-full -rotate-90" viewBox="0 0 36 36">
                <circle cx="18" cy="18" r="14" fill="none" stroke="#f1f5f9" strokeWidth="3.5" />
                <circle
                  cx="18" cy="18" r="14" fill="none" stroke="#10b981" strokeWidth="3.5"
                  strokeDasharray="87.96"
                  strokeDashoffset={87.96 * (1 - (total ? checkinDone / total : 0))}
                  strokeLinecap="round"
                />
              </svg>
              <span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-slate-700">
                {percent(checkinDone)}%
              </span>
            </div>
          </div>
        </div>

        <div
          onClick={() => setSubTab('summary')}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            subTab === 'summary'
              ? 'bg-amber-50/40 border-amber-500 shadow-sm'
              : 'bg-white border-slate-200/90 shadow-2xs hover:border-amber-300'
          }`}
        >
          <div className="flex items-start justify-between">
            <div>
              <span className="text-xs font-medium text-slate-500">② 今日总结业务</span>
              <div className="flex items-baseline gap-1 mt-1.5">
                <span className="text-2xl font-black text-slate-900 tracking-tight">
                  {summaryDone} / {total}
                </span>
                <span className="ml-2 text-[11px] font-bold text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full border border-amber-200">
                  {percent(summaryDone)}%
                </span>
              </div>
              <span className="text-[11px] text-slate-500 mt-1 block">
                {summaryDone} 人已提交 ·{' '}
                {total - summaryDone > 0 ? (
                  <span className="text-amber-700 font-semibold">{total - summaryDone} 人待补录</span>
                ) : (
                  <span className="text-emerald-700">总结全员交齐</span>
                )}
              </span>
            </div>
            <div className="relative w-12 h-12 shrink-0">
              <svg className="w-full h-full -rotate-90" viewBox="0 0 36 36">
                <circle cx="18" cy="18" r="14" fill="none" stroke="#f1f5f9" strokeWidth="3.5" />
                <circle
                  cx="18" cy="18" r="14" fill="none" stroke="#f59e0b" strokeWidth="3.5"
                  strokeDasharray="87.96"
                  strokeDashoffset={87.96 * (1 - (total ? summaryDone / total : 0))}
                  strokeLinecap="round"
                />
              </svg>
              <span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-slate-700">
                {percent(summaryDone)}%
              </span>
            </div>
          </div>
        </div>

        <div
          onClick={() => setSubTab('report')}
          className={`p-4 rounded-xl border transition-all cursor-pointer ${
            subTab === 'report'
              ? 'bg-blue-50/40 border-blue-500 shadow-sm'
              : 'bg-white border-slate-200/90 shadow-2xs hover:border-blue-300'
          }`}
        >
          <div className="flex items-start justify-between">
            <div>
              <span className="text-xs font-medium text-slate-500">③ 今日报数业务</span>
              <div className="flex items-baseline gap-1 mt-1.5">
                <span className="text-2xl font-black text-slate-900 tracking-tight">
                  {reportDone} / {total}
                </span>
                <span className="ml-2 text-[11px] font-bold text-blue-700 bg-blue-50 px-2 py-0.5 rounded-full border border-blue-200">
                  {percent(reportDone)}%
                </span>
              </div>
              <span className="text-[11px] text-slate-500 mt-1 block">
                {reportDone} 人已报数 ·{' '}
                {total - reportDone > 0 ? (
                  <span className="text-amber-700 font-semibold">{total - reportDone} 人待补录</span>
                ) : (
                  <span className="text-emerald-700">报数全员交齐</span>
                )}
              </span>
            </div>
            <div className="relative w-12 h-12 shrink-0">
              <svg className="w-full h-full -rotate-90" viewBox="0 0 36 36">
                <circle cx="18" cy="18" r="14" fill="none" stroke="#f1f5f9" strokeWidth="3.5" />
                <circle
                  cx="18" cy="18" r="14" fill="none" stroke="#3b82f6" strokeWidth="3.5"
                  strokeDasharray="87.96"
                  strokeDashoffset={87.96 * (1 - (total ? reportDone / total : 0))}
                  strokeLinecap="round"
                />
              </svg>
              <span className="absolute inset-0 flex items-center justify-center text-[10px] font-bold text-slate-700">
                {percent(reportDone)}%
              </span>
            </div>
          </div>
        </div>
      </div>

      <div className="w-full bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
        <div className="p-4 border-b border-slate-100 flex flex-col lg:flex-row items-start lg:items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h3 className="font-bold text-slate-900 text-sm">成员业务完成情况</h3>
              <span className="text-[10px] bg-slate-100 text-slate-600 px-2 py-0.5 rounded-full font-semibold">
                全员 {total} 人
              </span>
              <span className="text-[10px] bg-amber-50 text-amber-700 border border-amber-200 px-2 py-0.5 rounded-full font-semibold">
                {pendingMembers.length} 人待补录
              </span>
            </div>
            <p className="text-[11px] text-slate-500 mt-0.5">
              支持综合总览，以及打卡、总结、报数三大业务全部成员详细上报数据穿透与补录。
            </p>
          </div>

          <div className="flex flex-wrap items-center gap-1.5 p-1 bg-slate-100 rounded-xl text-xs font-medium">
            <button
              onClick={() => setSubTab('matrix')}
              className={`px-3 py-1.5 rounded-lg transition-all cursor-pointer flex items-center gap-1 ${
                subTab === 'matrix' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Layers className="w-3.5 h-3.5" />
              <span>综合总览</span>
            </button>
            <button
              onClick={() => setSubTab('checkin')}
              className={`px-3 py-1.5 rounded-lg transition-all cursor-pointer flex items-center gap-1 ${
                subTab === 'checkin' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <Clock className="w-3.5 h-3.5 text-blue-600" />
              <span>打卡业务详情</span>
            </button>
            <button
              onClick={() => setSubTab('summary')}
              className={`px-3 py-1.5 rounded-lg transition-all cursor-pointer flex items-center gap-1 ${
                subTab === 'summary' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <FileText className="w-3.5 h-3.5 text-amber-600" />
              <span>今日总结详情</span>
            </button>
            <button
              onClick={() => setSubTab('report')}
              className={`px-3 py-1.5 rounded-lg transition-all cursor-pointer flex items-center gap-1 ${
                subTab === 'report' ? 'bg-white font-bold text-blue-700 shadow-2xs' : 'text-slate-600 hover:text-slate-900'
              }`}
            >
              <BarChart3 className="w-3.5 h-3.5 text-emerald-600" />
              <span>今日报数详情</span>
            </button>
          </div>
        </div>

        <div className="p-3 bg-slate-50/70 border-b border-slate-100 flex flex-wrap items-center justify-between gap-2 text-xs">
          <div className="flex items-center gap-2 flex-1 max-w-xs">
            <div className="relative w-full">
              <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
              <input
                type="text"
                placeholder="搜索成员姓名、手机或岗位..."
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="w-full bg-white border border-slate-200 rounded-lg pl-8 pr-3 py-1.5 text-xs text-slate-800 outline-none focus:border-blue-500"
              />
            </div>
          </div>

          <div className="flex items-center gap-2">
            <div className="flex p-0.5 bg-white border border-slate-200 rounded-lg text-xs">
              <button
                onClick={() => setFilterTab('all')}
                className={`px-2.5 py-1 rounded-md transition-colors cursor-pointer ${
                  filterTab === 'all' ? 'bg-slate-100 font-bold text-slate-900' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                全部 ({members.length})
              </button>
              <button
                onClick={() => setFilterTab('need_backfill')}
                className={`px-2.5 py-1 rounded-md transition-colors cursor-pointer flex items-center gap-1 ${
                  filterTab === 'need_backfill' ? 'bg-amber-100 font-bold text-amber-800' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-amber-500"></span>
                待补录 ({pendingMembers.length})
              </button>
              <button
                onClick={() => setFilterTab('completed')}
                className={`px-2.5 py-1 rounded-md transition-colors cursor-pointer flex items-center gap-1 ${
                  filterTab === 'completed' ? 'bg-emerald-100 font-bold text-emerald-800' : 'text-slate-600 hover:text-slate-900'
                }`}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-emerald-500"></span>
                全勤达标 ({completedMembers.length})
              </button>
            </div>
          </div>
        </div>

        {loading && (
          <div className="py-16 text-center text-slate-400 text-xs">正在加载业务数据…</div>
        )}

        {!loading && members.length === 0 && (
          <div className="py-16 text-center text-slate-400 text-xs space-y-1">
            <p>该群还没有应交人员</p>
            <span className="text-[11px]">请先在「群管理 → 群成员」中添加成员</span>
          </div>
        )}

        {!loading && members.length > 0 && subTab === 'matrix' && (
          <div className="overflow-x-auto text-xs">
            <table className="w-full text-left">
              <thead>
                <tr className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                  <th className="py-2.5 px-3.5">成员 / 岗位</th>
                  {slots.map((slot) => (
                    <th key={slot.id} className="py-2.5 px-3">{slot.name}打卡</th>
                  ))}
                  <th className="py-2.5 px-3">今日总结</th>
                  <th className="py-2.5 px-3">今日报数</th>
                  <th className="py-2.5 px-3 text-right">操作</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-100 text-slate-700">
                {members
                  .filter((m) => {
                    if (!matches(m)) return false;
                    if (filterTab === 'need_backfill') return !overallDone(m);
                    if (filterTab === 'completed') return overallDone(m);
                    return true;
                  })
                  .map((m) => {
                    const done = overallDone(m);
                    return (
                      <tr key={m.user_id} className={`hover:bg-slate-50/60 transition-colors ${done ? '' : 'bg-amber-50/20'}`}>
                        <td className="py-3 px-3.5 whitespace-nowrap">{memberCell(m)}</td>
                        {slots.map((slot) => {
                          const cell = m.checkin[slot.id];
                          return (
                            <td key={slot.id} className="py-3 px-3 whitespace-nowrap">
                              {renderStatusBadge(cell?.status || 'pending', m, 'checkin', cell?.raw_status)}
                              {cell?.time && (
                                <div className="text-[10px] text-slate-400 font-mono mt-0.5">{cell.time}</div>
                              )}
                            </td>
                          );
                        })}
                        <td className="py-3 px-3 whitespace-nowrap">
                          {renderStatusBadge(m.summary_status, m, 'summary')}
                          {m.summary && (
                            <div className="text-[10px] text-slate-400 font-mono mt-0.5">
                              {m.summary.at} ({m.summary.words}字)
                            </div>
                          )}
                        </td>
                        <td className="py-3 px-3 whitespace-nowrap">
                          {renderStatusBadge(m.report_status, m, 'report')}
                          {m.report && (
                            <div className="text-[10px] text-slate-400 font-mono mt-0.5">
                              {m.report.at} ({m.report.total}项)
                            </div>
                          )}
                        </td>
                        <td className="py-3 px-3 whitespace-nowrap text-right">
                          <button
                            onClick={() => setEditTarget({ member: m, biz: 'checkin' })}
                            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 text-xs font-semibold cursor-pointer transition-colors shadow-2xs"
                            title="管理员修改并保存入库"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                            <span>编辑</span>
                          </button>
                        </td>
                      </tr>
                    );
                  })}
              </tbody>
            </table>
          </div>
        )}

        {!loading && members.length > 0 && subTab === 'checkin' && (
          <div>
            <div className="p-3.5 bg-blue-50/60 border-b border-blue-100 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-6 h-6 rounded-lg bg-blue-600 text-white flex items-center justify-center font-bold text-xs">
                  <Clock className="w-3.5 h-3.5" />
                </div>
                <div>
                  <h4 className="font-bold text-slate-900 text-xs">
                    ① 打卡业务详细完成列表 (全部用户 {checkinDone}/{total})
                  </h4>
                  <span className="text-[11px] text-slate-500">
                    包含全部成员各时段打卡时间与凭证图片，可查看大图并由管理员修改入库
                  </span>
                </div>
              </div>
              <span className="text-xs font-mono font-bold text-blue-700 bg-white px-2 py-0.5 rounded-full border border-blue-200">
                达标率 {percent(checkinDone)}%
              </span>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                  <tr>
                    <th className="py-2.5 px-3.5">全部用户</th>
                    {slots.map((slot) => (
                      <th key={slot.id} className="py-2.5 px-3">{slot.name}打卡时间</th>
                    ))}
                    <th className="py-2.5 px-3">打卡凭证图片</th>
                    <th className="py-2.5 px-3">完成状态</th>
                    <th className="py-2.5 px-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {members.filter(matches).map((m) => {
                    const proofSlot = slots.find((slot) => m.checkin[slot.id]?.image_ref);
                    return (
                      <tr key={m.user_id} className="hover:bg-slate-50/60 transition-colors">
                        <td className="py-3 px-3.5 whitespace-nowrap">{memberCell(m)}</td>
                        {slots.map((slot) => {
                          const cell = m.checkin[slot.id];
                          return (
                            <td key={slot.id} className="py-3 px-3 whitespace-nowrap">
                              <div className="font-mono font-bold text-slate-800">
                                {cell?.time || <span className="text-slate-400 font-normal">未打卡</span>}
                              </div>
                            </td>
                          );
                        })}
                        <td className="py-3 px-3 whitespace-nowrap">
                          {proofSlot ? (
                            <ProofThumb
                              refId={String(m.checkin[proofSlot.id]!.image_ref)}
                              onOpen={() => openCheckinImage(m, proofSlot.id, proofSlot.name)}
                            />
                          ) : (
                            <span className="text-slate-400 text-[11px]">暂无图片</span>
                          )}
                        </td>
                        <td className="py-3 px-3 whitespace-nowrap">
                          {statusCell(m.checkin_status, m, 'checkin')}
                        </td>
                        <td className="py-3 px-3 whitespace-nowrap text-right">
                          <button
                            onClick={() => setEditTarget({ member: m, biz: 'checkin' })}
                            className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 text-xs font-semibold cursor-pointer transition-colors shadow-2xs"
                            title="管理员修改并保存入库"
                          >
                            <Pencil className="w-3.5 h-3.5" />
                            <span>编辑</span>
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!loading && members.length > 0 && subTab === 'summary' && (
          <div>
            <div className="p-3.5 bg-amber-50/60 border-b border-amber-100 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-6 h-6 rounded-lg bg-amber-500 text-white flex items-center justify-center font-bold text-xs">
                  <FileText className="w-3.5 h-3.5" />
                </div>
                <div>
                  <h4 className="font-bold text-slate-900 text-xs">
                    ② 今日总结业务详细完成列表 (全部用户 {summaryDone}/{total})
                  </h4>
                  <span className="text-[11px] text-slate-500">
                    包含全量用户今日工作总结汇报、字数统计与合规状态
                  </span>
                </div>
              </div>
              <span className="text-xs font-mono font-bold text-amber-800 bg-white px-2 py-0.5 rounded-full border border-amber-200">
                达标率 {percent(summaryDone)}%
              </span>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                  <tr>
                    <th className="py-2.5 px-3.5">全部用户</th>
                    <th className="py-2.5 px-3">完成提交时间</th>
                    <th className="py-2.5 px-3">完成的总结内容 / 汇报信息</th>
                    <th className="py-2.5 px-3">附件图片</th>
                    <th className="py-2.5 px-3">状态</th>
                    <th className="py-2.5 px-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {members.filter(matches).map((m) => (
                    <tr key={m.user_id} className="hover:bg-slate-50/60 transition-colors">
                      <td className="py-3 px-3.5 whitespace-nowrap">{memberCell(m)}</td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <div className="font-mono font-bold text-slate-800">
                          {m.summary?.at || <span className="text-amber-700 font-normal">未提交 (待补录)</span>}
                        </div>
                      </td>
                      <td className="py-3 px-3 max-w-sm">
                        {m.summary ? (
                          <button
                            onClick={() => setSummaryDetail(m)}
                            className="w-full text-left text-slate-700 hover:text-blue-600 cursor-pointer transition-colors group flex items-center justify-between gap-2"
                            title="点击查看工作总结全文"
                          >
                            <span className="truncate">{m.summary.content || '（正文为空）'}</span>
                            <span className="shrink-0 text-[10px] text-blue-600 bg-blue-50 px-1.5 py-0.5 rounded opacity-0 group-hover:opacity-100 transition-opacity font-medium">
                              查看全文
                            </span>
                          </button>
                        ) : (
                          <span className="text-slate-400">—</span>
                        )}
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <span className="text-slate-400 text-[11px]">暂无图片</span>
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        {statusCell(m.summary_status, m, 'summary')}
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap text-right">
                        <button
                          onClick={() => setEditTarget({ member: m, biz: 'summary' })}
                          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 text-xs font-semibold cursor-pointer transition-colors shadow-2xs"
                          title="管理员修改并保存入库"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                          <span>编辑</span>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        {!loading && members.length > 0 && subTab === 'report' && (
          <div>
            <div className="p-3.5 bg-emerald-50/60 border-b border-emerald-100 flex items-center justify-between">
              <div className="flex items-center gap-2">
                <div className="w-6 h-6 rounded-lg bg-emerald-600 text-white flex items-center justify-center font-bold text-xs">
                  <BarChart3 className="w-3.5 h-3.5" />
                </div>
                <div>
                  <h4 className="font-bold text-slate-900 text-xs">
                    ③ 今日报数业务详细完成列表 (全部用户 {reportDone}/{total})
                  </h4>
                  <span className="text-[11px] text-slate-500">
                    包含全量用户字段池各量化指标完成情况
                  </span>
                </div>
              </div>
              <span className="text-xs font-mono font-bold text-emerald-800 bg-white px-2 py-0.5 rounded-full border border-emerald-200">
                达标率 {percent(reportDone)}%
              </span>
            </div>

            <div className="overflow-x-auto text-xs">
              <table className="w-full text-left">
                <thead className="bg-slate-50 text-slate-500 border-b border-slate-200/80 font-medium">
                  <tr>
                    <th className="py-2.5 px-3.5">全部用户</th>
                    <th className="py-2.5 px-3">完成报数时间</th>
                    <th className="py-2.5 px-3">完成的信息 / 量化报数数据</th>
                    <th className="py-2.5 px-3">指标汇总</th>
                    <th className="py-2.5 px-3">凭证图片</th>
                    <th className="py-2.5 px-3">状态</th>
                    <th className="py-2.5 px-3 text-right">操作</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100 text-slate-700">
                  {members.filter(matches).map((m) => (
                    <tr key={m.user_id} className="hover:bg-slate-50/60 transition-colors">
                      <td className="py-3 px-3.5 whitespace-nowrap">{memberCell(m)}</td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <div className="font-mono font-bold text-slate-800">
                          {m.report?.at || <span className="text-amber-700 font-normal">未报数 (待补录)</span>}
                        </div>
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <div className="flex flex-wrap items-center gap-1.5">
                          {(m.report?.items || []).length === 0 ? (
                            <span className="text-slate-400">—</span>
                          ) : (
                            m.report?.items.map((item) => (
                              <span
                                key={item.field_id}
                                className="px-2 py-0.5 rounded bg-slate-100 border border-slate-200 text-slate-700 font-mono text-[10px]"
                              >
                                {item.field_name}: <strong className="text-blue-700">{item.quantity}</strong>{item.unit}
                              </span>
                            ))
                          )}
                        </div>
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <span className="font-mono text-emerald-700 font-bold text-sm">
                          {m.report?.total || 0} 项
                        </span>
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        <span className="text-slate-400 text-[11px]">暂无图片</span>
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap">
                        {statusCell(m.report_status, m, 'report')}
                      </td>
                      <td className="py-3 px-3 whitespace-nowrap text-right">
                        <button
                          onClick={() => setEditTarget({ member: m, biz: 'report' })}
                          className="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg bg-blue-50 hover:bg-blue-100 text-blue-700 border border-blue-200 text-xs font-semibold cursor-pointer transition-colors shadow-2xs"
                          title="管理员修改并保存入库"
                        >
                          <Pencil className="w-3.5 h-3.5" />
                          <span>编辑</span>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div className="bg-slate-50/70 px-4 py-2.5 border-t border-slate-100 flex flex-col sm:flex-row items-start sm:items-center justify-between text-[11px] text-slate-500 gap-1">
          <span>
            提示：仅「待补录」状态支持点击直接跳转并打开补录窗口；支持点击总结文本与凭证图片查看大图全文，右侧编辑按钮支持管理员修改入库。
          </span>
          <span className="font-mono">
            打卡 {checkinDone}/{total} · 总结 {summaryDone}/{total} · 报数 {reportDone}/{total}
          </span>
        </div>
      </div>

      <div className="w-full bg-white rounded-2xl border border-slate-200/90 p-5 shadow-2xs space-y-4">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-3">
          <div className="flex items-center gap-2.5">
            <div className="w-8 h-8 rounded-xl bg-amber-50 border border-amber-200 text-amber-700 flex items-center justify-center font-bold">
              <Clock className="w-4 h-4 text-amber-600" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <h4 className="font-bold text-sm text-slate-900">今日待补录异常名单</h4>
                <span className="text-[11px] font-bold text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full border border-amber-200">
                  {pendingMembers.length} 位成员待补录
                </span>
              </div>
              <p className="text-xs text-slate-500 mt-0.5">
                汇总当前群聊中打卡缺卡、总结未交或报数未提交的成员，点击对应卡片即可快捷代录。
              </p>
            </div>
          </div>

          <button
            onClick={handleRemindPending}
            disabled={reminding || pendingMembers.length === 0}
            className={`inline-flex items-center gap-1.5 px-3.5 py-2 rounded-xl text-xs font-bold transition-all shadow-2xs cursor-pointer shrink-0 ${
              pendingMembers.length > 0
                ? 'bg-amber-500 hover:bg-amber-600 text-white'
                : 'bg-slate-100 text-slate-400 cursor-not-allowed'
            }`}
            title="向今日未完成业务的成员发送群内提醒与催办消息"
          >
            <BellRing className="w-4 h-4" />
            <span>{reminding ? '正在提醒…' : '提醒业务'}</span>
            {pendingMembers.length > 0 && (
              <span className="ml-1 px-1.5 py-0.5 bg-amber-600 text-white text-[10px] rounded-full">
                {pendingMembers.length}人待催
              </span>
            )}
          </button>
        </div>

        {pendingMembers.length === 0 ? (
          <div className="py-8 text-center text-slate-400 bg-slate-50/70 rounded-2xl border border-dashed border-slate-200 space-y-1.5">
            <CheckCircle2 className="w-8 h-8 text-emerald-500 mx-auto" />
            <div className="font-bold text-slate-700 text-sm">今日全员皆已达标</div>
            <p className="text-xs text-slate-400">当前群聊今日三项业务均已上报完成，无待补录异常人员。</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
            {pendingMembers.map((m) => {
              const missingCheckin = slots.some((slot) => !m.checkin[slot.id]?.time);
              return (
                <div
                  key={m.user_id}
                  className="p-3.5 rounded-xl bg-slate-50/80 hover:bg-slate-50 border border-slate-200/90 transition-all flex flex-col justify-between gap-3"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex items-center gap-2.5">
                      <div className={`w-8 h-8 rounded-full text-white font-bold text-xs flex items-center justify-center shrink-0 ${avatarColor(m.user_id)}`}>
                        {(m.nickname || m.user_id).slice(0, 1)}
                      </div>
                      <div>
                        <div className="flex items-center gap-1.5">
                          <span className="font-bold text-slate-900 text-xs">
                            {m.nickname || m.user_id}
                          </span>
                          <span className="text-[10px] text-slate-500 bg-white px-1.5 py-0.2 rounded border border-slate-200">
                            {m.role || '成员'}
                          </span>
                        </div>
                        <div className="text-[10px] text-slate-400 font-mono mt-0.5">{m.user_id}</div>
                      </div>
                    </div>

                    <button
                      onClick={() => openBackfill(m)}
                      className="px-2.5 py-1 bg-amber-500 hover:bg-amber-600 text-white rounded-lg text-xs font-bold shadow-2xs transition-colors cursor-pointer shrink-0"
                    >
                      去补录
                    </button>
                  </div>

                  <div className="flex flex-wrap items-center gap-1.5 text-[11px] pt-1 border-t border-slate-200/60">
                    <span className="text-slate-400 text-[10px]">待办项：</span>
                    {missingCheckin && (
                      <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium">缺打卡</span>
                    )}
                    {!m.summary && (
                      <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium">缺今日总结</span>
                    )}
                    {!m.report && (
                      <span className="px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 font-medium">缺今日报数</span>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {board && (
        <QuickBackfillModal
          isOpen={modalOpen}
          onClose={() => setModalOpen(false)}
          groupId={selectedGroupId}
          defaultDate={date}
          periodType={board.period_type}
          members={members}
          fields={fields}
          slots={slots}
          target={backfillTarget}
          onDone={(message) => {
            showToast(message);
            load();
          }}
        />
      )}

      <ImagePreviewModal
        data={imagePreview}
        onClose={() => setImagePreview(null)}
      />

      <SummaryDetailModal
        member={summaryDetail}
        onClose={() => setSummaryDetail(null)}
        onOpenEdit={(member) => {
          setSummaryDetail(null);
          setEditTarget({ member, biz: 'summary' });
        }}
      />

      {editTarget && selectedGroupId && (
        <AdminRecordEditModal
          isOpen
          onClose={() => setEditTarget(null)}
          groupId={selectedGroupId}
          date={date}
          member={editTarget.member}
          slots={slots}
          initialBiz={editTarget.biz}
          onSaved={(message) => {
            showToast(message);
            load();
          }}
        />
      )}

      {addMemberTarget && selectedGroupId && (
        <AddMemberModal
          isOpen
          groupId={selectedGroupId}
          userId={addMemberTarget.user_id}
          defaultName={addMemberTarget.nickname || ''}
          defaultRole={addMemberTarget.role || ''}
          onClose={() => setAddMemberTarget(null)}
          onAdded={(message) => {
            showToast(message);
            load();
          }}
        />
      )}
    </div>
  );
};
