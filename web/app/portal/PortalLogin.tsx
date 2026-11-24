'use client';

import {useId, useRef, useState} from 'react';
import {ArrowUpRight, CalendarCheck, Eye, EyeOff, ScanLine, ShieldCheck} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {useT} from '@/lib/i18n/provider';
import PortalRegister from './PortalRegister';

export default function PortalLogin({login}: {login: (username: string, password: string) => Promise<void>}) {
  const t = useT();
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [visible, setVisible] = useState(false);
  const [error, setError] = useState('');
  const [registering,setRegistering]=useState(false);
  const [registeredName,setRegisteredName]=useState('');
  const inFlight = useRef(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (inFlight.current) return;
    const form = event.currentTarget;
    const username = (form.elements.namedItem('username') as HTMLInputElement).value.trim();
    const password = (form.elements.namedItem('password') as HTMLInputElement).value;
    if (!username || !password) { setError(t('portal.login.needBoth')); return; }
    inFlight.current = true; setBusy(true); setError('');
    try {
      await login(username, password);
      form.reset(); setVisible(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : t('portal.login.uncertain'));
    } finally { inFlight.current = false; setBusy(false); }
  }

  if(registering)return <PortalRegister onCancel={()=>setRegistering(false)} onRegistered={username=>{
    setRegisteredName(username);setRegistering(false);setError('');
  }}/>;
  return <section className="portal-login-stage" aria-labelledby={`${id}-title`}>
    <div className="portal-login-intro"><span className="portal-section-kicker">{t('portal.login.kicker')}</span>
      <h1 id={`${id}-title`}>{t('portal.login.title')}</h1>
      <p>{t('portal.login.desc')}</p></div>
    {registeredName&&<p className="portal-notice" role="status">{t('portal.login.registered')}</p>}
    <form className="portal-login-card" onSubmit={event => void submit(event)} noValidate>
      <div className="portal-login-fields">
        <div className="portal-login-field"><Label htmlFor={`${id}-username`}>{t('portal.common.username')}</Label>
          <Input id={`${id}-username`} name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
            defaultValue={registeredName} placeholder={t('portal.login.usernamePlaceholder')} disabled={busy} aria-invalid={!!error} aria-describedby={error ? `${id}-error` : undefined}
            onChange={() => setError('')} /></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-password`}>{t('portal.common.password')}</Label>
          <div className="portal-login-password"><Input id={`${id}-password`} name="password" type={visible ? 'text' : 'password'}
            autoComplete="current-password" placeholder={t('portal.login.passwordPlaceholder')} disabled={busy} aria-invalid={!!error}
            aria-describedby={error ? `${id}-error` : undefined} onChange={() => setError('')} />
            <button type="button" aria-label={visible ? t('portal.login.hidePassword') : t('portal.login.showPassword')} aria-pressed={visible} disabled={busy}
              onClick={() => setVisible(value => !value)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></div>
        {error && <p id={`${id}-error`} className="portal-login-error" role="alert">{error}</p>}
        <Button className="portal-login-submit" disabled={busy}>{busy ? t('portal.login.busy') : t('portal.login.submit')}<ArrowUpRight size={15} /></Button>
        <Button type="button" variant="outline" disabled={busy} onClick={()=>setRegistering(true)}>{t('portal.login.register')}</Button>
      </div>
      <p className="portal-login-help"><ShieldCheck size={14} />{t('portal.login.help')}</p>
    </form>
    <div className="portal-login-features"><span><ScanLine size={15} />{t('portal.login.featureAuth')}</span><span><CalendarCheck size={15} />{t('portal.login.featureCheckin')}</span><span><ShieldCheck size={15} />{t('portal.login.featureIsolation')}</span></div>
    <p className="portal-login-policy">{t('portal.login.policy')}</p>
  </section>;
}
