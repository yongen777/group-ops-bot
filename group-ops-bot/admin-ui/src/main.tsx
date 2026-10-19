import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import './index.css';
import { App } from './App';
import { LoginPage } from './pages/LoginPage';
import { ChangePasswordPage } from './pages/ChangePasswordPage';
import { api, setCsrfToken } from './lib/api';
import type { AdminMe } from './components/system/AdminUserCard';

/**
 * 启动顺序（重要）：
 * 1. /login、/change-password 两个「门外页」由前端自渲染，服务端只发外壳
 * 2. 其余页面先取 /api/admin/me —— 同时拿到 CSRF 令牌与「是否必须改密」
 * 3. CSRF 只驻内存，不落 localStorage；鉴权失败统一回登录页
 */
function currentPath(): string {
  const path = window.location.pathname.replace(/\/+$/, '');
  return path || '/';
}

async function boot() {
  const container = document.getElementById('root');
  if (!container) return;
  const root = createRoot(container);
  const path = currentPath();

  if (path === '/login') {
    root.render(
      <React.StrictMode>
        <LoginPage />
      </React.StrictMode>,
    );
    return;
  }

  try {
    const me = await api.get<AdminMe & { csrf_token: string }>('/api/admin/me');
    setCsrfToken(me.csrf_token);

    if (path === '/change-password') {
      root.render(
        <React.StrictMode>
          <ChangePasswordPage onChanged={() => undefined} />
        </React.StrictMode>,
      );
      return;
    }

    root.render(
      <React.StrictMode>
        <BrowserRouter>
          <App me={{ username: me.username, must_change_password: me.must_change_password }} />
        </BrowserRouter>
      </React.StrictMode>,
    );
  } catch {
    window.location.href = '/login';
  }
}

boot();
