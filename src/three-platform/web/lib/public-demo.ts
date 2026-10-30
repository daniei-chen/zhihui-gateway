import type {AxiosAdapter} from 'axios';

// Compiled only for the separately published public demo, never a server proxy.
export const publicDemoAdapter: AxiosAdapter = async config => {
  const path = (config.url || '').split('?')[0];
  const platform = String(config.params?.platform || config.headers.get('X-QTG-Platform') || 'workbuddy');
  const amount = platform === 'qoder' ? 202 : platform === 'trae' ? 303 : 101;
  const day = '2026-10-30';
  const dates = (days: number) => Array.from({length: days}, (_, i) =>
    new Date(Date.UTC(2026, 9, 30 - days + 1 + i)).toISOString().slice(0, 10));
  const accounts = ['cn', 'global'].map(realm => ({file: `${platform}-${realm}.json`, uid: `demo-${realm}`,
    nickname: realm === 'cn' ? '创作工作室' : '灵感实验室', realm, expires_at: 1900000000,
    is_expired: false, remain_seconds: 9999999, credits: amount, healthy: true, disabled: false,
    cooling: false, in_pool: true, success_count: 8, source: 'demo'}));
  const usage = [{name: path.endsWith('by-key') ? '创作工作室' : '示例模型', key_id: 7, requests: 12,
    prompt_tokens: 10000, completion_tokens: 2000, credit: 1.25, cache_hit_tokens: 9400,
    cache_miss_tokens: 600, cache_hit_rate: .94, cache_hit_requests: 11, cache_observed_requests: 12,
    cache_request_hit_rate: 11 / 12, tokens_per_sec: 37.5, throughput_samples: 12}];
  const responses: Record<string, unknown> = {
    '/api/me': {username: '界面演示 · 合成数据', role: 'viewer'},
    '/api/status': {connected: true, total: 2, healthy: 2, cooling: 0, disabled: 0, in_flight_full: 0,
      accounts, realm_totals: {cn: {total: 1, healthy: 1, cooling: 0, disabled: 0},
        global: {total: 1, healthy: 1, cooling: 0, disabled: 0}}},
    '/api/accounts': {accounts, total: 2, manageable: false, pool_available: true,
      upstream: {id: null, name: '界面演示', is_default: true}},
    '/api/stats/summary': {today_requests: amount, today_tokens: amount * 100, today_credit: 3.5,
      week_credit: 9.5, total_credit: 19.5, week_requests: amount, week_tokens: amount * 100,
      total_requests: amount, total_tokens: amount * 100, active_keys: 0, top_model: '示例模型',
      failures: {today_4xx: 1, today_5xx: 0, week_4xx: 1, week_5xx: 0}, usage_health: {ok: true}},
    '/api/stats/daily': dates(14).map((date, i) => ({day: date,
      requests: [18,24,37,29,52,46,61,56,73,63,82,71,101,85][i],
      prompt_tokens: (i + 1) * 740, completion_tokens: (i + 1) * 180, failed: i % 5 === 0 ? 2 : 0})),
    '/api/stats/hourly': [{day, hour: 10, requests: amount, prompt_tokens: 1000, completion_tokens: 2000, failed: 1}],
    '/api/stats/credits': {days: 7, from: dates(7)[0], to: day,
      today: {income: 1700, expense: 1200, net: 500}, week: {income: 13900, expense: 7490, net: 6410},
      total: {income: 13900, expense: 7490, net: 6410},
      daily: dates(7).map((date, i) => ({day: date, income: [800,1400,1100,2200,3600,3100,1700][i],
        expense: [240,900,750,600,2100,1700,1200][i], net: [560,500,350,1600,1500,1400,500][i]}))},
    '/api/stats/by-model': usage, '/api/stats/by-key': usage,
    '/api/stats/upstream': {available: false, detail: '公开演示不连接真实上游'},
    '/api/logs': {items: [], total: 0, page: 1, size: 20},
    '/api/models': {models: [], source: 'static', count: 0},
    '/api/model-catalog': {models: [], source: 'none', source_label: '界面演示', via: '', errors: [],
      cached: false, cache_age: 0, summary: {total: 0, reasoning: 0, large_context: 0, max_context: 0, series: [], unique_ids: 0}},
    '/api/task-logs': {logs: [], total: 0, stats: {by_kind: {}, total: 0, total_credits: 0}, kinds: {}, collector: {}},
    '/api/task-run': {running: false, mode: '', target: '', started_at: 0, finished_at: 0,
      exit_code: null, timed_out: false, error: '', lines: [], tasks: [], available: false,
      unavailable_reason: '公开演示不执行任务', schedule: {enabled: false, hours: []}},
    '/api/task-claim-schedule': {enabled: false, hours: []},
    '/api/security/config': {enabled: true, mode: 'blacklist'},
    '/api/checkin-logs': {items: [], total: 0}, '/api/audit-logs': {items: [], total: 0},
    '/api/keys': [], '/api/tokens': [], '/api/security/rules': [], '/api/security/logs': [],
    '/api/upstreams': {items: []}, '/api/upstream/reload-state': {pending: false, can_reload: false, groups: []},
    '/api/accounts/refresh-plan': {enabled: false, interval_days: 7, hour: 0, last_run: 0, next_run: null},
    '/api/accounts/daily-plan': Object.fromEntries(['checkin','compensation','claim','full'].map(k =>
      [k, {enabled: false, hour: 0, last_run: ''}])),
    '/api/system/versions': {manager: 'v0.0.2', upstream_connected: false, upstream_accounts: 0},
    '/api/system/check-update': {checked_at: 0, cached: true,
      manager: {current: '0.0.2', latest: '0.0.2', has_update: false, error: '', repo: '', assets_ready: true}},
  };
  const allowed = (!config.method || config.method.toLowerCase() === 'get') && Object.hasOwn(responses, path);
  if (!allowed) throw Object.assign(new Error('公开演示只展示界面，不执行操作'),
    {response: {status: 403, data: {detail: '公开演示只展示界面，不执行操作'}}});
  return {data: responses[path], status: 200, statusText: 'Public Demo', headers: {}, config};
};
