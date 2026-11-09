export interface OperationPage<T> {items: T[]; total: number; page: number; page_size: number}
export interface WalletPending {
  request_id: string; owner_id: string; username: string; key_id: number; state: string;
  created_at: number; held_units: number; worker_status: 'alive' | 'dead' | 'unproven';
  can_resolve: boolean; blocked_reason: string; receipt_units: number | null;
  end_confirmed: boolean;
  request: {model: string; status: number; terminal_state: string; credit: number | null} | null;
}
export interface WalletDebt {
  owner_id: string; username: string; enabled: number; amount_units: number; repayable_units: number;
}
export interface WalletHistory {
  id: string; owner_id: string; username: string; kind: string; amount_units: number;
  before_units: number | null; after_units: number | null; actor: string; reason: string; evidence: string; created_at: number;
  expires_at: number | null;
}
export function exactCredit(units: number): string {
  if (!Number.isSafeInteger(units) || units < 0) throw new Error('积分单位无效');
  return `${Math.floor(units / 1_000_000)}.${String(units % 1_000_000).padStart(6, '0')}`;
}
export function creditInputUnits(text: string): number | null {
  if (!/^(0|[1-9]\d{0,9})(\.\d{1,6})?$/.test(text)) return null;
  const [whole, fraction = ''] = text.split('.');
  const result = Number(whole) * 1_000_000 + Number(fraction.padEnd(6, '0'));
  return Number.isSafeInteger(result) && result <= 1_000_000_000_000_000 ? result : null;
}
export const operationLabel = (kind: string) => ({resolve: '费用核对', repay: '积分抵历史欠额', waive: '管理员豁免', bonus: '管理员赠送积分', platform_overrun: '平台承担超额（不扣用户）'}[kind] || kind);
