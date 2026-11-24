'use client';

import {useId, useRef, useState} from 'react';
import {Check, Eye, EyeOff, ShieldCheck, Sparkles, UserPlus} from 'lucide-react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {PortalError, portalFetch} from '@/lib/portal-api';
import {passwordCharacters, portalFieldForError, portalPasswordLengthProblem, portalUsernameProblem, PORTAL_PASSWORD_HINT_KEY} from '@/lib/portal-members';
import {useT} from '@/lib/i18n/provider';

export default function CreateMemberDialog({close, saved}: {close: () => void; saved: () => Promise<void>}) {
  const t = useT();
  const id = useId();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [field, setField] = useState('');
  const [created, setCreated] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState(false);
  const inFlight = useRef(false);

  function edit() { setError(''); setField(''); }
  function generate() {
    const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    setPassword(Array.from(crypto.getRandomValues(new Uint8Array(18)), byte => alphabet[byte & 63]).join(''));
    setVisible(true); edit();
  }
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current || created) return;
    const problem = portalUsernameProblem(username) || portalPasswordLengthProblem(password);
    if (problem) { setError(t(problem)); setField(portalUsernameProblem(username) ? 'username' : 'password'); return; }
    inFlight.current = true; setBusy(true); edit();
    try {
      const result = await portalFetch<{id: string; username: string}>('portal-admin/users', {
        method: 'POST', body: JSON.stringify({username, password}),
      });
      setCreated(result.username); setPassword(''); setVisible(false);
      try { await saved(); } catch { setRefreshError(true); }
    } catch (err) {
      const message = err instanceof Error ? err.message : t('portal.members.createUncertain');
      setError(message);
      setField(err instanceof PortalError ? portalFieldForError(message) : '');
    } finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !inFlight.current) close(); }}>
    <DialogContent className="portal-ops-dialog member-create-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => { if (inFlight.current) event.preventDefault(); }}
      onInteractOutside={event => { if (inFlight.current) event.preventDefault(); }}>
      <DialogHeader><div className="member-dialog-icon"><UserPlus size={21} /></div><DialogTitle>{t('portal.members.createTitle')}</DialogTitle>
        <DialogDescription>{t('portal.members.createDesc')}</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-14rem)]"><form className="portal-ops-form member-create-form" onSubmit={event => void submit(event)} noValidate>
        {created ? <div className="member-created"><span className="member-created-mark"><Check size={26} /></span><h3>{t('portal.members.created')}</h3>
          <p>{t('portal.members.createdHint', {username: created})}</p>
          {refreshError && <p role="status">{t('portal.members.createdRefreshFailed')}</p>}
          <p className="member-form-note">{t('portal.members.createdNote')}</p>
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>{t('portal.members.finish')}</button></div> : <>
          <label className="portal-field" htmlFor={`${id}-username`}>{t('portal.members.usernameLabel')}<span className="member-field-caption">{t('portal.members.usernameCaption')}</span>
            <input id={`${id}-username`} className="portal-input" value={username} onChange={event => {setUsername(event.target.value); edit();}}
              autoComplete="off" autoCapitalize="none" spellCheck={false} disabled={busy} aria-invalid={field === 'username'}
              aria-describedby={`${id}-username-hint${field === 'username' ? ` ${id}-error` : ''}`} /></label>
          <p id={`${id}-username-hint`} className="member-form-note">{t('portal.members.usernameHint')}</p>
          <div className="member-password-heading"><label htmlFor={`${id}-password`}>{t('portal.members.passwordLabel')}</label>
            <button type="button" className="member-text-button" disabled={busy} onClick={generate}><Sparkles size={13} />{t('portal.members.generatePassword')}</button></div>
          <div className="member-password-wrap"><input id={`${id}-password`} className="portal-input" type={visible ? 'text' : 'password'}
            value={password} onChange={event => {setPassword(event.target.value); edit();}} autoComplete="new-password" disabled={busy}
            aria-invalid={field === 'password'} aria-describedby={`${id}-password-hint${field === 'password' ? ` ${id}-error` : ''}`} />
            <button type="button" className="member-password-toggle" disabled={busy} aria-label={visible ? t('portal.members.hidePassword') : t('portal.members.showPassword')}
              aria-pressed={visible} onClick={() => setVisible(value => !value)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div>
          <div className="member-password-meta"><span id={`${id}-password-hint`}>{t(PORTAL_PASSWORD_HINT_KEY)}</span><span>{passwordCharacters(password)}/128</span></div>
          {error && <p id={`${id}-error`} className="portal-error" role="alert">{error}</p>}
          <div className="member-form-assurance"><ShieldCheck size={17} /><span>{t('portal.members.passwordAssurance')}</span></div>
          <div className="member-dialog-footer"><button type="button" className="portal-button" disabled={busy} onClick={close}>{t('portal.members.cancel')}</button>
            <button className="portal-button primary" disabled={busy}>{busy ? t('portal.members.createBusy') : t('portal.members.createSubmit')}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
