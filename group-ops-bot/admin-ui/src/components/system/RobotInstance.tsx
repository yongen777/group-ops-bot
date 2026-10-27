import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Link2,
  CheckCircle2,
  AlertTriangle,
  Copy,
  Check,
  Info,
  X,
  Trash2,
  HelpCircle,
} from 'lucide-react';
import { api, gid, plainError } from '../../lib/api';
import { useApp } from '../../lib/store';
import type { RobotInstance as RobotInstanceRow } from '../../lib/types';

type StatusFilter = 'all' | 'ACTIVE' | 'WAIT_BIND' | 'DISABLED';

function fmtTime(value: string): string {
  if (!value) return '—';
  const text = String(value).replace('T', ' ').replace('Z', '');
  return text.slice(0, 16);
}

/**
 * 群显示名优先级：后台备注名 -> 群配置群名称 -> 群 ID。
 *
 * ⭐ 2026-10-20 修「群内录入了群名，后台却显示群 ID」：群内两轮绑定只把
 * 群名写进 `group_config.group_name`，实例行的 `display_name`（后台备注名）
 * 仍是空 —— 原来这里直接回退 group_id，就显示成一串数字。
 */
function groupNameOf(row: {
  display_name?: string | null;
  group_name?: string | null;
  group_id: string;
}): string {
  return row.display_name || row.group_name || row.group_id;
}

/**
 * 推送地址脱敏（列表页一眼扫过时不暴露平台 key）。
 *
 * ⭐ D30 加固二：平台每条回调都自带该群的「回推地址」，后台把它记下来当
 * 待绑定行的候选值。列表里只显示 `key=***` 的形态，完整地址只在
 * 「确认绑定」弹窗内、由管理员点「填入」时才展示与使用。
 */
function maskCallbackUrl(url?: string | null): string {
  if (!url) return '';
  return url.replace(/(key=)[^&]+/i, '$1***');
}

