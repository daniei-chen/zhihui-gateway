/**
 * D-41：「写请求的同源锚点」在前端要回答的两个问题（纯函数，可单测）。
 *
 * 背景：`Host=Origin=evil.test` 在修前能通过同源校验（DNS rebinding）。修后
 * 期望来源来自服务端配置/可信反代，只有「用域名直连、又没有任何服务端声明」这一
 * 种组合会被拒。这个组合**必须在使用者填表之前**就说出来，而不是等他提交后看到
 * 一个通用 403 —— 那正是本次修复要「去掉静默」的地方。
 *
 * 两个判据都是服务端给的机器可读字段，不在前端猜 `location.hostname`：
 *   · `GET /api/system/origin-status` 的 `writes_allowed`（D-41，管理端状态端点；
 *     登录页用匿名的 `GET /api/origin-check`）；
 *   · 403 体的 `detail`（后端约定 `origin_unconfigured`，见
 *     `server/security.py:_reject_unanchored`）与同名响应头
 *     `X-Manager-Csrf-Reason`。
 */

import type {OriginCheck, OriginCheckState} from './types';

/** 后端状态 → i18n 键。未知状态返回空串：宁可少显示一行，也不编造一个状态。 */
const STATE_KEYS: Record<OriginCheckState, string> = {
  configured: 'origin.stateConfigured',
  proxy: 'origin.stateProxy',
  'loopback-only': 'origin.stateLoopback',
  unconfigured: 'origin.stateUnconfigured',
  relaxed: 'origin.stateRelaxed',
};

/** 页面上「这个地址提交会被拒」的横幅要不要出现。 */
export function needsOriginNotice(check?: OriginCheck | null): boolean {
  // 字段缺失（旧后端 / 升级顺序不一致）一律不提示：凭空吓人比晚一条提示更糟。
  // 只有「服务端已经明确说没有锚点」才提示：writes_allowed === false，或状态本身
  // 就是 unconfigured（老一点的 D-41 后端只给 state）。
  if (!check) return false;
  return check.writes_allowed === false
    || (check.writes_allowed === undefined && check.state === 'unconfigured');
}

export function originStateKey(state?: string | null): string {
  if (!state) return '';
  return STATE_KEYS[state as OriginCheckState] || '';
}

/**
 * 403 的机器可读原因。body `detail` 与响应头任一命中即算 —— 服务端两处同值，
 * 但中间层（代理、日志脱敏）可能只留下一个。
 */
export function originBlockedReason(data: unknown, headerReason?: string | null): string {
  const header = (headerReason || '').trim().toLowerCase();
  if (header === 'origin_unconfigured') return 'origin_unconfigured';
  if (data && typeof data === 'object') {
    const detail = (data as {detail?: unknown}).detail;
    if (typeof detail === 'string' && detail.trim().toLowerCase() === 'origin_unconfigured') {
      return 'origin_unconfigured';
    }
  }
  return '';
}
