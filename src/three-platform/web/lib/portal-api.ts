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
}
export class PortalError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function portalFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_PATH}/api/${path}`, {
    ...options, credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json', ...options.headers},
  });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new PortalError(response.status,
    typeof body?.detail === 'string' ? body.detail : '暂时无法读取，请稍后重试');
  return body as T;
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
