'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch, portalTime} from '@/lib/portal-api';
import {creditInputUnits, exactCredit, operationLabel, type OperationPage, type WalletPending, type WalletDebt, type WalletHistory} from '@/lib/portal-operations';
import {useT} from '@/lib/i18n/provider';

type Data = {pending: OperationPage<WalletPending>; debts: OperationPage<WalletDebt>; history: OperationPage<WalletHistory>};
type Target = {kind: 'resolve'; row: WalletPending} | {kind: 'repay' | 'waive'; row: WalletDebt};
type Submission = {path: string; body: object};
const emptyPage = <T,>(): OperationPage<T> => ({items: [], total: 0, page: 1, page_size: 30});

export default function WalletOperations({onChanged}: {onChanged: () => Promise<void>}) {
  const t = useT();
  const [data, setData] = useState<Data>({pending: emptyPage(), debts: emptyPage(), history: emptyPage()});
  const [pages, setPages] = useState({pending: 1, debts: 1, history: 1});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [target, setTarget] = useState<Target | null>(null);
  const seq = useRef(0);
  const load = useCallback(async (signal?: AbortSignal) => {
    const generation = ++seq.current;
    setLoading(true);
    try {
      const [pending, debts, history] = await Promise.all([
        portalFetch<OperationPage<WalletPending>>(`portal-admin/wallet-pending?page=${pages.pending}`, {signal}),
        portalFetch<OperationPage<WalletDebt>>(`portal-admin/wallet-debts?page=${pages.debts}`, {signal}),
        portalFetch<OperationPage<WalletHistory>>(`portal-admin/wallet-history?page=${pages.history}`, {signal}),
      ]);
      if (signal?.aborted || generation !== seq.current) return;
      setData({pending, debts, history}); setError('');
    } catch (err) {
      if (!signal?.aborted && generation === seq.current) {
        setError(err instanceof Error ? err.message : t('portal.wallet.errLoad'));
        setTarget(null);
      }
    } finally { if (!signal?.aborted && generation === seq.current) setLoading(false); }
  }, [pages, t]);
  const invalidate = useCallback(() => {seq.current++;}, []);
  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve().then(() => {if (!controller.signal.aborted) return load(controller.signal);});
    return () => { controller.abort(); invalidate(); };
  }, [load, invalidate]);
  async function changed(message: string) {
    setTarget(null); setNotice(message);
    await Promise.all([load(), onChanged()]);
  }
  function pagination(scope: keyof Data) {
    const page = data[scope];
    return <div className="portal-actions portal-ops-pagination"><span className="portal-small">{t('portal.wallet.summary', {total: page.total, page: pages[scope]})}</span>
      <button className="portal-button" disabled={loading || pages[scope] <= 1} onClick={() => setPages(current => ({...current, [scope]: current[scope] - 1}))}>{t('portal.wallet.prev')}</button>
      <button className="portal-button" disabled={loading || pages[scope] * page.page_size >= page.total} onClick={() => setPages(current => ({...current, [scope]: current[scope] + 1}))}>{t('portal.wallet.next')}</button></div>;
  }
  return <section aria-labelledby="wallet-ops-title" className="portal-ops">
    <div className="portal-account"><div><p className="portal-eyebrow">Wallet controls</p><h2 id="wallet-ops-title">{t('portal.wallet.title')}</h2>
      <p className="portal-small">{t('portal.wallet.desc')}</p></div>
      <button className="portal-button" disabled={loading || !!target} onClick={() => void load()}>{loading ? t('portal.wallet.loading') : t('portal.wallet.refresh')}</button></div>
    {notice && <p className="portal-notice" role="status">{notice}</p>}
    {error ? <p className="portal-error" role="alert">{error} · {t('portal.wallet.stale')}</p> : <>
      <div className="portal-panel"><h3>{t('portal.wallet.pendingTitle')} <span className="portal-badge pending">{data.pending.total}</span></h3>
        <p className="portal-small">{t('portal.wallet.pendingDesc')}</p>
        {data.pending.items.map(row => <article className="portal-account" key={row.request_id}><div className="portal-ops-detail"><strong>{row.username}</strong>
          <p className="portal-small portal-ops-id">{t('portal.wallet.requestLine', {request: row.request_id, key: row.key_id})}</p>
          <p className="portal-small">{portalTime(row.created_at)} · {t('portal.wallet.frozenLine', {held: exactCredit(row.held_units)})} · {row.request?.model || t('portal.wallet.modelMissing')}</p>
          <span className="portal-badge pending">{row.state === 'unknown' ? t('portal.wallet.stateUnknown') : row.state === 'reserved' ? t('portal.wallet.stateReserved') : t('portal.wallet.stateInFlight')}</span>
          <p className="portal-small">{row.blocked_reason || (row.end_confirmed ? t('portal.wallet.endConfirmed') : t('portal.wallet.processExited'))}{row.receipt_units !== null ? ` ${t('portal.wallet.hasReceipt', {credit: exactCredit(row.receipt_units)})}` : ''}</p></div>
          <button className="portal-button" disabled={loading || !row.can_resolve} onClick={() => setTarget({kind: 'resolve', row})}>{t('portal.wallet.resolve')}</button></article>)}
        {!loading && !data.pending.items.length && <p className="portal-empty">{t('portal.wallet.pendingEmpty')}</p>}{pagination('pending')}
      </div>
      <div className="portal-panel"><h3>{t('portal.wallet.debtsTitle')} <span className="portal-badge pending">{data.debts.total}</span></h3>
        <p className="portal-small">{t('portal.wallet.debtsDesc')}</p>
        <p className="portal-small">{t('portal.wallet.debtsDesc2')}</p>
        {data.debts.items.map(row => <article className="portal-account" key={row.owner_id}><div><strong>{row.username}</strong>
          <p className="portal-small">{t('portal.wallet.debtLine', {amount: exactCredit(row.amount_units), repayable: exactCredit(row.repayable_units)})} · {row.enabled ? t('portal.wallet.userEnabled') : t('portal.wallet.userDisabled')}</p></div>
          <div className="portal-actions"><button className="portal-button" disabled={loading || row.repayable_units <= 0} onClick={() => setTarget({kind: 'repay', row})}>{t('portal.wallet.repay')}</button>
            <button className="portal-button" disabled={loading} onClick={() => setTarget({kind: 'waive', row})}>{t('portal.wallet.waive')}</button></div></article>)}
        {!loading && !data.debts.items.length && <p className="portal-empty">{t('portal.wallet.debtsEmpty')}</p>}{pagination('debts')}
      </div>
      <div className="portal-panel"><h3>{t('portal.wallet.historyTitle')}</h3><p className="portal-small">{t('portal.wallet.historyDesc')}</p>
        {data.history.items.map(row => <article className="portal-account" key={`${row.kind}:${row.id}`}><div className="portal-ops-detail">
          <strong>{t('portal.wallet.historyLine', {username: row.username, kind: t(operationLabel(row.kind)), credit: exactCredit(row.amount_units)})}</strong>
          <p className="portal-small">{portalTime(row.created_at)} · {t('portal.wallet.actorLine', {actor: row.actor})}{row.before_units !== null && row.after_units !== null ? ` · ${t('portal.wallet.beforeAfter', {before: exactCredit(row.before_units), after: exactCredit(row.after_units)})}` : ''}</p>
          <p className="portal-small portal-ops-id">{t('portal.wallet.idLine', {id: row.id})}</p><p className="portal-small">{t('portal.wallet.reasonLine', {reason: row.reason})}</p>{row.expires_at !== null && <p className="portal-small">{t('portal.wallet.grantExpiry', {time: portalTime(row.expires_at)})}</p>}{row.evidence && <p className="portal-small">{t('portal.wallet.evidenceLine', {evidence: row.evidence})}</p>}</div></article>)}
        {!loading && !data.history.items.length && <p className="portal-empty">{t('portal.wallet.historyEmpty')}</p>}{pagination('history')}
      </div>
    </>}
    {target && <ResolutionDialog target={target} close={() => setTarget(null)} saved={changed} />}
  </section>;
}

