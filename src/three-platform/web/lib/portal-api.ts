import {BASE_PATH} from './base-path';

export interface PortalAccount {
  id: string; label: string; created_at: number; status: string; initial_state: string;
  checkin_state: string | null; confirmed_at: number | null; credited_units: number | null;
  pool_state?: 'public' | 'pending';
}
export interface PortalOverview {
  accounts: PortalAccount[]; account_count: number; active_account_count: number;
  balance_units: number; expiring_7d_units: number; today_checkin_units: number;
  grants: {source: string; amount_units: number; remaining_units: number; received_at: number; expires_at: number}[];
  events: {kind: string; amount_units: number; ts: number; expires_at: number | null}[];
  scale: number; snapshot_at: number; dispatch_enabled: boolean; dispatch_note: string;
  held_units: number; debt_units: number; allowed_models: string[];
  callable_units: number; pending_pool_units: number;
  pool_enabled: boolean;
}
export interface PortalMember extends PortalOverview {
  id: string; username: string; enabled: number; created_at: number;
}
export interface PortalRequest {
  ts: number; model: string; status: number; prompt_tokens: number;
  completion_tokens: number; latency_ms: number; credit: number | null;
  billing_state?: string;
}
export class PortalError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value);
const whole = (value: unknown): value is number => Number.isSafeInteger(value) && (value as number) >= 0;
const strings = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string');
const list = (value: unknown, valid: (item: unknown) => boolean) => Array.isArray(value) && value.every(valid);
const account = (value: unknown) => record(value) && ['id', 'label', 'status', 'initial_state'].every(key => typeof value[key] === 'string')
  && whole(value.created_at) && (value.checkin_state === null || typeof value.checkin_state === 'string');
const overview = (value: unknown) => record(value)
  && ['account_count', 'active_account_count', 'balance_units', 'expiring_7d_units', 'today_checkin_units', 'held_units', 'debt_units', 'callable_units', 'pending_pool_units', 'snapshot_at'].every(key => whole(value[key]))
  && whole(value.scale) && value.scale > 0 && typeof value.dispatch_enabled === 'boolean' && typeof value.pool_enabled === 'boolean'
  && typeof value.dispatch_note === 'string' && strings(value.allowed_models) && list(value.accounts, account)
  && list(value.grants, row => record(row) && typeof row.source === 'string'
    && ['amount_units', 'remaining_units', 'received_at', 'expires_at'].every(key => whole(row[key])))
  && list(value.events, row => record(row) && typeof row.kind === 'string' && Number.isSafeInteger(row.amount_units)
    && whole(row.ts) && (row.expires_at === null || whole(row.expires_at)));

function validResponse(path: string, method: string, value: unknown) {
  if (!record(value)) return false;
  const route = path.split('?')[0];
  if (method === 'GET') {
    if (route === 'portal/me') return typeof value.id === 'string' && !!value.id && typeof value.username === 'string';
    if (route === 'portal/overview') return overview(value);
    if (route === 'portal/requests') return whole(value.page) && value.page >= 1 && value.page <= 10000 && whole(value.total)
      && Array.isArray(value.items) && value.items.length <= 30 && value.items.every(row => record(row)
        && whole(row.ts) && typeof row.model === 'string' && whole(row.status));
    if (route === 'portal/keys') return strings(value.allowed_models) && list(value.items, row => record(row)
      && whole(row.id) && typeof row.label === 'string' && typeof row.prefix === 'string' && typeof row.enabled === 'boolean' && whole(row.created_at));
    if (route === 'portal-admin/users') return typeof value.automatic_checkin === 'boolean' && list(value.users,
      row => record(row) && typeof row.id === 'string' && typeof row.username === 'string' && [0, 1].includes(row.enabled as number) && overview(row));
  }
  if (method === 'POST' && route === 'portal/keys') return whole(value.id) && typeof value.key === 'string' && !!value.key;
  if (route === 'portal/auth/start') return typeof value.session === 'string' && /^[a-f0-9]{48}$/.test(value.session)
    && typeof value.auth_url === 'string' && whole(value.expires_in) && value.expires_in > 0 && value.expires_in <= 300;
  if (route === 'portal/auth/poll') return ['waiting', 'processing', 'complete', 'expired', 'failed'].includes(value.state as string);
  return true;
}

export async function portalFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const method = (options.method || 'GET').toUpperCase();
  const read = method === 'GET' || method === 'HEAD';
  const controller = new AbortController();
  const cancelled = () => controller.abort(options.signal?.reason);
  if (options.signal?.aborted) throw options.signal.reason;
  options.signal?.addEventListener('abort', cancelled, {once: true});
  let timedOut = false;
  // Only reads have a deadline. Aborting a mutation must never imply rollback.
  const timer = read ? setTimeout(() => {timedOut = true; controller.abort();}, 15000) : null;
  try {
    const response = await fetch(`${BASE_PATH}/api/${path}`, {
      ...options, signal: controller.signal, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json', ...options.headers},
    });
    const body: unknown = await response.json().catch(() => null);
    if (!response.ok) throw new PortalError(response.status,
      record(body) && typeof body.detail === 'string' ? body.detail : '暂时无法读取，请稍后重试');
    if (controller.signal.aborted) throw controller.signal.reason;
    if (!validResponse(path, method, body)) throw new Error(read ? '响应数据异常，请重新读取。' : '响应数据异常，操作结果待确认；请先刷新核对，不要重复提交。');
    return body as T;
  } catch (err) {
    if (options.signal?.aborted) throw options.signal.reason;
    if (err instanceof PortalError) throw err;
    if (timedOut) throw new Error('读取超时，请重新读取；没有自动重试。');
    throw new Error(read ? '连接或响应数据异常，请重新读取。' : '操作结果待确认，请先刷新核对，不要重复提交；没有自动重试。');
  } finally {
    if (timer !== null) clearTimeout(timer);
    options.signal?.removeEventListener('abort', cancelled);
  }
}
export const points = (units: number, scale = 1_000_000) => (units / scale).toLocaleString('zh-CN', {maximumFractionDigits: 6});
export const portalTime = (seconds: number) => new Date(seconds * 1000).toLocaleString('zh-CN', {
  timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit',
});
export function checkinLabel(state: string | null): string {
  return ({confirmed: '今日已签到', confirmed_uncredited: '已签到 · 积分待核实',
    running: '签到中', reconciling: '正在核对到账', unknown: '结果待确认', failed: '签到失败 · 可重试'} as Record<string, string>)[state || ''] || '今日未签到';
}
export function canCheckin(account: PortalAccount): boolean {
  return account.status === 'active' && account.initial_state === 'confirmed'
    && (!account.checkin_state || account.checkin_state === 'failed');
}
