/**
 * 后台 API 客户端。
 *
 * 纪律（沿用 D3-C）：
 * - CSRF 令牌只存内存（不落 localStorage），由 /api/admin/me 注入
 * - 401 -> 回登录页（登录页自身除外）
 * - 失败原因只回显后端给的中文提示；技术术语一律走兜底文案
 */

let csrfToken = '';

export function setCsrfToken(token: string): void {
  csrfToken = token || '';
}

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

const GENERIC = '操作未成功，请刷新页面后重试';
const TECH_PATTERN =
  /(payload|json|sql|null|undefined|traceback|exception|stack|csrf|http\s*\d{3}|internal|server error|validation)/i;

export function plainError(detail: unknown, fallback = GENERIC): string {
  if (typeof detail !== 'string') return fallback;
  const text = detail.trim();
  if (!text || TECH_PATTERN.test(text)) return fallback;
  return text;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    ...(init.headers as Record<string, string> | undefined),
  };
  if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  let body = init.body;
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(body);
  }
  const resp = await fetch(path, {
    ...init,
    body,
    headers,
    credentials: 'same-origin',
  });
  let data: any = null;
  try {
    data = await resp.json();
  } catch {
    data = null;
  }
  if (resp.status === 401 && !path.endsWith('/api/admin/me')) {
    window.location.href = '/login';
    throw new ApiError(401, '登录已失效');
  }
  if (!resp.ok) {
    const detail = data && (data.detail ?? data.message);
    throw new ApiError(resp.status, plainError(detail));
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'POST', body: body as BodyInit | undefined }),
  put: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'PUT', body: body as BodyInit | undefined }),
  del: <T>(path: string, body?: unknown) =>
    request<T>(path, { method: 'DELETE', body: body as BodyInit | undefined }),
};

export function qs(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const text = search.toString();
  return text ? `?${text}` : '';
}

export function gid(groupId: string): string {
  return encodeURIComponent(groupId);
}
