import React, { useState } from 'react';
import { ShieldCheck, AlertCircle, LogIn } from 'lucide-react';
import { api, plainError, setCsrfToken } from '../lib/api';

interface LoginResponse {
  ok: boolean;
  username: string;
  must_change_password: boolean;
  csrf_token: string;
}

/**
 * 登录页（React 自渲染，服务端只发外壳）。
 * 登录接口不要求 CSRF；成功返回的 csrf_token 只驻内存。
 */
export const LoginPage: React.FC = () => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setError(null);
    if (!username.trim() || !password) {
      setError('请输入账号与密码');
      return;
    }
    setBusy(true);
    try {
      const data = await api.post<LoginResponse>('/api/admin/login', {
        username: username.trim(),
        password,
      });
      setCsrfToken(data.csrf_token);
      window.location.href = data.must_change_password ? '/change-password' : '/';
    } catch (err) {
      setError(err instanceof Error ? plainError(err.message) : '登录未成功，请稍后重试');
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen w-full bg-[#f7f8fa] flex items-center justify-center p-4">
      <div className="w-full max-w-sm">
        <div className="flex items-center gap-2.5 mb-5 px-1">
          <div className="w-10 h-10 rounded-xl bg-blue-600 flex items-center justify-center shadow-xs">
            <svg
              viewBox="0 0 24 24"
              className="w-5 h-5 text-white"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="8" width="18" height="12" rx="3" />
              <path d="M12 4v4" />
              <circle cx="12" cy="3" r="1" />
              <circle cx="9" cy="13" r="1" fill="currentColor" />
              <circle cx="15" cy="13" r="1" fill="currentColor" />
              <path d="M9.5 17h5" />
            </svg>
          </div>
          <div>
            <h1 className="font-bold text-base text-slate-900 tracking-tight">群运维机器人</h1>
            <div className="flex items-center gap-1.5">
              <span className="text-[10px] text-slate-400 font-mono">group-ops-bot</span>
              <span className="text-[9px] text-blue-600 bg-blue-50 px-1 py-0.2 rounded font-medium">
                管理后台
              </span>
            </div>
          </div>
        </div>

        <form
          onSubmit={submit}
          className="bg-white rounded-2xl border border-slate-200/90 shadow-2xs p-6 space-y-4"
        >
          <div>
            <h2 className="text-lg font-black text-slate-900 tracking-tight">管理员登录</h2>
            <p className="text-xs text-slate-500 mt-0.5">请使用后台管理员账号登录。</p>
          </div>

          {error && (
            <div className="p-2.5 bg-rose-50 border border-rose-200 rounded-xl text-rose-700 text-xs flex items-center gap-1.5">
              <AlertCircle className="w-3.5 h-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <div className="space-y-3 text-xs">
            <div>
              <label className="font-semibold text-slate-700 block mb-1">管理员账号</label>
              <input
                type="text"
                autoComplete="username"
                autoFocus
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="请输入账号"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800 outline-none focus:bg-white focus:border-blue-500 font-medium"
              />
            </div>

            <div>
              <label className="font-semibold text-slate-700 block mb-1">登录密码</label>
              <input
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                placeholder="请输入密码"
                className="w-full bg-slate-50 border border-slate-200 rounded-xl p-2.5 text-xs text-slate-800 outline-none focus:bg-white focus:border-blue-500 font-medium"
              />
            </div>
          </div>

          <button
            type="submit"
            disabled={busy}
            className="w-full py-2.5 bg-blue-600 hover:bg-blue-700 active:scale-95 disabled:opacity-60 text-white font-bold text-xs rounded-xl shadow-xs flex items-center justify-center gap-1.5 transition-all cursor-pointer"
          >
            <LogIn className="w-3.5 h-3.5" />
            <span>{busy ? '正在登录…' : '登录后台'}</span>
          </button>

          <div className="pt-1 border-t border-slate-100 flex items-center gap-1.5 text-[11px] text-slate-400">
            <ShieldCheck className="w-3.5 h-3.5 shrink-0" />
            <span>连续多次输错密码会临时锁定登录，请确认凭据后再试。</span>
          </div>
        </form>
      </div>
    </div>
  );
};
