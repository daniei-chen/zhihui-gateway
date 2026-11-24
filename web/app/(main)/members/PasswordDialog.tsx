'use client';

import {useId, useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch} from '@/lib/portal-api';
import {portalPasswordLengthProblem, PORTAL_PASSWORD_HINT_KEY} from '@/lib/portal-members';
import {useT} from '@/lib/i18n/provider';

export default function PasswordDialog({user, close, saved}: {
  user: {id: string; username: string}; close: () => void; saved: () => Promise<void>;
}) {
  const t = useT();
  const id = useId();
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [refreshError, setRefreshError] = useState(false);
  const inFlight = useRef(false);
  const lengthProblem = portalPasswordLengthProblem(password);
  const valid = lengthProblem === null && password === repeat && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || inFlight.current || done) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      await portalFetch(`portal-admin/users/${encodeURIComponent(user.id)}`, {method: 'PATCH', body: JSON.stringify({password})});
      setPassword(''); setRepeat(''); setDone(true);
      try { await saved(); } catch { setRefreshError(true); }
    } catch (err) { setError(err instanceof Error ? err.message : t('portal.members.resetUncertain')); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !inFlight.current) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => { if (inFlight.current) event.preventDefault(); }}
      onInteractOutside={event => { if (inFlight.current) event.preventDefault(); }}>
      <DialogHeader><DialogTitle>{t('portal.members.resetTitle', {username: user.username})}</DialogTitle><DialogDescription>{t('portal.members.resetDesc')}</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)} noValidate>
        {done ? <><p className="portal-notice" role="status">{t('portal.members.resetDone')}</p>
          {refreshError && <p className="portal-small" role="status">{t('portal.members.resetDoneRefreshFailed')}</p>}
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>{t('portal.members.finish')}</button></> : <>
          <label className="portal-field" htmlFor={`${id}-password`}>{t('portal.members.newPassword')}<input id={`${id}-password`} className="portal-input" type="password" required autoComplete="new-password" value={password} onChange={event => {setPassword(event.target.value); setError('');}} disabled={busy}
            aria-invalid={!!error || (!!password && lengthProblem !== null)} aria-describedby={`${id}-hint ${id}-length${error ? ` ${id}-error` : ''}`} /></label>
          <p id={`${id}-hint`} className="portal-small">{t(PORTAL_PASSWORD_HINT_KEY)}</p>
          <p id={`${id}-length`} className="portal-small" role="status">{password && lengthProblem ? t(lengthProblem) : null}</p>
          <label className="portal-field" htmlFor={`${id}-repeat`}>{t('portal.members.repeatPassword')}<input id={`${id}-repeat`} className="portal-input" type="password" required autoComplete="new-password" value={repeat} onChange={event => {setRepeat(event.target.value); setError('');}} disabled={busy}
            aria-invalid={!!repeat && password !== repeat} aria-describedby={`${id}-mismatch`} /></label>
          <p id={`${id}-mismatch`} className="portal-small" role="status">{repeat && password !== repeat ? t('portal.members.passwordMismatch') : null}</p>
          <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy} />{t('portal.members.resetConfirm', {username: user.username})}</label>
          {error && <p id={`${id}-error`} className="portal-error" role="alert">{error}</p>}
          <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>{t('portal.members.cancel')}</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? t('portal.members.resetBusy') : t('portal.members.resetSubmit')}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
