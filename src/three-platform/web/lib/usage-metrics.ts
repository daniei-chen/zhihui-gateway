import type {UsageBreakdown} from './types';

const validCount = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0;

export function usageTokenTotal(row: {total_tokens?: number | null; prompt_tokens?: number | null; completion_tokens?: number | null}): number | null {
  if (row.total_tokens !== undefined) return validCount(row.total_tokens) ? row.total_tokens : null;
  if (!validCount(row.prompt_tokens) || !validCount(row.completion_tokens)) return null;
  const total = row.prompt_tokens + row.completion_tokens;
  return validCount(total) ? total : null;
}

export function formatCacheHitRate(row: Pick<UsageBreakdown, 'cache_hit_rate' | 'cache_hit_tokens' | 'cache_miss_tokens'>): string {
  let rate = row.cache_hit_rate;
  if (rate === undefined) {
    const hit = row.cache_hit_tokens;
    const miss = row.cache_miss_tokens;
    if (!validCount(hit) || !validCount(miss) || hit + miss <= 0) return '—';
    rate = hit / (hit + miss);
  }
  return validCount(rate) && rate <= 1 ? `${Math.round(rate * 100)}%` : '—';
}

export function formatOutputRate(value: unknown): string {
  return validCount(value) && value > 0 ? value.toFixed(1) : '—';
}
