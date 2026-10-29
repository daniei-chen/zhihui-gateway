const state = {mode: 'success'};
const outcome = {request_id: 'fixture-execution-001', ts: 1793200000, endpoint: '/v1/messages', method: 'POST', protocol: 'anthropic', http_status: 502, terminal_state: 'failed', error_stage: 'upstream', error_code: 'upstream.timeout', error_source: 'manager', release_id: 'fixture-release', client_request_id: 'caller-id-not-execution', latency_ms: 1000, platform: 'workbuddy', account: 'synthetic-account', attempt_count: 1, traffic_class: 'inference'};

function fixture(url) {
  const path = url.pathname;
  if (!['/api/logs/outcomes', '/api/logs/attempts/explain', '/api/stats/outcomes', '/api/system/configuration', '/api/system/config'].includes(path)) return undefined;
  if (state.mode === 'forbidden') return [403, {detail: 'Local authorization failure fixture'}];
  if (state.mode === 'unavailable') return [501, {detail: 'Local unsupported endpoint fixture'}];
  if (state.mode === 'stale') return [503, {detail: 'Local offline evidence fixture'}];
  if (path === '/api/logs/outcomes') return {items: [{...outcome, platform: url.searchParams.get('upstream_platform')}], total: 1, page: 1, size: 10, error_code_available: true};
  if (path === '/api/logs/attempts/explain') return {
    request_id: url.searchParams.get('request_id'), explainable: false, reason: 'upstream_does_not_expose_selection_snapshot', boundary: {known: ['selected_account'], unknown: ['candidate_set']}, attempt_count: 1, attempt_count_source: 'attempt_events',
    request: {...outcome, source: 'request_outcomes'},
    attempts: [{attempt: 1, account_uid: 'synthetic-account', resolved_model: 'synthetic-model', realm: 'cn', config_hash: null, phase_sequence: ['selected', 'upstream_request'], phases: [{phase: 'selected', ts: 1793200000, status: null}, {phase: 'upstream_request', ts: 1793200001, status: null}], started_at: 1793200000, ended_at: 1793200001, duration_ms: 1000, phase_gaps_ms: [1000], reached_upstream: true, terminal_phase: null, last_phase: 'upstream_request', billing: null}],
    timeline: [{ts: 1793200000, attempt: 1, phase: 'selected', status: null, elapsed_ms: 0}, {ts: 1793200001, attempt: 1, phase: 'upstream_request', status: null, elapsed_ms: 1000}],
    evidence: {sources: ['attempt_events', 'request_outcomes', 'attempt_billing_facts'], attempt_events: 2, attempt_billing_facts: 0, request_outcomes: 1, first_event_ts: 1793200000, note: 'Synthetic recorded facts only'},
  };
  if (path === '/api/stats/outcomes') return {
    ok: true, window: {days: Number(url.searchParams.get('days')), realm: url.searchParams.get('realm'), since: 1793200000, until: 1793300000}, requests: 18,
    classes: {completed: 14, failed: 2, cancelled: 1, unknown: 1}, by_terminal_state: {completed: 14, failed: 2, client_closed: 1, legacy: 1}, by_traffic_class: {inference: 14, management: 3, probe: 1}, error_code_top: [{error_code: 'upstream.timeout', error_source: 'manager', count: 2}], error_code_coverage: {with_code: null, without_code: null, total: null, ratio: null, note: 'Synthetic legacy column unavailable'}, by_release: [{release_id: 'fixture-release', requests: 18, failed: 2, cancelled: 1}],
    backup: {status: 'unknown', age_seconds: null, reason: 'Synthetic backup evidence absent'}, certificate: {status: 'unknown', days_left: null, reason: 'Synthetic certificate evidence absent'}, notes: ['Synthetic unknown values are not zero.'],
  };
  if (path === '/api/system/configuration') return {ok: true, configuration: {schema: 'synthetic', service: 'manager', settings: {
    WB_HTTP_POOL_CONNECTIONS: {value: 64, source: 'process_environment', type: 'integer'},
    WB_ADMIN_PASSWORD: {value: 'synthetic-secret-must-not-render', source: 'process_environment', type: 'string'},
    UNKNOWN_FUTURE_SETTING: {value: 'synthetic-secret-must-not-render', source: 'default', type: 'string'},
  }}, active: {upstream_timeout_s: 180, drain_timeout_s: 30, http_pool_connections: 64, http_pool_keepalive: 32, managed_launch: 'synthetic-secret-must-not-render', unexpected_sensitive: 'synthetic-secret-must-not-render'}, note: 'Synthetic startup snapshot; not every upstream override.'};
  if (path === '/api/system/config') return {ok: true, release: 'fixture-release', generated_at: 1793200000, sources: {declared: {available: false, reason: 'Synthetic unit not readable'}}, summary: {rows: 3, settings: 3, high: 1, medium: 0, ok: 2, unit_readable: false, cross_checks_ok: true}, settings: [
    {name: 'WB_HTTP_POOL_CONNECTIONS', registered: true, sensitive: false, schema_default: 32, source_default: 32, source_expression: null, declared: null, process: '64', flags: ['process_only', 'override'], risk: 'high'},
    {name: 'WB_ADMIN_PASSWORD', registered: true, sensitive: true, schema_default: 'synthetic-secret-must-not-render', source_default: 'synthetic-secret-must-not-render', source_expression: null, declared: 'synthetic-secret-must-not-render', process: 'synthetic-secret-must-not-render', flags: [], risk: 'ok'},
    {name: 'UNKNOWN_FUTURE_SETTING', registered: false, sensitive: false, schema_default: 'synthetic-secret-must-not-render', source_default: 'synthetic-secret-must-not-render', source_expression: null, declared: 'synthetic-secret-must-not-render', process: 'synthetic-secret-must-not-render', flags: ['unregistered'], risk: 'high'},
  ]};
}

module.exports = {state, fixture};
