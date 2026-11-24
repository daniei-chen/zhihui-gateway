'use client';

import {useEffect, useId, useRef, useState} from 'react';
import {PortalError,portalFetch} from '@/lib/portal-api';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {useT} from '@/lib/i18n/provider';

type Policy={enabled:boolean; invite_required:true; device_registered:boolean};

export default function PortalRegister({onRegistered,onCancel}:{onRegistered:(username:string)=>void;onCancel:()=>void}) {
  const t = useT();
  const id=useId();
  const [policy,setPolicy]=useState<Policy|null>(null);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');
  const [uncertain,setUncertain]=useState(false);
  const inFlight=useRef(false);
  const mounted=useRef(true);
  useEffect(()=>{
    mounted.current=true;
    const controller=new AbortController();
    void portalFetch<Policy>('portal/registration',{signal:controller.signal}).then(result=>{
      if(!controller.signal.aborted)setPolicy(result);
    }).catch(err=>{if(!controller.signal.aborted)setError(err instanceof Error?err.message:t('portal.register.unavailable'));});
    return ()=>{mounted.current=false;controller.abort();};
  },[t]);
  async function submit(event:React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if(inFlight.current||!policy?.enabled||policy.device_registered||uncertain)return;
    const form=event.currentTarget;
    const value=(name:string)=>(form.elements.namedItem(name) as HTMLInputElement).value;
    const username=value('username').trim(),password=value('password'),invite=value('invite').trim();
    if(password!==value('confirmation')){setError(t('portal.register.mismatch'));return;}
    if(!/^[a-zA-Z0-9_-]{3,40}$/.test(username)||password.length<12||!invite){setError(t('portal.register.invalid'));return;}
    inFlight.current=true;setBusy(true);setError('');
    try {
      const result=await portalFetch<{registered:true;username:string}>('portal/register',{
        method:'POST',body:JSON.stringify({username,password,invite}),
      });
      if(!mounted.current)return;
      form.reset();onRegistered(result.username);
    } catch(err) {
      if(!mounted.current)return;
      const unknown=!(err instanceof PortalError)||err.status<400||err.status>=500||err.status===409;
      setError(unknown?t('portal.register.uncertainDetail'):err.message);
      if(unknown)setUncertain(true);
    } finally{inFlight.current=false;if(mounted.current)setBusy(false);}
  }
  const unavailable=!policy||!policy.enabled||policy.device_registered;
  return <section className="portal-login-stage" aria-labelledby={`${id}-title`}>
    <div className="portal-login-intro"><span className="portal-section-kicker">{t('portal.login.kicker')}</span>
      <h1 id={`${id}-title`}>{t('portal.register.title')}</h1><p>{t('portal.register.desc')}</p></div>
    <form className="portal-login-card" onSubmit={event=>void submit(event)}>
      <div className="portal-login-fields">
        {!policy&&!error&&<p role="status">{t('portal.register.preparing')}</p>}
        {policy?.device_registered&&<p className="portal-notice" role="status">{t('portal.register.deviceUsed')}</p>}
        {policy&&!policy.enabled&&<p role="status">{t('portal.register.disabled')}</p>}
        <div className="portal-login-field"><Label htmlFor={`${id}-username`}>{t('portal.common.username')}</Label><Input id={`${id}-username`} name="username" required minLength={3} maxLength={40}
          pattern="[a-zA-Z0-9_-]{3,40}" autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder={t('portal.register.usernamePlaceholder')} disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-password`}>{t('portal.common.password')}</Label><Input id={`${id}-password`} name="password" type="password" required minLength={12} maxLength={128}
          autoComplete="new-password" placeholder={t('portal.register.passwordPlaceholder')} disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-confirmation`}>{t('portal.register.confirmation')}</Label><Input id={`${id}-confirmation`} name="confirmation" type="password" required minLength={12} maxLength={128}
          autoComplete="new-password" disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-invite`}>{t('portal.register.invite')}</Label><Input id={`${id}-invite`} name="invite" required maxLength={64}
          autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder={t('portal.register.invitePlaceholder')} disabled={busy||unavailable}/></div>
        {error&&<p className="portal-login-error" role="alert">{error}</p>}
        <Button className="portal-login-submit" disabled={busy||unavailable||uncertain}>{busy?t('portal.register.busy'):uncertain?t('portal.register.uncertainAction'):t('portal.register.submit')}</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={onCancel}>{t('portal.common.backToLogin')}</Button>
      </div>
      <p className="portal-login-help">{t('portal.register.help')}</p>
    </form>
  </section>;
}
