/* API 客户端：统一错误处理、401 跳登录、请求去重与超时。 */
const BASE = 'api/v1';
const inflight = new Map();

export class ApiError extends Error {
  constructor(message, { status = 0, code = '', body = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.body = body;
  }
}

function qs(params) {
  if (!params) return '';
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== '') usp.set(k, String(v));
  }
  const s = usp.toString();
  return s ? `?${s}` : '';
}

async function request(path, { method = 'GET', body, timeout = 30000, dedupe = false } = {}) {
  const key = `${method} ${path}`;
  if (dedupe && inflight.has(key)) return inflight.get(key);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeout);
  const p = (async () => {
    try {
      const res = await fetch(BASE + path, {
        method,
        headers: body ? { 'Content-Type': 'application/json' } : undefined,
        body: body ? JSON.stringify(body) : undefined,
        credentials: 'same-origin',
        cache: 'no-store',
        signal: ctrl.signal,
      });
      const text = await res.text();
      let data = null;
      try { data = text ? JSON.parse(text) : null; } catch { data = text; }
      if (res.status === 401) {
        const err = data?.error || {};
        if (!location.hash.includes('login')) window.dispatchEvent(new CustomEvent('qtg:unauthorized'));
        throw new ApiError(err.message || '未授权', { status: 401, code: err.code, body: data });
      }
      if (!res.ok) {
        const err = data?.error || {};
        throw new ApiError(err.message || `HTTP ${res.status}`, { status: res.status, code: err.code, body: data });
      }
      return data;
    } catch (e) {
      if (e instanceof ApiError) throw e;
      if (e.name === 'AbortError') throw new ApiError('请求超时，请重试', { code: 'timeout' });
      throw new ApiError(e.message || '网络错误', { code: 'network' });
    } finally {
      clearTimeout(timer);
      if (dedupe) inflight.delete(key);
    }
  })();
  if (dedupe) inflight.set(key, p);
  return p;
}

export const api = {
  // ---- 读 ----
  overview: (refresh = false) => request(`/overview${refresh ? '?refresh=true' : ''}`, { dedupe: !refresh }),
  accounts: (refresh = false) => request(`/accounts${refresh ? '?refresh=true' : ''}`, { dedupe: !refresh }),
  usage: (days = 14) => request(`/usage?days=${days}`),
  keys: () => request('/keys'),
  models: () => request('/models'),
  logs: () => request('/logs'),
  alerts: () => request('/alerts'),
  tasks: () => request('/tasks'),
  requests: (days = 3) => request(`/requests?days=${days}`),
  ops: () => request('/ops'),
  settings: () => request('/settings'),
  audit: (limit = 100) => request(`/audit?limit=${limit}`),
  health: () => request('/health'),
  meta: () => request('/meta'),

  // ---- 认证 ----
  login: (password) => request('/auth/login', { method: 'POST', body: { password } }),
  logout: () => request('/auth/logout', { method: 'POST' }),
  session: () => request('/auth/session'),

  // ---- 写 ----
  checkin: (platform = 'all') => request('/actions/checkin', { method: 'POST', body: { platform } }),
  accountAction: (platform, action, uid = '') =>
    request(`/actions/accounts/${platform}/${action}`, { method: 'POST', body: { uid, action } }),
  addAccount: (platform, credential, name = '', realm = '') =>
    request('/actions/accounts/add', { method: 'POST', body: { platform, credential, name, realm } }),
  saveKeys: (platform, keys, auth_disabled = null) =>
    request('/actions/keys', { method: 'POST', body: { platform, keys, auth_disabled } }),
  saveLimits: (payload) => request('/actions/limits', { method: 'POST', body: payload }),
  saveNotify: (payload) => request('/actions/notify', { method: 'POST', body: payload }),
  backup: (target) => request('/actions/backup', { method: 'POST', body: { target }, timeout: 900000 }),
  ackAlerts: () => request('/actions/alerts/ack', { method: 'POST' }),
  oauthStart: (platform, realm = 'cn') => request('/actions/oauth/start', { method: 'POST', body: { platform, realm } }),
  oauthPoll: (platform, state) => request(`/actions/oauth/poll${qs({ platform, state })}`),
  oauthFinish: (callback, name = '') =>
    request('/actions/oauth/finish', { method: 'POST', body: { platform: 'trae', callback, name } }),
  invalidateCache: (prefix = '') => request(`/cache/invalidate${qs({ prefix })}`, { method: 'POST' }),

  // ---- 数据面管理 ----
  dataPlane: () => request('/data-plane'),
  qtgKeys: () => request('/qtg-keys'),
  createQtgKey: (payload) => request('/actions/qtg-keys', { method: 'POST', body: payload }),
  qtgKeyOp: (id, op) => request(`/actions/qtg-keys/${id}/${op}`, { method: 'POST' }),
  saveUpstreamKey: (platform, key) => request('/actions/upstream-keys', { method: 'POST', body: { platform, key } }),
  proxyRequests: (limit = 200, platform = '', keyId = '') =>
    request(`/proxy-requests${qs({ limit, platform, key_id: keyId })}`),
  proxyStats: (days = 1) => request(`/proxy-stats?days=${days}`),
  pruneProxy: (days = 30) => request(`/actions/proxy-prune?keep_days=${days}`, { method: 'POST' }),
};

export { request, qs };
