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
