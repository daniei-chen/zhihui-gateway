import {http} from './api';

export interface StewardPolicy {
  revision: number;
  enabled: boolean;
  recipients: string[];
  send_recovery: boolean;
  ai_analysis: boolean;
  model: string;
  interval_seconds: number;
  consecutive_failures: number;
  disk_warning_percent: number;
  disk_critical_percent: number;
  memory_warning_percent: number;
  failure_warning_percent: number;
  min_requests: number;
  latency_warning_ms: number;
  budget_unknown_warning: number;
}

export interface StewardSnapshot {
  observed_at: number;
  resources: {disk: {used_percent: number; free_bytes: number}; memory: {used_percent: number | null}};
  traffic: {requests: number; failed: number; failure_percent: number | null; p95_total_latency_ms: number | null};
  budget: {unknown_count: number};
  release: {version: string; release_id: string};
  ledger: {state: string; pending: number; dead: number};
  backup: {status: string};
  platforms: {name: string; state: 'up' | 'down' | 'unknown'; pool: {total: number; enabled: number | null; credits_remain: number | null} | null}[];
}

export interface StewardStatus {
  available: boolean;
  stale: boolean;
  snapshot: StewardSnapshot | null;
  age_seconds: number | null;
  capabilities: {smtp_configured: boolean; model_configured: boolean};
  monitor: {at: number; collector: string} | null;
  can_chat: boolean;
  read_only_tools: boolean;
}

export interface StewardIncident {
  id: number; rule: string; severity: string; state: string; title: string;
  detail: string; first_seen: number; last_seen: number; resolved_at: number | null;
}

export interface StewardDelivery {
  id: number; event: string; status: string; attempts: number; created_at: number;
  finished_at: number | null; error_code: string;
  recipient?: string;
}

export interface StewardAnalysis {
  id: number; incident_id: number; event: string; status: string;
  created_at: number; finished_at: number | null; error_code: string;
}

export interface StewardSettings {
  policy: StewardPolicy; can_edit: boolean; recipient_count: number;
  capabilities: StewardStatus['capabilities'];
}

export interface ConversationMessage {role: 'user' | 'assistant'; content: string}

export const stewardApi = {
  status: () => http.get<StewardStatus>('/api/steward/status').then(r => r.data),
  alerts: () => http.get<{incidents: StewardIncident[]; deliveries: StewardDelivery[]; analyses: StewardAnalysis[];
    queue: {counts: Record<string, number>; oldest_pending_at: number | null}}>('/api/steward/alerts').then(r => r.data),
  settings: () => http.get<StewardSettings>('/api/steward/settings').then(r => r.data),
  save: (policy: StewardPolicy) => http.put<{ok: boolean; policy: StewardPolicy}>('/api/steward/settings', policy).then(r => r.data),
  test: () => http.post<{queued: number[]; accepted_by_smtp: boolean}>('/api/steward/test-mail').then(r => r.data),
  chat: (question: string, history: ConversationMessage[], signal?: AbortSignal) =>
    http.post<{answer: string; model: string; tools: string[]; observed_at: number; read_only: boolean}>(
      '/api/steward/chat', {question, history}, {timeout: 90000, signal}).then(r => r.data),
};