export const RobotInstance: React.FC = () => {
  const { refreshGroups } = useApp();
  const [instances, setInstances] = useState<RobotInstanceRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all');
  const [toast, setToast] = useState<{ text: string; type: 'success' | 'error' } | null>(null);

  const [editing, setEditing] = useState<RobotInstanceRow | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<RobotInstanceRow | null>(null);
  const [confirmBindTarget, setConfirmBindTarget] = useState<RobotInstanceRow | null>(null);
  const [bindName, setBindName] = useState('');
  const [bindUrl, setBindUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [copiedKey, setCopiedKey] = useState<string | null>(null);

  const showToast = useCallback((text: string, type: 'success' | 'error' = 'success') => {
    setToast({ text, type });
    window.setTimeout(() => setToast(null), 4000);
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.get<{ ok: boolean; instances: RobotInstanceRow[] }>(
        '/api/admin/robot-instances',
      );
      setInstances(data.instances || []);
    } catch (err) {
      showToast(plainError(err instanceof Error ? err.message : ''), 'error');
    } finally {
      setLoading(false);
    }
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  const filtered = useMemo(() => {
    if (statusFilter === 'all') return instances;
    return instances.filter((row) => row.status === statusFilter);
  }, [instances, statusFilter]);

  const counts = useMemo(
    () => ({
      all: instances.length,
      ACTIVE: instances.filter((i) => i.status === 'ACTIVE').length,
      WAIT_BIND: instances.filter((i) => i.status === 'WAIT_BIND').length,
      DISABLED: instances.filter((i) => i.status === 'DISABLED').length,
    }),
    [instances],
  );

  /** 停用 / 启用（后端守卫：启用前必须有推送地址） */
  const toggleStatus = async (row: RobotInstanceRow) => {
    setBusy(true);
    try {
      const path =
        row.status === 'ACTIVE'
          ? `/api/admin/robot-instances/${gid(row.group_id)}/disable`
          : `/api/admin/robot-instances/${gid(row.group_id)}/enable`;
      await api.post(path);
      showToast(
        `群聊【${groupNameOf(row)}】状态已变更为 ${
          row.status === 'ACTIVE' ? '「已停用」' : '「已激活」'
        }`,
      );
      await load();
    } catch (err) {
      showToast(plainError(err instanceof Error ? err.message : ''), 'error');
    } finally {
      setBusy(false);
    }
  };

  const saveEdit = async () => {
    if (!editing) return;
    const name = editing.display_name.trim();
    if (!name) {
      showToast('群备注名不能为空', 'error');
      return;
    }
    const url = editing.webhook_base_url.trim();
    if (!url) {
      showToast('推送地址不能为空', 'error');
      return;
    }
    setBusy(true);
    try {
      await api.put(`/api/admin/robot-instances/${gid(editing.group_id)}`, {
        display_name: name,
        webhook_base_url: url,
      });
      showToast(`群聊【${name}】配置已成功保存`);
      setEditing(null);
      await load();
      await refreshGroups();
    } catch (err) {
      showToast(plainError(err instanceof Error ? err.message : ''), 'error');
    } finally {
      setBusy(false);
    }
  };

  const openConfirmBind = (row: RobotInstanceRow) => {
    setConfirmBindTarget(row);
    // 群内绑定已录过群名 -> 直接预填，管理员不必重打一遍
    setBindName(row.display_name || row.group_name || '');
    setBindUrl(row.webhook_base_url || '');
  };

  /** 待绑定 -> 已激活（后端要求推送地址；备注名可改） */
  const submitConfirmBind = async () => {
    if (!confirmBindTarget) return;
    const name = bindName.trim();
    const url = bindUrl.trim();
    if (!name) {
      showToast('请填写群备注名', 'error');
      return;
    }
    if (!url) {
      showToast('请填写该群的 Webhook 推送地址', 'error');
      return;
    }
    setBusy(true);
    try {
      await api.post(`/api/admin/robot-instances/${gid(confirmBindTarget.group_id)}/bind`, {
        display_name: name,
        webhook_base_url: url,
      });
      showToast(`群聊【${name}】已成功绑定并正式激活`);
      setConfirmBindTarget(null);
      await load();
      await refreshGroups();
    } catch (err) {
      showToast(plainError(err instanceof Error ? err.message : ''), 'error');
    } finally {
      setBusy(false);
    }
  };

  /** 移除群聊：实例 + 群配置 + 名单 + 授权 + 字段全删（业务历史记录保留） */
  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await api.del(`/api/admin/robot-instances/${gid(deleteTarget.group_id)}`);
      showToast(`已成功移除群聊【${groupNameOf(deleteTarget)}】`);
      setDeleteTarget(null);
      await load();
      await refreshGroups();
    } catch (err) {
      showToast(plainError(err instanceof Error ? err.message : ''), 'error');
    } finally {
      setBusy(false);
    }
  };

  const copyText = (text: string) => {
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => undefined);
    }
    setCopiedKey(text);
    window.setTimeout(() => setCopiedKey(null), 2000);
  };

  return (
    <div className="space-y-5">
      {/* 顶部标题栏 */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 bg-white p-4 rounded-2xl border border-slate-200/90 shadow-2xs">
        <div>
          <h2 className="text-lg font-black text-slate-900 tracking-tight flex items-center gap-2">
            <Link2 className="w-5 h-5 text-blue-600" />
            <span>群实例绑定</span>
          </h2>
          <p className="text-xs text-slate-500 mt-0.5">
            管理已接入本系统的群聊与机器人实例，支持修改推送地址、快速绑定与移除群聊。
          </p>
        </div>

        <div className="flex items-center gap-2">
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as StatusFilter)}
            className="text-xs bg-slate-50 border border-slate-200 rounded-xl px-3 py-1.5 outline-none text-slate-700 cursor-pointer font-medium"
          >
            <option value="all">全部状态 ({counts.all})</option>
            <option value="ACTIVE">已激活 ({counts.ACTIVE})</option>
            <option value="WAIT_BIND">待绑定 ({counts.WAIT_BIND})</option>
            <option value="DISABLED">已停用 ({counts.DISABLED})</option>
          </select>
        </div>
      </div>

      {toast && (
        <div
          className={`p-3 rounded-xl text-xs flex items-center gap-2 animate-in fade-in border ${
            toast.type === 'success'
              ? 'bg-blue-50 border-blue-200 text-blue-800'
              : 'bg-rose-50 border-rose-200 text-rose-700'
          }`}
        >
          {toast.type === 'success' ? (
            <CheckCircle2 className="w-4 h-4 text-blue-600 shrink-0" />
          ) : (
            <AlertTriangle className="w-4 h-4 text-rose-600 shrink-0" />
          )}
          <span className="font-medium">{toast.text}</span>
        </div>
      )}

      {/* 绑定流程说明：五步 */}
      <div className="bg-white border border-slate-200/90 p-4 rounded-2xl text-xs text-slate-700 shadow-2xs space-y-2">
        <div className="flex items-center gap-2 font-bold text-slate-900 text-sm">
          <Info className="w-4 h-4 text-blue-600" />
          <span>群机器人绑定流程说明</span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-5 gap-2.5 pt-1">
          <div className="p-2.5 rounded-xl bg-slate-50 border border-slate-100 space-y-1">
            <span className="w-5 h-5 rounded-full bg-blue-100 text-blue-700 font-bold text-[10px] flex items-center justify-center">
              1
            </span>
            <div className="font-semibold text-slate-800 text-[11px]">创建群聊</div>
            <p className="text-[10px] text-slate-500">在量子密信等软件中新建工作或业务群聊。</p>
          </div>

          <div className="p-2.5 rounded-xl bg-slate-50 border border-slate-100 space-y-1">
            <span className="w-5 h-5 rounded-full bg-blue-100 text-blue-700 font-bold text-[10px] flex items-center justify-center">
              2
            </span>
            <div className="font-semibold text-slate-800 text-[11px]">添加机器人</div>
            <p className="text-[10px] text-slate-500">在群设置中添加机器人，填入后台回调地址。</p>
          </div>

          <div className="p-2.5 rounded-xl bg-slate-50 border border-slate-100 space-y-1">
            <span className="w-5 h-5 rounded-full bg-blue-100 text-blue-700 font-bold text-[10px] flex items-center justify-center">
              3
            </span>
            <div className="font-semibold text-slate-800 text-[11px]">获得 Webhook</div>
            <p className="text-[10px] text-slate-500">机器人添加成功后，获取该群的 Webhook 地址。</p>
          </div>

          <div className="p-2.5 rounded-xl bg-blue-50/70 border border-blue-200 space-y-1">
            <span className="w-5 h-5 rounded-full bg-blue-600 text-white font-bold text-[10px] flex items-center justify-center">
              4
            </span>
            <div className="font-bold text-blue-900 text-[11px]">群内发送绑定口令</div>
            <p className="text-[10px] text-blue-700">
              在群里@机器人只能发送【绑定】指令方可激活并实现该功能（发送其他内容无法触发绑定）。
            </p>
          </div>

          <div className="p-2.5 rounded-xl bg-emerald-50 border border-emerald-200 space-y-1">
            <span className="w-5 h-5 rounded-full bg-emerald-600 text-white font-bold text-[10px] flex items-center justify-center">
              5
            </span>
            <div className="font-bold text-emerald-900 text-[11px]">后台确认绑定</div>
            <p className="text-[10px] text-emerald-700">
              管理员在下方点击确认绑定并填写群备注名，正式启用。
            </p>
          </div>
        </div>
      </div>

      {/* 实例列表 */}
      <div className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs overflow-hidden">
        <div className="p-4 border-b border-slate-100 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h3 className="font-bold text-slate-900 text-sm">群聊实例列表</h3>
            <span className="text-xs text-slate-400">共 {filtered.length} 个群聊</span>
          </div>
          <span className="text-[11px] text-slate-400">
            支持修改群备注、更新推送地址与移除群聊
          </span>
        </div>

        <div className="overflow-x-auto text-xs">
          <table className="w-full text-left">
            <thead>
              <tr className="bg-slate-50/70 text-slate-500 border-b border-slate-200 font-medium">
                <th className="py-3 px-4">群聊 ID (GROUP_ID)</th>
                <th className="py-3 px-3">群备注名</th>
                <th className="py-3 px-3">当前状态</th>
                <th className="py-3 px-4">Webhook 推送地址</th>
                <th className="py-3 px-3">更新时间</th>
                <th className="py-3 px-4 text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 text-slate-700">
              {loading ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-slate-400">
                    正在加载群聊实例…
                  </td>
                </tr>
              ) : filtered.length === 0 ? (
                <tr>
                  <td colSpan={6} className="py-12 text-center text-slate-400">
                    {instances.length === 0
                      ? '暂未接入任何群聊。在群内 @机器人 发送「绑定」即可生成待绑定记录。'
                      : '当前筛选条件下没有群聊'}
                  </td>
                </tr>
              ) : (
                filtered.map((row) => {
                  const isActive = row.status === 'ACTIVE';
                  const isWait = row.status === 'WAIT_BIND';
                  return (
                    <tr key={row.group_id} className="hover:bg-slate-50/80 transition-colors">
                      <td className="py-3 px-4 font-mono text-[11px] font-semibold text-slate-900 whitespace-nowrap">
                        {row.group_id}
                      </td>

                      <td className="py-3 px-3 font-semibold text-slate-800 whitespace-nowrap">
                        {groupNameOf(row)}
                        {isWait && (
                          <span className="ml-1.5 px-1.5 py-0.2 rounded bg-amber-100 text-amber-800 text-[10px] font-normal">
                            新群接入
                          </span>
                        )}
                      </td>

                      <td className="py-3 px-3 whitespace-nowrap">
                        <span
                          className={`px-2.5 py-0.5 rounded-full text-[11px] font-bold border ${
                            isActive
                              ? 'bg-emerald-50 text-emerald-700 border-emerald-300'
                              : isWait
                                ? 'bg-amber-50 text-amber-800 border-amber-300 animate-pulse'
                                : 'bg-slate-100 text-slate-600 border-slate-300'
                          }`}
                        >
                          {isActive ? '已激活' : isWait ? '待绑定' : '已停用'}
                        </span>
                      </td>

                      <td
                        className="py-3 px-4 max-w-sm font-mono text-[11px] text-slate-600 truncate"
                        title={row.webhook_base_url || ''}
                      >
                        {row.webhook_base_url ? (
                          <div className="flex items-center gap-1.5">
                            <span className="truncate">{row.webhook_base_url}</span>
                            <button
                              onClick={() => copyText(row.webhook_base_url)}
                              className="p-1 text-slate-400 hover:text-blue-600 shrink-0 cursor-pointer"
                              title="复制推送地址"
                            >
                              {copiedKey === row.webhook_base_url ? (
                                <Check className="w-3.5 h-3.5 text-emerald-600" />
                              ) : (
                                <Copy className="w-3.5 h-3.5" />
                              )}
                            </button>
                          </div>
                        ) : row.last_callback_url ? (
                          <div className="space-y-0.5">
                            <span className="text-slate-400 font-normal">（暂无，待绑定配置）</span>
                            <div
                              className="text-[10px] text-amber-700 truncate max-w-[220px]"
                              title="平台最近一次回调带回的地址，点「确认绑定」可一键填入"
                            >
                              最近回调带回：{maskCallbackUrl(row.last_callback_url)}
                            </div>
                          </div>
                        ) : (
                          <span className="text-slate-400 font-normal">（暂无，待绑定配置）</span>
                        )}
                      </td>

                      <td className="py-3 px-3 font-mono text-[11px] text-slate-400 whitespace-nowrap">
                        {fmtTime(row.updated_at)}
                      </td>

                      <td className="py-3 px-4 text-right whitespace-nowrap">
                        <div className="flex items-center justify-end gap-1.5">
                          {isWait && (
                            <button
                              onClick={() => openConfirmBind(row)}
                              className="px-2.5 py-1 bg-amber-500 hover:bg-amber-600 text-white rounded text-[11px] font-bold transition-colors cursor-pointer shadow-2xs"
                            >
                              确认绑定
                            </button>
                          )}

                          {/* ⭐ 2026-10-20 用户指令：待绑定状态只留「确认绑定」一个
                              绑定入口 —— 「修改」弹的是同一组字段（备注名 + 推送地址），
                              两个按钮做重叠的事，容易让人以为要先后点两次。
                              待绑定阶段要改名/填地址，直接在「确认绑定」里改即可。 */}
                          {!isWait && (
                            <button
                              onClick={() => setEditing({ ...row })}
                              className="px-2 py-1 text-blue-600 hover:text-blue-700 border border-blue-200 hover:bg-blue-50 rounded text-[11px] font-medium transition-colors cursor-pointer"
                            >
                              修改
                            </button>
                          )}

                          {!isWait && (
                            <button
                              onClick={() => toggleStatus(row)}
                              disabled={busy}
                              className={`px-2 py-1 rounded text-[11px] font-medium border transition-colors cursor-pointer disabled:opacity-50 ${
                                isActive
                                  ? 'text-amber-700 hover:bg-amber-50 border-amber-200'
                                  : 'text-emerald-700 hover:bg-emerald-50 border-emerald-200'
                              }`}
                            >
                              {isActive ? '停用' : '启用'}
                            </button>
                          )}

                          <button
                            onClick={() => setDeleteTarget(row)}
                            className="px-2 py-1 text-rose-600 hover:text-rose-700 border border-rose-200 hover:bg-rose-50 rounded text-[11px] font-medium transition-colors cursor-pointer flex items-center gap-1"
                            title="移除此群聊绑定"
                          >
                            <Trash2 className="w-3 h-3" />
                            <span>移除群聊</span>
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* 弹窗：确认绑定 */}
      {confirmBindTarget && (
        <div className="fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <h3 className="font-bold text-slate-900 text-base flex items-center gap-2">
                <CheckCircle2 className="w-5 h-5 text-emerald-600" />
                <span>确认绑定群聊</span>
              </h3>
              <button
                onClick={() => setConfirmBindTarget(null)}
                className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <p className="text-xs text-slate-600 leading-relaxed">
              该群已在群聊内首次 @机器人 成功触发。请为该群设置一个方便识别的备注名称，
              并填写机器人推送地址，确认后将正式激活群运维监控。
            </p>

            <div className="space-y-3 text-xs">
              <div>
                <label className="font-semibold text-slate-700 block mb-1">群 ID</label>
                <div className="p-2.5 bg-slate-100 rounded-xl font-mono text-slate-600">
                  {confirmBindTarget.group_id}
                </div>
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">群备注名</label>
                <input
                  type="text"
                  value={bindName}
                  onChange={(e) => setBindName(e.target.value)}
                  placeholder="例如：华东业务运维沟通群"
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white text-slate-800 font-medium"
                />
              </div>

              {confirmBindTarget.last_callback_url && (
                <div className="p-2.5 rounded-xl bg-amber-50 border border-amber-200 space-y-1.5">
                  <p className="text-[10px] font-bold text-amber-800">
                    平台最近一次回调带回的地址（只读参考）
                  </p>
                  <p className="font-mono text-[10px] text-amber-900 break-all">
                    {maskCallbackUrl(confirmBindTarget.last_callback_url)}
                  </p>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] text-amber-700 leading-relaxed">
                      来自该群真实消息，通常就是它的推送地址；仍需你确认后才会启用
                    </span>
                    <button
                      onClick={() => setBindUrl(confirmBindTarget.last_callback_url || '')}
                      className="shrink-0 px-2 py-1 bg-amber-500 hover:bg-amber-600 text-white rounded text-[10px] font-bold cursor-pointer"
                    >
                      填入此地址
                    </button>
                  </div>
                </div>
              )}

              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  Webhook 推送地址
                </label>
                <input
                  type="text"
                  value={bindUrl}
                  onChange={(e) => setBindUrl(e.target.value)}
                  placeholder="https://..."
                  className="w-full font-mono bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white text-[11px]"
                />
                <span className="text-[10px] text-slate-400 mt-1 block">
                  机器人向该群发送通知的消息通道地址，绑定后才会正式激活。
                </span>
              </div>
            </div>

            <div className="pt-2 flex items-center justify-end gap-2">
              <button
                onClick={() => setConfirmBindTarget(null)}
                className="px-4 py-2 rounded-xl text-xs text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={submitConfirmBind}
                disabled={busy}
                className="px-4 py-2 bg-emerald-600 hover:bg-emerald-700 text-white font-bold text-xs rounded-xl shadow-xs cursor-pointer disabled:opacity-60"
              >
                确认绑定并激活
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 弹窗：修改群实例 */}
      {editing && (
        <div className="fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-lg w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <h3 className="font-bold text-slate-900 text-base">修改群实例配置</h3>
              <button
                onClick={() => setEditing(null)}
                className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="space-y-3 text-xs">
              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  群聊 ID (系统自动分配，不可修改)
                </label>
                <input
                  type="text"
                  disabled
                  value={editing.group_id}
                  className="w-full bg-slate-100 border border-slate-200 rounded-xl p-2.5 font-mono text-slate-500"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">群备注名称</label>
                <input
                  type="text"
                  value={editing.display_name}
                  onChange={(e) => setEditing({ ...editing, display_name: e.target.value })}
                  className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
                />
              </div>

              <div>
                <label className="font-semibold text-slate-700 block mb-1">
                  Webhook 推送地址
                </label>
                <input
                  type="text"
                  value={editing.webhook_base_url}
                  onChange={(e) => setEditing({ ...editing, webhook_base_url: e.target.value })}
                  placeholder="https://..."
                  className="w-full font-mono bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white text-[11px]"
                />
                <span className="text-[10px] text-slate-400 mt-1 block">
                  机器人在该群发送通知的消息通道，请确保地址完整有效。
                </span>
              </div>
            </div>

            <div className="pt-2 flex items-center justify-end gap-2">
              <button
                onClick={() => setEditing(null)}
                className="px-4 py-2 rounded-xl text-xs text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={saveEdit}
                disabled={busy}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white font-medium text-xs rounded-xl shadow-xs cursor-pointer disabled:opacity-60"
              >
                保存变更
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 弹窗：确认移除群聊 */}
      {deleteTarget && (
        <div className="fixed inset-0 z-50 bg-slate-900/40 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center gap-3 border-b border-slate-100 pb-3">
              <div className="w-10 h-10 rounded-xl bg-rose-50 border border-rose-200 text-rose-600 flex items-center justify-center shrink-0">
                <Trash2 className="w-5 h-5" />
              </div>
              <div>
                <h3 className="font-bold text-slate-900 text-base">确认移除群聊</h3>
                <p className="text-[11px] text-slate-400">解除该群机器人实例绑定关系</p>
              </div>
            </div>

            <div className="space-y-2 text-xs text-slate-600">
              <p>
                您即将从系统中移除群聊{' '}
                <strong className="text-slate-900">
                  【{groupNameOf(deleteTarget)}】
                </strong>
              </p>
              <p className="font-mono text-[11px] text-slate-500 break-all">
                ID: {deleteTarget.group_id}
              </p>
              <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-amber-800 text-[11px] space-y-1">
                <p className="font-semibold">该群的配置将一并删除：</p>
                <p>群设置与业务开关、成员名单、授权管理员、报数字段池、提醒计划。</p>
                <p className="text-emerald-800">
                  已有的打卡、总结、报数历史记录不会删除；如需重新接入，让机器人在群内再次
                  @ 发送「绑定」即可。
                </p>
              </div>
            </div>

            <div className="pt-2 flex items-center justify-end gap-2">
              <button
                onClick={() => setDeleteTarget(null)}
                className="px-4 py-2 rounded-xl text-xs text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={confirmDelete}
                disabled={busy}
                className="px-4 py-2 bg-rose-600 hover:bg-rose-700 text-white font-bold text-xs rounded-xl shadow-xs cursor-pointer disabled:opacity-60"
              >
                确认移除群聊
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="flex items-center gap-1.5 text-[11px] text-slate-400 px-1">
        <HelpCircle className="w-3.5 h-3.5" />
        <span>
          停用后该群将无法执行任何指令（打卡 / 总结 / 报数 / 导出），也不再接收提醒推送；移除群聊则会清空该群的全部配置。
        </span>
      </div>
    </div>
  );
};
