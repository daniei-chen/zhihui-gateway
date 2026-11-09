import axios from 'axios';
import {BASE_PATH} from './base-path';
import type {Platform} from './platform-store';
import type {Realm} from './realm-context';
import type {AttemptExplanation, ConfigurationDrift, ConfigurationSnapshot, OutcomePage, OutcomeStats} from './operational-evidence';
import type {BudgetReviewPage, RetentionStatus, ReviewBucket} from './budget-review';

// These are Manager ledger/configuration endpoints, not QTG compatibility routes.
// upstream_platform is a ledger filter; platform=qoder would rewrite the endpoint.
const manager = axios.create({baseURL: BASE_PATH, withCredentials: true, timeout: 20000,
  headers: {'X-QTG-Platform': 'workbuddy'}});

async function read<T>(url: string, params?: Record<string, unknown>): Promise<T> {
  return (await manager.get<T>(url, {params})).data;
}

export const operationalApi = {
  budgetReview: (bucket: ReviewBucket, cursor?: string, signal?: AbortSignal) => manager.get<BudgetReviewPage>('/api/budgets/review', {
    params: {bucket, cursor, limit: 10}, signal,
  }).then(response => response.data),
  budgetEvidence: (requestId: string, signal?: AbortSignal) => manager.get<Record<string, unknown>>('/api/budgets/review/evidence', {
    params: {request_id: requestId}, signal,
  }).then(response => response.data),
  retention: (signal?: AbortSignal) => manager.get<RetentionStatus>('/api/logs/retention/status', {signal}).then(response => response.data),
  outcomes: (platform: Platform, page: number, errorCode: string, days: number) => read<OutcomePage>('/api/logs/outcomes', {
    upstream_platform: platform, page, size: 10, days, error_code: errorCode || undefined,
  }),
  explain: (requestId: string) => read<AttemptExplanation>('/api/logs/attempts/explain', {request_id: requestId}),
  stats: (days: number, realm: Realm) => read<OutcomeStats>('/api/stats/outcomes', {days, realm}),
  configuration: () => read<ConfigurationSnapshot>('/api/system/configuration'),
  drift: () => read<ConfigurationDrift>('/api/system/config'),
};
