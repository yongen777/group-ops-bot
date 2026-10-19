import React, { useEffect, useState } from 'react';
import { AlertTriangle, UserPlus, X } from 'lucide-react';
import { api, gid } from '../../lib/api';
import { ROLE_LABEL, ROLE_OPTIONS } from '../../lib/roles';

interface Props {
  isOpen: boolean;
  groupId: string;
  userId: string;
  defaultRole?: string;
  onClose: () => void;
  onAdded: (message: string) => void;
}

/**
 * 一键添加该成员（名单外成员完成了业务时出现）。
 *
 * 口径：只往本群名单写「姓名 + 岗位角色」，⛔ 不改业务记录、⛔ 不建管理权限。
 * 2026-10-19 用户指令：姓名**默认留空**（⛔ 不拿手机号占位），
 * 岗位角色改为与群管理同一套下拉选项。
 */
export const AddMemberModal: React.FC<Props> = ({
  isOpen,
  groupId,
  userId,
  defaultRole,
  onClose,
  onAdded,
}) => {
  const [nickname, setNickname] = useState('');
  const [role, setRole] = useState(defaultRole || '');
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    // ⛔ 不预填姓名：手机号不是姓名，预填会让管理员直接点保存
    setNickname('');
    setRole(defaultRole || '');
    setError('');
    setSaving(false);
  }, [isOpen, defaultRole, userId]);

  if (!isOpen) return null;

  const submit = async () => {
    setError('');
    if (!nickname.trim()) {
      setError('请填写成员姓名。');
      return;
    }
    setSaving(true);
    try {
      await api.post(`/api/admin/groups/${gid(groupId)}/members`, {
        user_id: userId,
        nickname: nickname.trim(),
        role: role.trim() || null,
        enabled: true,
      });
      onAdded(`已把「${nickname.trim()}」加入本群名单，后续可直接在看板查看其业务`);
      onClose();
    } catch (err: any) {
      setError(err?.message || '添加未成功，请稍后重试');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/60 p-4">
      <div className="w-full max-w-md bg-white rounded-2xl shadow-xl border border-slate-200 overflow-hidden">
        <div className="px-5 py-3.5 border-b border-slate-100 flex items-start justify-between gap-3">
          <div>
            <h3 className="font-bold text-slate-900 text-sm flex items-center gap-2">
              <UserPlus className="w-4 h-4 text-emerald-600" />
              <span>一键添加该成员</span>
            </h3>
            <p className="text-[11px] text-slate-500 mt-1">
              该成员不在本群名单中，填写姓名与岗位角色即录入（业务记录保持不变）
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

        <div className="p-5 space-y-3">
          <div>
            <label className="text-[11px] font-semibold text-slate-600 mb-1 block">成员标识</label>
            <div className="px-3 py-2 rounded-xl bg-slate-50 border border-slate-200 text-xs font-mono text-slate-600">
              {userId}
            </div>
          </div>
          <div>
            <label className="text-[11px] font-semibold text-slate-600 mb-1 block">
              姓名 <span className="text-rose-600">（必填）</span>
            </label>
            <input
              value={nickname}
              onChange={(e) => setNickname(e.target.value)}
              maxLength={32}
              placeholder="例如：张伟"
              className="w-full px-3 py-2 rounded-xl border border-slate-200 text-xs text-slate-800 outline-none focus:border-blue-500"
            />
          </div>
          <div>
            <label className="text-[11px] font-semibold text-slate-600 mb-1 block">岗位角色</label>
            <select
              value={role}
              onChange={(e) => setRole(e.target.value)}
              className="w-full px-3 py-2 rounded-xl border border-slate-200 text-xs text-slate-800 outline-none focus:border-blue-500 bg-white"
            >
              <option value="">未设置</option>
              {ROLE_OPTIONS.map((item) => (
                <option key={item} value={item}>{ROLE_LABEL[item]}</option>
              ))}
            </select>
          </div>
          {error && (
            <div className="p-2.5 rounded-xl bg-rose-50 border border-rose-200 text-[11px] text-rose-700 flex items-start gap-2">
              <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}
        </div>

        <div className="px-5 py-3 border-t border-slate-100 flex items-center justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3.5 py-1.5 rounded-lg border border-slate-200 text-slate-700 text-xs font-semibold hover:bg-slate-50 transition-colors cursor-pointer"
          >
            取消
          </button>
          <button
            onClick={submit}
            disabled={saving}
            className="px-4 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-700 disabled:opacity-60 text-white text-xs font-semibold transition-colors cursor-pointer"
          >
            {saving ? '正在添加…' : '添加到名单'}
          </button>
        </div>
      </div>
    </div>
  );
};
