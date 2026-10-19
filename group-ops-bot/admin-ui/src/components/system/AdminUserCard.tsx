import React, { useState } from 'react';
import {
  User,
  Key,
  LogOut,
  X,
  CheckCircle2,
  Shield,
  ChevronDown,
  AlertCircle,
} from 'lucide-react';
import { api, plainError, setCsrfToken } from '../../lib/api';

export interface AdminMe {
  username: string;
  must_change_password: boolean;
}

/* ------------------------------------------------------------------ */
/* 修改密码弹窗（强制改密时不可关闭）                                   */
/* ------------------------------------------------------------------ */

interface ChangePasswordModalProps {
  isOpen: boolean;
  forced?: boolean;
  onClose: () => void;
  onChanged: () => void;
}

export const ChangePasswordModal: React.FC<ChangePasswordModalProps> = ({
  isOpen,
  forced = false,
  onClose,
  onChanged,
}) => {
  const [oldPassword, setOldPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (!isOpen) return null;

  const submit = async () => {
    setErrorMsg(null);
    if (!oldPassword) {
      setErrorMsg('请输入原密码');
      return;
    }
    if (newPassword.length < 8) {
      setErrorMsg('新密码长度不能少于 8 位');
      return;
    }
    if (newPassword !== confirmPassword) {
      setErrorMsg('两次输入的新密码不一致');
      return;
    }
    if (newPassword === oldPassword) {
      setErrorMsg('新密码不能与原密码相同');
      return;
    }
    setBusy(true);
    try {
      await api.post('/api/admin/change-password', {
        old_password: oldPassword,
        new_password: newPassword,
      });
      setSuccessMsg('密码已修改，当前登录已失效，请使用新密码重新登录。');
      window.setTimeout(() => {
        setCsrfToken('');
        window.location.href = '/login';
      }, 1600);
      onChanged();
    } catch (err) {
      setErrorMsg(err instanceof Error ? plainError(err.message) : '密码修改未成功');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
        <div className="flex items-center justify-between border-b border-slate-100 pb-3">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-xl bg-blue-600 text-white flex items-center justify-center font-bold text-xs">
              <Key className="w-4 h-4" />
            </div>
            <div>
              <h3 className="font-bold text-slate-900 text-base">修改管理员密码</h3>
              <span className="text-[11px] text-slate-400">群运维机器人管理后台 · 身份凭据</span>
            </div>
          </div>
          {!forced && (
            <button
              onClick={onClose}
              className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 cursor-pointer"
            >
              <X className="w-5 h-5" />
            </button>
          )}
        </div>

        {forced && (
          <div className="p-2.5 bg-amber-50 border border-amber-200 rounded-xl text-amber-800 text-xs flex items-center gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            <span>首次登录必须修改初始密码后才能使用后台功能。</span>
          </div>
        )}

        <div className="space-y-3 text-xs">
          {errorMsg && (
            <div className="p-2.5 bg-rose-50 border border-rose-200 rounded-xl text-rose-700 flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              <span>{errorMsg}</span>
            </div>
          )}
          {successMsg && (
            <div className="p-2.5 bg-emerald-50 border border-emerald-200 rounded-xl text-emerald-800 flex items-center gap-1.5">
              <CheckCircle2 className="w-3.5 h-3.5 shrink-0 text-emerald-600" />
              <span>{successMsg}</span>
            </div>
          )}

          <div>
            <label className="font-semibold text-slate-700 block mb-1">当前原密码</label>
            <input
              type="password"
              placeholder="请输入当前登录密码"
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>

          <div>
            <label className="font-semibold text-slate-700 block mb-1">新密码 (至少 8 位)</label>
            <input
              type="password"
              placeholder="至少8位字符，建议包含字母与数字"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>

          <div>
            <label className="font-semibold text-slate-700 block mb-1">确认新密码</label>
            <input
              type="password"
              placeholder="请再次输入新密码"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>

          <div className="pt-2 flex items-center justify-end gap-2">
            {!forced && (
              <button
                onClick={onClose}
                className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
              >
                取消
              </button>
            )}
            <button
              onClick={submit}
              disabled={busy}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl font-semibold shadow-xs cursor-pointer disabled:opacity-60"
            >
              {busy ? '正在提交…' : '确认修改'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* 侧边栏底部管理员卡片                                                 */
/* ------------------------------------------------------------------ */

interface AdminUserCardProps {
  me: AdminMe;
  onLoggedOut: () => void;
}

export const AdminUserCard: React.FC<AdminUserCardProps> = ({ me, onLoggedOut }) => {
  const [open, setOpen] = useState(false);
  const [modal, setModal] = useState<'none' | 'password' | 'profile' | 'logout'>('none');
  const [busy, setBusy] = useState(false);

  const display = me.username || '管理员';
  const initial = display.slice(0, 1).toUpperCase();

  const doLogout = async () => {
    setBusy(true);
    try {
      await api.post('/api/admin/logout');
    } catch {
      /* 会话已失效也照常回登录页 */
    } finally {
      setCsrfToken('');
      onLoggedOut();
    }
  };

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((prev) => !prev)}
        className="w-full flex items-center justify-between p-2 rounded-xl bg-slate-50 hover:bg-slate-100 border border-slate-200/80 transition-all cursor-pointer text-left"
      >
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-8 h-8 rounded-full bg-blue-600 text-white font-bold text-xs flex items-center justify-center shrink-0 shadow-xs">
            {initial}
          </div>
          <div className="truncate">
            <div className="font-bold text-xs text-slate-800 leading-tight">{display}</div>
            <div className="text-[10px] text-blue-600 flex items-center gap-1 font-medium">
              <Shield className="w-2.5 h-2.5" />
              <span>超级管理员</span>
            </div>
          </div>
        </div>
        <ChevronDown className="w-3.5 h-3.5 text-slate-400 shrink-0" />
      </button>

      {open && (
        <div className="absolute left-0 bottom-full mb-2 w-full bg-white rounded-2xl shadow-xl border border-slate-200 p-1.5 z-50 text-xs animate-in fade-in zoom-in-95">
          <div className="px-3 py-2 border-b border-slate-100">
            <div className="font-bold text-slate-800">{display}</div>
            <div className="text-[10px] text-slate-400 font-mono">单账号管理员体系</div>
            <div className="text-[10px] text-emerald-600 font-semibold mt-0.5">● 当前在线</div>
          </div>

          <div className="py-1 space-y-0.5">
            <button
              onClick={() => {
                setOpen(false);
                setModal('password');
              }}
              className="w-full text-left px-3 py-2 rounded-xl hover:bg-slate-50 text-slate-700 font-medium flex items-center gap-2 cursor-pointer transition-colors"
            >
              <Key className="w-3.5 h-3.5 text-blue-600" />
              <span>修改管理员密码</span>
            </button>

            <button
              onClick={() => {
                setOpen(false);
                setModal('profile');
              }}
              className="w-full text-left px-3 py-2 rounded-xl hover:bg-slate-50 text-slate-700 font-medium flex items-center gap-2 cursor-pointer transition-colors"
            >
              <User className="w-3.5 h-3.5 text-slate-500" />
              <span>账号与权限信息</span>
            </button>
          </div>

          <div className="pt-1 border-t border-slate-100">
            <button
              onClick={() => {
                setOpen(false);
                setModal('logout');
              }}
              className="w-full text-left px-3 py-2 rounded-xl hover:bg-rose-50 text-rose-600 font-semibold flex items-center gap-2 cursor-pointer transition-colors"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span>安全退出后台</span>
            </button>
          </div>
        </div>
      )}

      <ChangePasswordModal
        isOpen={modal === 'password'}
        onClose={() => setModal('none')}
        onChanged={() => setModal('none')}
      />

      {modal === 'profile' && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-xl bg-blue-600 text-white flex items-center justify-center font-bold text-xs">
                  <User className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="font-bold text-slate-900 text-base">管理员账户信息</h3>
                  <span className="text-[11px] text-slate-400">群运维机器人管理后台 · 身份凭据</span>
                </div>
              </div>
              <button
                onClick={() => setModal('none')}
                className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <div className="p-3 bg-slate-50 rounded-xl border border-slate-100 space-y-2 text-xs">
              <div className="flex justify-between">
                <span className="text-slate-400">登录账号</span>
                <span className="font-semibold text-slate-800 font-mono">{me.username}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">系统身份</span>
                <span className="text-blue-700 font-medium">超级管理员 (单账号体系)</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">密码策略</span>
                <span className="text-slate-600">至少 8 位 · 可随时更换</span>
              </div>
              <div className="flex justify-between">
                <span className="text-slate-400">安全防线</span>
                <span className="text-slate-600">失败次数锁定 · 会话可即时吊销</span>
              </div>
            </div>

            <div className="pt-2 flex justify-end">
              <button
                onClick={() => setModal('none')}
                className="px-4 py-2 bg-slate-900 text-white rounded-xl font-medium text-xs cursor-pointer"
              >
                知道了
              </button>
            </div>
          </div>
        </div>
      )}

      {modal === 'logout' && (
        <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
          <div className="bg-white rounded-2xl max-w-md w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
            <div className="flex items-center justify-between border-b border-slate-100 pb-3">
              <div className="flex items-center gap-2">
                <div className="w-8 h-8 rounded-xl bg-rose-600 text-white flex items-center justify-center font-bold text-xs">
                  <LogOut className="w-4 h-4" />
                </div>
                <div>
                  <h3 className="font-bold text-slate-900 text-base">退出登录确认</h3>
                  <span className="text-[11px] text-slate-400">群运维机器人管理后台 · 身份凭据</span>
                </div>
              </div>
              <button
                onClick={() => setModal('none')}
                className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 cursor-pointer"
              >
                <X className="w-5 h-5" />
              </button>
            </div>

            <p className="text-xs text-slate-600 leading-relaxed">
              确定要退出当前管理员登录会话吗？退出后将清除当前浏览器的登录凭据，下次访问需重新输入账号密码。
            </p>

            <div className="pt-2 flex items-center justify-end gap-2">
              <button
                onClick={() => setModal('none')}
                className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-medium text-xs cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={doLogout}
                disabled={busy}
                className="px-4 py-2 bg-rose-600 hover:bg-rose-700 text-white rounded-xl font-semibold shadow-xs flex items-center gap-1.5 text-xs cursor-pointer disabled:opacity-60"
              >
                <LogOut className="w-3.5 h-3.5" />
                <span>{busy ? '正在退出…' : '确认退出登录'}</span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