function ResolutionDialog({target, close, saved}: {target: Target; close: () => void; saved: (message: string) => Promise<void>}) {
  const t = useT();
  const [credit, setCredit] = useState(target.kind === 'resolve' ? target.row.receipt_units === null ? '' : exactCredit(target.row.receipt_units)
    : exactCredit(target.kind === 'repay' ? Math.min(target.row.amount_units, target.row.repayable_units) : target.row.amount_units));
  const [reason, setReason] = useState('');
  const [evidence, setEvidence] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState<Submission | null>(null);
  const actionId = useRef('');
  const inFlight = useRef(false);
  const amount = creditInputUnits(credit);
  const maximum = target.kind === 'resolve' ? null : target.kind === 'repay' ? Math.min(target.row.amount_units, target.row.repayable_units) : target.row.amount_units;
  const valid = amount !== null && (target.kind === 'resolve' || amount > 0) && (maximum === null || amount <= maximum)
    && reason.trim().length >= 8 && evidence.trim().length >= 8 && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current || !valid) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      if (!actionId.current) actionId.current = crypto.randomUUID().replaceAll('-', '');
      const operation = submitted || (target.kind === 'resolve' ? {
        path: `portal-admin/wallet-pending/${encodeURIComponent(target.row.request_id)}/resolve`,
        body: {credit, reason: reason.trim(), evidence: evidence.trim()},
      } : {
        path: `portal-admin/wallet-debts/${encodeURIComponent(target.row.owner_id)}/adjust`,
        body: {action_id: actionId.current, kind: target.kind, credit, expected_units: target.row.amount_units, reason: reason.trim(), evidence: evidence.trim()},
      });
      setSubmitted(operation);
      const result = await portalFetch<{message: string}>(operation.path, {method: 'POST', body: JSON.stringify(operation.body)});
      await saved(result.message);
    } catch (err) { setError(err instanceof Error ? err.message : t('portal.wallet.submitUncertain')); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>{t(operationLabel(target.kind))} · {target.row.username}</DialogTitle><DialogDescription>
        {target.kind === 'resolve' ? t('portal.wallet.resolveDesc') : target.kind === 'repay'
          ? t('portal.wallet.repayDesc') : t('portal.wallet.waiveDesc')}
      </DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)}>
        <p className="portal-small">{target.kind === 'resolve'
          ? t('portal.wallet.resolveTarget', {request: target.row.request_id, held: exactCredit(target.row.held_units)})
          : t('portal.wallet.repayTarget', {amount: exactCredit(target.row.amount_units), repayable: exactCredit(target.row.repayable_units)})}</p>
        <label className="portal-field">{target.kind === 'resolve' ? t('portal.wallet.amountResolve') : t('portal.wallet.amountRepay')}<input className="portal-input" inputMode="decimal" required maxLength={17} value={credit} onChange={event => setCredit(event.target.value)} disabled={busy || !!submitted || (target.kind === 'resolve' && target.row.receipt_units !== null)} /></label>
        {amount === null && <p className="portal-small">{t('portal.wallet.amountHint')}</p>}
        {amount !== null && maximum !== null && amount > maximum && <p className="portal-error">{t('portal.wallet.amountTooLarge')}</p>}
        <label className="portal-field">{t('portal.wallet.reasonLabel')}<textarea className="portal-input" required minLength={8} maxLength={500} rows={2} value={reason} onChange={event => setReason(event.target.value)} disabled={busy || !!submitted} /></label>
        <label className="portal-field">{t('portal.wallet.evidenceLabel')}<textarea className="portal-input" required minLength={8} maxLength={500} rows={2} value={evidence} onChange={event => setEvidence(event.target.value)} disabled={busy || !!submitted} /></label>
        <p className="portal-small">{t('portal.wallet.evidenceHint')}</p>
        <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy || !!submitted} />{t('portal.wallet.confirm')}</label>
        {error && <p className="portal-error" role="alert">{error}{t('portal.wallet.retryNote')}</p>}
        <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>{t('portal.wallet.cancel')}</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? t('portal.wallet.busy') : submitted ? t('portal.wallet.retry') : t('portal.wallet.submit')}</button></div>
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
