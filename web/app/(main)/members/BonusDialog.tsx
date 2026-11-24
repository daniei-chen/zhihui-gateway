'use client';

import {useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch, points, portalTime} from '@/lib/portal-api';
import {creditInputUnits} from '@/lib/portal-operations';
import {useT} from '@/lib/i18n/provider';

type Submission = {action_id: string; credit: string; valid_days: number; reason: string};
export default function BonusDialog({user, close, saved}: {
  user: {id: string; username: string}; close: () => void; saved: () => Promise<void>;
}) {
  const t = useT();
  const [credit, setCredit] = useState('');
  const [days, setDays] = useState('30');
  const [reason, setReason] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState<Submission | null>(null);
  const [result, setResult] = useState<{amount_units: number; expires_at: number; message: string} | null>(null);
  const actionId = useRef('');
  const inFlight = useRef(false);
  const amount = creditInputUnits(credit);
  const validDays = /^(?:[1-9]\d{0,2})$/.test(days) && Number(days) <= 365;
  const valid = amount !== null && amount > 0 && amount <= 1_000_000_000_000 && validDays && reason.trim().length >= 8 && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || inFlight.current || result) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      if (!actionId.current) actionId.current = crypto.randomUUID().replaceAll('-', '');
      const body = submitted || {action_id: actionId.current, credit, valid_days: Number(days), reason: reason.trim()};
      setSubmitted(body);
      const response = await portalFetch<NonNullable<typeof result>>(`portal-admin/users/${encodeURIComponent(user.id)}/bonus`, {method: 'POST', body: JSON.stringify(body)});
      setResult(response);
      await saved();
    } catch (err) { setError(err instanceof Error ? err.message : t('portal.members.bonusUncertain')); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>{t('portal.members.bonusTitle', {username: user.username})}</DialogTitle><DialogDescription>{t('portal.members.bonusDesc')}</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)}>
        {result ? <><p className="portal-notice" role="status">{result.message}<br />{t('portal.members.bonusGranted', {amount: points(result.amount_units), expires: portalTime(result.expires_at)})}</p>
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>{t('portal.members.finish')}</button></> : <>
          <label className="portal-field">{t('portal.members.bonusAmount')}<input className="portal-input" inputMode="decimal" required maxLength={17} value={credit} onChange={event => setCredit(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">{t('portal.members.bonusAmountHint')}</p>
          <label className="portal-field">{t('portal.members.bonusDays')}<input className="portal-input" inputMode="numeric" required maxLength={3} value={days} onChange={event => setDays(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">{t('portal.members.bonusDaysHint')}</p>
          <label className="portal-field">{t('portal.members.bonusReason')}<textarea className="portal-input" required minLength={8} maxLength={500} rows={3} value={reason} onChange={event => setReason(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">{t('portal.members.bonusReasonHint')}</p>
          <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy || !!submitted} />{t('portal.members.bonusConfirm', {username: user.username})}</label>
          {error && <p className="portal-error" role="alert">{error}{t('portal.members.bonusRetryNote')}</p>}
          <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>{t('portal.members.cancel')}</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? t('portal.members.bonusBusy') : submitted ? t('portal.members.bonusRetry') : t('portal.members.bonusSubmit')}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
