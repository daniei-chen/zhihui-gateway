import configurationFields from './configuration-fields.json';

export interface RecordedOutcome {
  request_id: string;
  ts: number | null;
  terminal_state: string | null;
  error_code: string | null;
  error_stage: string | null;
  error_source?: string | null;
  http_status: number | null;
  latency_ms: number | null;
  endpoint: string | null;
  release_id: string | null;
  platform?: string | null;
  attempt_count?: number;
  traffic_class?: string;
}

export interface OutcomePage {
  total: number;
  page: number;
  size: number;
  error_code_available: boolean;
  items: RecordedOutcome[];
}

export interface AttemptExplanation {
  request_id: string;
  explainable: boolean;
  reason: string;
  boundary: Record<string, string[]>;
  attempt_count: number;
  attempt_count_source: 'attempt_events' | 'absent';
  request: Omit<RecordedOutcome, 'request_id'> & {source: 'request_outcomes' | 'absent'};
  attempts: {
    attempt: number;
    account_uid: string | null;
    resolved_model: string | null;
    realm: string | null;
    duration_ms: number | null;
    reached_upstream: boolean;
    terminal_phase: string | null;
    last_phase: string | null;
    phase_sequence: string[];
    phases: {phase: string; ts: number; status: number | null}[];
    billing: {billing_fact: string; billing_source: string; evidence: string | null} | null;
  }[];
  timeline: {ts: number; attempt: number; phase: string; status: number | null; elapsed_ms: number | null}[];
  evidence: {sources: string[]; attempt_events: number; attempt_billing_facts: number; request_outcomes: number; first_event_ts: number | null; note: string};
}

export interface OutcomeStats {
  ok: boolean;
  window: {days: number; since: number; until: number; realm: string | null};
  requests: number;
  classes: Record<string, number>;
  by_traffic_class: Record<string, number>;
  error_code_top: {error_code: string; error_source: string | null; count: number}[];
  error_code_coverage: {with_code: number | null; without_code: number | null; total: number | null; ratio: number | null; note: string};
  backup: {status: string; age_seconds: number | null; reason: string | null};
  certificate: {status: string; days_left: number | null; reason: string | null};
  notes: string[];
}

export interface ConfigurationSnapshot {
  ok: boolean;
  configuration: {schema: string; service: string; settings: Record<string, {value: unknown; source: string; type: string; unit?: string | null}>};
  active: {upstream_timeout_s: number; drain_timeout_s: number; http_pool_connections: number; http_pool_keepalive: number};
  note: string;
}

export interface ConfigurationDrift {
  ok: boolean;
  release: string;
  generated_at: number;
  summary: {rows: number; high: number; medium: number; ok: number; unit_readable: boolean; cross_checks_ok: boolean};
  settings: {
    name: string; registered: boolean; sensitive: boolean;
    schema_default: unknown; source_default: unknown; source_expression: string | null;
    declared: unknown; process: unknown; flags: string[]; risk: string;
  }[];
  sources: {declared: {available: boolean; reason: string | null}};
}

export function evidenceValue(value: unknown, unknownLabel = 'Unknown'): string {
  if (value === undefined || value === null || value === '') return unknownLabel;
  return typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'
    ? String(value) : unknownLabel;
}

export function evidenceAccess(error: unknown): 'forbidden' | 'unavailable' | 'stale' | null {
  if (!error) return null;
  const status = (error as {response?: {status?: number}}).response?.status;
  return status === 401 || status === 403 ? 'forbidden' : status === 404 || status === 501 ? 'unavailable' : 'stale';
}

/** Defense in depth: never render arbitrary server JSON or URL credentials. */
export function safeConfigurationValue(value: unknown, sensitive = false, registered = true): string {
  if (sensitive || !registered || (value !== null && typeof value === 'object')) return '[redacted]';
  if (typeof value === 'string' && value.includes('://')) {
    try { const url = new URL(value); return url.origin; } catch { return '[redacted]'; }
  }
  return evidenceValue(value);
}

/** Local schema metadata adds protection even if a backend regresses its redaction. */
export function safeNamedConfigurationValue(name: string, value: unknown, sensitive = false, registered = true): string {
  const spec = (configurationFields as Record<string, {sensitive: boolean}>)[name];
  return safeConfigurationValue(value, sensitive || spec?.sensitive, registered && !!spec);
}
