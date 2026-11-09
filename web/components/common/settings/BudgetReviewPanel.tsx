'use client';

import {useEffect, useId, useRef, useState} from 'react';
import {RefreshCw} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {errText} from '@/lib/api';
import {operationalApi} from '@/lib/operational-api';
import type {BudgetReviewPage, RetentionStatus, ReviewBucket} from '@/lib/budget-review';
import {Button} from '@/components/ui/button';

const BUCKETS: ReviewBucket[] = ['all', 'new_1h', 'older_1h', 'older_7d', 'upstream_rejected', 'archived_pending'];

export function BudgetReviewPanel() {
  const {isAdmin} = useAuth();
  const {t, locale} = useI18n();
  const headingId = useId();
  const [bucket, setBucket] = useState<ReviewBucket>('all');
  const [cursor, setCursor] = useState<string>();
  const [revision, setRevision] = useState(0);
  const [page, setPage] = useState<BudgetReviewPage>();
  const [retention, setRetention] = useState<RetentionStatus>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [evidence, setEvidence] = useState<{requestId: string; text: string}>();
  const [evidenceBusy, setEvidenceBusy] = useState(false);
  const pendingEvidence = useRef<AbortController | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    pendingEvidence.current?.abort();
    setPage(undefined);
    setEvidence(undefined);
    setRetention(undefined);
    setEvidenceBusy(false);
    setError('');
    if (!isAdmin) return () => controller.abort();
    setLoading(true);
    Promise.allSettled([operationalApi.budgetReview(bucket, cursor, controller.signal), operationalApi.retention(controller.signal)])
      .then(([review, archive]) => {
        if (controller.signal.aborted) return;
        if (review.status === 'fulfilled') setPage(review.value);
        else setError(errText(review.reason));
        if (archive.status === 'fulfilled') setRetention(archive.value);
        else if (review.status === 'fulfilled') setError(errText(archive.reason));
      })
      .catch(failure => {if (!controller.signal.aborted) setError(errText(failure));})
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => {
      controller.abort();
      pendingEvidence.current?.abort();
    };
  }, [isAdmin, bucket, cursor, revision]);

  async function showEvidence(requestId: string) {
    pendingEvidence.current?.abort();
    const controller = new AbortController();
    pendingEvidence.current = controller;
    setEvidenceBusy(true);
    setEvidence(undefined);
    setError('');
    try {
      const result = await operationalApi.budgetEvidence(requestId, controller.signal);
      if (!controller.signal.aborted) setEvidence({requestId, text: JSON.stringify(result, null, 2).slice(0, 20000)});
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(errText(failure));
        const status = (failure as {response?: {status?: number}})?.response?.status;
        if (status === 401 || status === 403) {
          setPage(undefined);
          setRetention(undefined);
        }
      }
    } finally {
      if (!controller.signal.aborted) setEvidenceBusy(false);
    }
  }

  if (!isAdmin) return null;
  const archiveLabel = !retention ? 'archiveUnknown' : !retention.enabled ? 'archiveDisabled'
    : retention.sampled && retention.ok && retention.fresh ? 'archiveHealthy' : 'archiveUnknown';

  return <section className="rounded-2xl border border-border p-4 sm:p-5" aria-labelledby={headingId}>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <h2 id={headingId} className="text-sm font-semibold">{t('budgetReview.title')}</h2>
      <Button size="sm" variant="outline" disabled={loading} onClick={() => setRevision(value => value + 1)}>
        <RefreshCw className="mr-2 size-3.5" aria-hidden="true" />{t('budgetReview.refresh')}
      </Button>
    </div>
    <p className="mt-2 text-xs leading-6 text-muted-foreground">{t('budgetReview.description')}</p>
    <p className="mt-1 text-xs leading-6 text-amber-700 dark:text-amber-400">{t('budgetReview.boundary')}</p>
    <div className="my-4 flex flex-wrap items-center gap-3">
      <label htmlFor={`${headingId}-filter`} className="text-xs">{t('budgetReview.filter')}</label>
      <select id={`${headingId}-filter`} className="rounded-lg border border-border bg-background px-3 py-2 text-xs"
        value={bucket} disabled={loading} onChange={event => {setBucket(event.target.value as ReviewBucket); setCursor(undefined);}}>
        {BUCKETS.map(value => <option key={value} value={value}>{t(`budgetReview.${value}`)}</option>)}
      </select>
      <span className="text-xs text-muted-foreground">{t(`budgetReview.${archiveLabel}`)}</span>
    </div>
    {error && <p role="alert" className="my-3 text-xs text-destructive">{error}</p>}
    <div className="overflow-x-auto" aria-busy={loading}>
      <table className="w-full min-w-[620px] text-left text-xs">
        <thead><tr className="border-b border-border text-muted-foreground">
          {['request', 'key', 'created', 'classification', 'evidence'].map(name => <th key={name} scope="col" className="p-2 font-medium">{t(`budgetReview.${name}`)}</th>)}
        </tr></thead>
        <tbody>{page?.items.map(item => <tr key={item.request_id} className="border-b border-border/50">
          <td className="max-w-[200px] break-all p-2 font-mono">{item.request_id}</td>
          <td className="p-2">{item.key_id}</td>
          <td className="p-2">{new Date(item.created_at * 1000).toLocaleString(locale)}</td>
          <td className="p-2">{t(`budgetReview.class_${item.class}`)}</td>
          <td className="p-2"><Button size="sm" variant="ghost" disabled={evidenceBusy || loading}
            onClick={() => void showEvidence(item.request_id)}>{t('budgetReview.view')}</Button></td>
        </tr>)}</tbody>
      </table>
    </div>
    {!loading && page?.items.length === 0 && <p className="py-5 text-center text-xs text-muted-foreground">{t('budgetReview.empty')}</p>}
    <div className="mt-4 flex justify-end gap-2">
      <Button size="sm" variant="outline" disabled={!cursor || loading} onClick={() => setCursor(undefined)}>{t('budgetReview.first')}</Button>
      <Button size="sm" variant="outline" disabled={!page?.next_cursor || loading} onClick={() => setCursor(page?.next_cursor ?? undefined)}>{t('budgetReview.next')}</Button>
    </div>
    {evidence && <div className="mt-4 rounded-xl bg-muted/50 p-3">
      <h3 className="break-all text-xs font-medium">{t('budgetReview.evidence')}: {evidence.requestId}</h3>
      <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all text-[11px]">{evidence.text}</pre>
    </div>}
  </section>;
}
