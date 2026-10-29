/**
 * 后端错误体的纯文本抽取（无依赖，可单测：`node --test lib/err-envelope.test.mjs`）。
 *
 * 后端现在有两种错误形状混用：
 *  · Manager/FastAPI：`{"detail": "..."}`，校验错误时 `detail` 是对象数组；
 *  · QTG（qtg/app/qtg/errors.py）：`{"error": {"code", "message", "type"}}`。
 *
 * 本模块只回答一个问题：**这具响应体里有没有人类可读的文案？**
 * 有就原样返回（i18n 的短语表翻译在 lib/api.ts 的 errText 里做，这里不做——
 * 保持纯净才测得动）；没有就返回空串，让调用方走自己的兜底。
 *
 * 合同：返回值永远是 string。任何形状都不允许把对象/数组漏给 React 文本节点
 * （那会在浏览器里抛 React #31，整页崩——见 tasks/NEW-FINDINGS.md N14）。
 */

/** 单个值里取文案：字符串原样；对象取 `.message` 字符串；其余（数组/数字/null）没有文案。 */
export function envelopeText(v: unknown): string {
  if (typeof v === 'string') return v;
  if (v && typeof v === 'object') {
    const msg = (v as {message?: unknown}).message;
    if (typeof msg === 'string' && msg) return msg;
  }
  return '';
}

/**
 * 从响应体里挑后端文案。优先级沿用 errText 历史 behavior：`detail` 先于 `error`
 * （FastAPI 侧 detail 更具体）。两边都取不到文案时返回空串。
 */
export function backendErrorText(data: unknown): string {
  if (typeof data === 'string') return data;
  if (!data || typeof data !== 'object') return '';
  const obj = data as {detail?: unknown; error?: unknown};
  return envelopeText(obj.detail) || envelopeText(obj.error);
}

export interface ChannelRejection {
  requestId: string | null;
  upstreamRequestId: string | null;
}

/** Recognize bounded, structured provider errors, not arbitrary quoted user text. */
export function channelCheckDetails(input: unknown): ChannelRejection | null {
  const safeId = (v: unknown): string | null =>
    typeof v === 'string' && /^[\x21-\x7e]{1,128}$/.test(v) ? v : null;
  let requestId: string | null = null;
  let upstreamRequestId: string | null = null;
  let found = false;
  const visit = (value: unknown, depth: number): void => {
    if (depth > 4 || found) return;
    if (typeof value === 'string') {
      if (value.length > 8192 || !value.trimStart().startsWith('{')) return;
      try { visit(JSON.parse(value), depth + 1); } catch { /* Keep unknown errors unchanged. */ }
      return;
    }
    if (!value || typeof value !== 'object' || Array.isArray(value)) return;
    const obj = value as Record<string, unknown>;
    requestId ??= safeId(obj.request_id) || safeId(obj.requestId);
    upstreamRequestId ??= safeId(obj.upstream_request_id);
    const code = typeof obj.code === 'string' || typeof obj.code === 'number' ? String(obj.code) : '';
    if (code === 'channel_check_rejected' || code === 'channelcheckrejected') {
      found = true;
      return;
    }
    // 11128 also represents other rejections: require channel-specific wording.
    if (code === '11128' || code === '11-128') {
      const text = [obj.msg, obj.message,
        ...(obj.displayTips && typeof obj.displayTips === 'object' ? Object.values(obj.displayTips) : [])]
        .filter((v): v is string => typeof v === 'string').join(' ').slice(0, 8192);
      if (/channel verification|unapproved channel|渠道校验|渠道校驗|通路驗證/i.test(text)) {
        found = true;
        return;
      }
    }
    for (const key of ['detail', 'error', 'message']) visit(obj[key], depth + 1);
  };
  visit(input, 0);
  return found ? {requestId, upstreamRequestId} : null;
}
