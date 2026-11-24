'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {ChevronLeft, ChevronRight, History, RefreshCw} from 'lucide-react';
import {PortalError, portalFetch, portalTime, type PortalRequest} from '@/lib/portal-api';
import {creditUnverified, requestCredit, requestLatency, requestStatus, requestTokens} from '@/lib/portal-display';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useT} from '@/lib/i18n/provider';

type Page = {items: PortalRequest[]; total: number; page: number};

export default function PortalRequests({onUnauthorized}: {onUnauthorized: () => void}) {
  const t = useT();
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const active = useRef<{controller: AbortController; promise: Promise<void>} | null>(null);
  const invalidate = useCallback(() => {
    sequence.current++; active.current?.controller.abort(); active.current = null;
  }, []);
  const read = useCallback((background = false) => {
    if (background && active.current) return active.current.promise;
    invalidate();
    const generation = sequence.current;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && generation === sequence.current;
    setPending(true);
    const promise = Promise.resolve().then(async () => {
      if (!current()) return;
      try {
        const result = await portalFetch<Page>(`portal/requests?page=${page}`, {signal: controller.signal});
        if (current()) {setData(result); setError('');}
      } catch (err) {
        if (!current()) return;
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {
          setData(null); onUnauthorized();
        } else setError(err instanceof Error ? err.message : t('portal.requests.errLoad'));
      } finally {
        if (current()) {setPending(false); active.current = null;}
      }
    });
    active.current = {controller, promise};
    return promise;
  }, [page, onUnauthorized, invalidate, t]);
  useEffect(() => {void read(); return invalidate;}, [read, invalidate]);
  useHeartbeat(() => read(true), 20000);
  const visible = data?.page === page ? data : null;
  const previousDisabled = pending || page <= 1;
  const nextDisabled = pending || !visible || page >= 10000 || page * 30 >= visible.total;

  return <section className="portal-panel portal-history" aria-labelledby="portal-history-title" aria-busy={pending}>
    <div className="portal-panel-heading"><div><h2 id="portal-history-title">{t('portal.requests.title')}</h2>
      <p className="portal-small">{t('portal.requests.desc')}</p></div>
      <button className="portal-button" disabled={pending} onClick={() => void read()}><RefreshCw size={14} />{t('portal.requests.refresh')}</button></div>
    {error && <div className="portal-error" role="alert">{error}{visible && ` · ${t('portal.requests.stale')}`}
      <button className="portal-button" disabled={pending} onClick={() => void read()}>{t('portal.requests.retry')}</button></div>}
    {!visible ? <p className="portal-empty" role="status">{pending ? t('portal.requests.loading') : t('portal.requests.unavailable')}</p>
      : !visible.items.length ? <div className="portal-empty"><History size={25} /><p>{t('portal.requests.empty')}</p><span className="portal-small">{t('portal.requests.emptyHint')}</span></div>
      : <><nav className="portal-history-quick-pages" aria-label={t('portal.requests.quickPages')}><span>{t('portal.requests.summary', {total: visible.total, page: visible.page})}</span>
        <button className="portal-button" aria-label={t('portal.requests.prevPageLabel')} disabled={previousDisabled} onClick={() => setPage(page - 1)}><ChevronLeft size={14} /></button>
        <button className="portal-button" aria-label={t('portal.requests.nextPageLabel')} disabled={nextDisabled} onClick={() => setPage(page + 1)}><ChevronRight size={14} /></button></nav>
      <div className="portal-request-list"><table className="portal-table portal-request-table"><caption className="sr-only">{t('portal.requests.caption', {page: visible.page})}</caption>
        <thead><tr><th scope="col">{t('portal.requests.colTime')}</th><th scope="col">{t('portal.requests.colModel')}</th><th scope="col">{t('portal.requests.colStatus')}</th><th scope="col">{t('portal.requests.colTokens')}</th><th scope="col">{t('portal.requests.colLatency')}</th><th scope="col">{t('portal.requests.colCredit')}</th></tr></thead>
        <tbody>{visible.items.map((row, index) => <tr key={`${row.ts}-${index}`}>
          <td data-label={t('portal.requests.colTime')}>{portalTime(row.ts)}</td><td data-label={t('portal.requests.colModel')} className="portal-request-model">{row.model}</td>
          <td data-label={t('portal.requests.colStatus')}><span className={`portal-request-status ${requestStatus(row.status, t).tone}`}>{requestStatus(row.status, t).label}<small>{row.status}</small></span></td>
          <td data-label={t('portal.requests.colTokens')}>{requestTokens(row.prompt_tokens, row.completion_tokens, t)}</td>
          <td data-label={t('portal.requests.colLatency')}>{requestLatency(row.latency_ms, t)}</td>
          <td data-label={t('portal.requests.colCredit')} className="portal-request-credit">{requestCredit(row.credit, t)}{creditUnverified(row.credit) && <span className="sr-only">{t('portal.requests.notFree')}</span>}</td>
        </tr>)}</tbody></table></div></>}
    <nav className="portal-actions portal-ops-pagination" aria-label={t('portal.requests.pagination')}>
      <span className="portal-small" role="status">{visible ? t('portal.requests.summary', {total: visible.total, page: visible.page}) : t('portal.requests.pageOnly', {page})}{pending ? ` · ${t('portal.requests.reading')}` : ''}</span>
      <button className="portal-button" disabled={previousDisabled} onClick={() => setPage(page - 1)}>{t('portal.requests.prev')}</button>
      <button className="portal-button" disabled={nextDisabled} onClick={() => setPage(page + 1)}>{t('portal.requests.next')}</button>
    </nav>
  </section>;
}
