export type ReviewBucket = 'all' | 'new_1h' | 'older_1h' | 'older_7d' | 'upstream_rejected' | 'archived_pending';

export interface BudgetReviewItem {
  request_id: string;
  key_id: number;
  created_at: number;
  age_seconds: number;
  state: 'unknown';
  class: string;
  has_request_log: boolean;
  has_provider_usage: boolean;
  wallet_frozen: null;
}

export interface BudgetReviewPage {
  items: BudgetReviewItem[];
  next_cursor: string | null;
  scanned_rows: number;
  usage_unknown_is_zero: false;
  scope: 'main-supplier-usage-not-customer-wallet';
}

export interface RetentionStatus {
  enabled: boolean;
  sampled: boolean;
  ok: boolean | null;
  fresh: boolean;
  age_seconds: number | null;
  deleted_rows: number;
}
