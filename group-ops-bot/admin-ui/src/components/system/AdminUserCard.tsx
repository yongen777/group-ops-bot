import React, { useEffect, useState } from 'react';
import {
  User,
  UserCog,
  Key,
  Link2,
  Copy,
  Check,
  LogOut,
  X,
  CheckCircle2,
  Shield,
  ChevronDown,
  AlertCircle,
} from 'lucide-react';
import { api, plainError, setCsrfToken } from '../../lib/api';

/** 密码校验框统一属性：⛔ 不用 type="password" —— Edge 会把它当登录表单，
 *  点「验证」时弹「Microsoft 登录（无网络连接 2603）」。改成 text + CSS 掩码，
 *  浏览器不再识别为密码字段，也就不会触发密码管理器/账户登录。 */
const MASKED_INPUT_STYLE = { WebkitTextSecurity: 'disc' } as unknown as React.CSSProperties;

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
              type="text"
              autoComplete="off"
              spellCheck={false}
              style={MASKED_INPUT_STYLE}
              placeholder="请输入当前登录密码"
              value={oldPassword}
              onChange={(e) => setOldPassword(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>

          <div>
            <label className="font-semibold text-slate-700 block mb-1">新密码 (至少 8 位)</label>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              style={MASKED_INPUT_STYLE}
              placeholder="至少8位字符，建议包含字母与数字"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>

          <div>
            <label className="font-semibold text-slate-700 block mb-1">确认新密码</label>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              style={MASKED_INPUT_STYLE}
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
/* 回调地址查看（⭐ 2026-10-20 用户指令：必须再输一次密码；只读不可改）   */
/* ------------------------------------------------------------------ */

interface CallbackUrlModalProps {
  isOpen: boolean;
  onClose: () => void;
}

export const CallbackUrlModal: React.FC<CallbackUrlModalProps> = ({ isOpen, onClose }) => {
  const [password, setPassword] = useState('');
  const [url, setUrl] = useState('');
  const [note, setNote] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setPassword('');
    setUrl('');
    setNote('');
    setErrorMsg(null);
    setCopied(false);
  }, [isOpen]);

  if (!isOpen) return null;

  const verify = async () => {
    setErrorMsg(null);
    if (!password) {
      setErrorMsg('请输入登录密码以查看回调地址');
      return;
    }
    setBusy(true);
    try {
      const data = await api.post<{ ok: boolean; url: string; note?: string }>(
        '/api/admin/callback-url',
        { password },
      );
      setUrl(data.url || '');
      setNote(data.note || '');
    } catch (err) {
      setErrorMsg(err instanceof Error ? plainError(err.message) : '验证未通过');
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!url) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(url);
      } else {
        // 内网 http 下 navigator.clipboard 不可用：退回 execCommand
        const el = document.createElement('textarea');
        el.value = url;
        document.body.appendChild(el);
        el.select();
        document.execCommand('copy');
        document.body.removeChild(el);
      }
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setErrorMsg('复制失败，请手动选中地址复制');
    }
  };

  return (
    <div className="fixed inset-0 z-50 bg-slate-900/50 backdrop-blur-xs flex items-center justify-center p-4">
      <div className="bg-white rounded-2xl max-w-lg w-full p-6 shadow-2xl border border-slate-200 animate-in fade-in zoom-in-95 space-y-4">
        <div className="flex items-center justify-between border-b border-slate-100 pb-3">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-xl bg-slate-900 text-white flex items-center justify-center font-bold text-xs">
              <Link2 className="w-4 h-4" />
            </div>
            <div>
              <h3 className="font-bold text-slate-900 text-base">群消息回调地址</h3>
              <span className="text-[11px] text-slate-400">量子密信平台接入配置 · 只读</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 cursor-pointer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {errorMsg && (
          <div className="p-2.5 bg-rose-50 border border-rose-200 rounded-xl text-rose-700 text-xs flex items-center gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            <span>{errorMsg}</span>
          </div>
        )}

        {!url ? (
          <div className="space-y-3 text-xs">
            <div className="p-3 bg-amber-50 border border-amber-200 rounded-xl text-amber-800 leading-relaxed">
              回调地址属于部署级配置，<span className="font-bold">查看前需再次验证登录密码</span>。
              验证通过后仅可查看与复制，后台不提供修改入口。
            </div>
            <div>
              <label className="font-semibold text-slate-700 block mb-1">登录密码</label>
              <input
                type="text"
                autoComplete="off"
                spellCheck={false}
                style={MASKED_INPUT_STYLE}
                placeholder="请输入当前登录密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') verify();
                }}
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
              />
            </div>
            <div className="flex justify-end gap-2 pt-1">
              <button
                onClick={onClose}
                className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
              >
                取消
              </button>
              <button
                onClick={verify}
                disabled={busy}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-xl font-semibold shadow-xs cursor-pointer disabled:opacity-60"
              >
                {busy ? '正在验证…' : '验证并查看'}
              </button>
            </div>
          </div>
        ) : (
          <div className="space-y-3 text-xs">
            <div>
              <label className="font-semibold text-slate-700 block mb-1">回调地址（POST JSON）</label>
              <div className="flex items-center gap-2">
                <input
                  readOnly
                  value={url}
                  onFocus={(e) => e.currentTarget.select()}
                  className="flex-1 bg-slate-50 border border-slate-200 rounded-xl p-2.5 font-mono text-[12px] text-slate-800 outline-none"
                />
                <button
                  onClick={copy}
                  className="px-3 py-2 rounded-xl bg-slate-900 hover:bg-slate-800 text-white font-semibold flex items-center gap-1.5 shrink-0 cursor-pointer"
                >
                  {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
                  <span>{copied ? '已复制' : '复制'}</span>
                </button>
              </div>
            </div>
            <div className="p-3 bg-slate-50 border border-slate-200 rounded-xl text-slate-600 leading-relaxed">
              {note || '该地址由服务器部署配置决定，后台只能查看与复制，不能修改。'}
            </div>
            <div className="flex justify-end">
              <button
                onClick={onClose}
                className="px-4 py-2 bg-slate-900 text-white rounded-xl font-medium cursor-pointer"
              >
                关闭
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

/* ------------------------------------------------------------------ */
/* 修改管理员账号名（⭐ 2026-10-20 用户指令：需验证登录密码）             */
/* ------------------------------------------------------------------ */

interface ChangeUsernameModalProps {
  isOpen: boolean;
  current: string;
  onClose: () => void;
  onChanged: (username: string) => void;
}

export const ChangeUsernameModal: React.FC<ChangeUsernameModalProps> = ({
  isOpen,
  current,
  onClose,
  onChanged,
}) => {
  const [newUsername, setNewUsername] = useState('');
  const [password, setPassword] = useState('');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setNewUsername('');
    setPassword('');
    setErrorMsg(null);
  }, [isOpen]);

  if (!isOpen) return null;

  const submit = async () => {
    setErrorMsg(null);
    const name = newUsername.trim();
    if (name.length < 2 || name.length > 32) {
      setErrorMsg('账号名长度需在 2~32 位之间');
      return;
    }
    if (/\s/.test(name)) {
      setErrorMsg('账号名不能包含空格');
      return;
    }
    if (name === current) {
      setErrorMsg('新账号名与当前账号名相同');
      return;
    }
    if (!password) {
      setErrorMsg('请输入登录密码以验证身份');
      return;
    }
    setBusy(true);
    try {
      const data = await api.post<{ ok: boolean; username: string }>(
        '/api/admin/username',
        { new_username: name, password },
      );
      onChanged(data.username || name);
    } catch (err) {
      setErrorMsg(err instanceof Error ? plainError(err.message) : '修改未成功');
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
              <UserCog className="w-4 h-4" />
            </div>
            <div>
              <h3 className="font-bold text-slate-900 text-base">修改登录账号名</h3>
              <span className="text-[11px] text-slate-400">改完下次登录请使用新账号名</span>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1 rounded-lg hover:bg-slate-100 text-slate-400 hover:text-slate-600 cursor-pointer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {errorMsg && (
          <div className="p-2.5 bg-rose-50 border border-rose-200 rounded-xl text-rose-700 text-xs flex items-center gap-1.5">
            <AlertCircle className="w-3.5 h-3.5 shrink-0" />
            <span>{errorMsg}</span>
          </div>
        )}

        <div className="space-y-3 text-xs">
          <div className="p-2.5 bg-slate-50 border border-slate-100 rounded-xl flex justify-between">
            <span className="text-slate-400">当前账号名</span>
            <span className="font-semibold text-slate-800 font-mono">{current}</span>
          </div>
          <div>
            <label className="font-semibold text-slate-700 block mb-1">新账号名（2~32 位，不含空格）</label>
            <input
              type="text"
              placeholder="请输入新的登录账号名"
              value={newUsername}
              onChange={(e) => setNewUsername(e.target.value)}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>
          <div>
            <label className="font-semibold text-slate-700 block mb-1">登录密码（用于验证身份）</label>
            <input
              type="text"
              autoComplete="off"
              spellCheck={false}
              style={MASKED_INPUT_STYLE}
              placeholder="请输入当前登录密码"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') submit();
              }}
              className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 outline-none focus:border-blue-500 focus:bg-white"
            />
          </div>
          <div className="pt-2 flex items-center justify-end gap-2">
            <button
              onClick={onClose}
              className="px-4 py-2 rounded-xl text-slate-600 hover:bg-slate-100 font-medium cursor-pointer"
            >
              取消
            </button>
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
  const [modal, setModal] = useState<
    'none' | 'password' | 'profile' | 'callback' | 'username' | 'logout'
  >('none');
  const [busy, setBusy] = useState(false);
  // ⭐ 2026-10-20：改完账号名立即刷新卡片显示（不必整页重载）
  const [displayName, setDisplayName] = useState(me.username || '');
  useEffect(() => setDisplayName(me.username || ''), [me.username]);

  const display = displayName || '管理员';
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
                setModal('callback');
              }}
              className="w-full text-left px-3 py-2 rounded-xl hover:bg-slate-50 text-slate-700 font-medium flex items-center gap-2 cursor-pointer transition-colors"
            >
              <Link2 className="w-3.5 h-3.5 text-slate-500" />
              <span>查看回调地址</span>
            </button>

            <button
              onClick={() => {
                setOpen(false);
                setModal('username');
              }}
              className="w-full text-left px-3 py-2 rounded-xl hover:bg-slate-50 text-slate-700 font-medium flex items-center gap-2 cursor-pointer transition-colors"
            >
              <UserCog className="w-3.5 h-3.5 text-slate-500" />
              <span>修改账号名</span>
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

      <CallbackUrlModal
        isOpen={modal === 'callback'}
        onClose={() => setModal('none')}
      />

      <ChangeUsernameModal
        isOpen={modal === 'username'}
        current={display}
        onClose={() => setModal('none')}
        onChanged={(name) => {
          setDisplayName(name);
          setModal('none');
        }}
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
                <span className="font-semibold text-slate-800 font-mono">{display}</span>
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
