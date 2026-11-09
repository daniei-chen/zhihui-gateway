import {canCheckin, type PortalAccount} from './portal-api';

export function checkinSummary(accounts: PortalAccount[]) {
  const eligible = accounts.filter(account => account.status === 'active' && account.initial_state === 'confirmed');
  return {
    total: eligible.length,
    confirmed: eligible.filter(account => account.checkin_state === 'confirmed').length,
    pending: eligible.filter(account => ['confirmed_uncredited', 'running', 'reconciling', 'unknown'].includes(account.checkin_state || '')).length,
    ready: eligible.filter(canCheckin).length,
  };
}

export function requestStatus(status: number) {
  if (status >= 200 && status < 300) return {label: '成功', tone: 'success'};
  if (status === 499) return {label: '已取消', tone: 'neutral'};
  if (status >= 400) return {label: '失败', tone: 'error'};
  return {label: '其他', tone: 'neutral'};
}

export function requestTokens(prompt: number, completion: number): string {
  return [prompt, completion, prompt + completion].every(value => Number.isSafeInteger(value) && value >= 0)
    ? (prompt + completion).toLocaleString('zh-CN') : '待核实';
}

export const requestLatency = (value: number) => Number.isFinite(value) && value >= 0
  ? value >= 1000 ? `${(value / 1000).toLocaleString('zh-CN', {maximumFractionDigits: 2})} s` : `${value} ms`
  : '待核实';

export const requestCredit = (value: number | null) => typeof value === 'number' && Number.isFinite(value) && value >= 0
  ? value.toLocaleString('zh-CN', {maximumFractionDigits: 6}) : '待核实';
