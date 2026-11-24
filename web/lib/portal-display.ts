import {canCheckin, type PortalAccount} from './portal-api';
import type {TParams} from './i18n';

/** 本地翻译函数类型：不引 provider，避免纯函数模块拖进 React。 */
type Translate = (key: string, params?: TParams) => string;

export function checkinSummary(accounts: PortalAccount[]) {
  const eligible = accounts.filter(account => account.status === 'active' && account.initial_state === 'confirmed');
  return {
    total: eligible.length,
    confirmed: eligible.filter(account => account.checkin_state === 'confirmed').length,
    pending: eligible.filter(account => ['confirmed_uncredited', 'running', 'reconciling', 'unknown'].includes(account.checkin_state || '')).length,
    ready: eligible.filter(canCheckin).length,
  };
}

// 下面四个函数的标签文案走 t()：它们只被门户页使用，而门户页原先整块是硬编码中文
// （外围用户看到「成功 / 待核实」这类中文）。数字格式仍用 zh-CN 的千分位，与 en 一致。
export function requestStatus(status: number, t: Translate) {
  if (status >= 200 && status < 300) return {label: t('portal.status.success'), tone: 'success'};
  if (status === 499) return {label: t('portal.status.cancelled'), tone: 'neutral'};
  if (status >= 400) return {label: t('portal.status.failed'), tone: 'error'};
  return {label: t('portal.status.other'), tone: 'neutral'};
}

export function requestTokens(prompt: number, completion: number, t: Translate): string {
  return [prompt, completion, prompt + completion].every(value => Number.isSafeInteger(value) && value >= 0)
    ? (prompt + completion).toLocaleString('zh-CN') : t('portal.pending');
}

export const requestLatency = (value: number, t: Translate) => Number.isFinite(value) && value >= 0
  ? value >= 1000 ? `${(value / 1000).toLocaleString('zh-CN', {maximumFractionDigits: 2})} s` : `${value} ms`
  : t('portal.pending');

export const requestCredit = (value: number | null, t: Translate) => typeof value === 'number' && Number.isFinite(value) && value >= 0
  ? value.toLocaleString('zh-CN', {maximumFractionDigits: 6}) : t('portal.pending');

/** 「积分待核实」的判据：与 requestCredit 内部判定一致，但不依赖其文案。 */
export const creditUnverified = (value: number | null | undefined): boolean =>
  !(typeof value === 'number' && Number.isFinite(value) && value >= 0);
