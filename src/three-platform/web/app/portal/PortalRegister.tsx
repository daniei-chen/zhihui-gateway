'use client';

import {useEffect, useId, useRef, useState} from 'react';
import {PortalError,portalFetch} from '@/lib/portal-api';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';

type Policy={enabled:boolean; invite_required:true; device_registered:boolean};

export default function PortalRegister({onRegistered,onCancel}:{onRegistered:(username:string)=>void;onCancel:()=>void}) {
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
    }).catch(err=>{if(!controller.signal.aborted)setError(err instanceof Error?err.message:'注册暂不可用');});
    return ()=>{mounted.current=false;controller.abort();};
  },[]);
  async function submit(event:React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if(inFlight.current||!policy?.enabled||policy.device_registered||uncertain)return;
    const form=event.currentTarget;
    const value=(name:string)=>(form.elements.namedItem(name) as HTMLInputElement).value;
    const username=value('username').trim(),password=value('password'),invite=value('invite').trim();
    if(password!==value('confirmation')){setError('两次密码不一致。');return;}
    if(!/^[a-zA-Z0-9_-]{3,40}$/.test(username)||password.length<12||!invite){setError('请填写有效用户名、至少12位密码和邀请码。');return;}
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
      setError(unknown?'注册结果待确认，请返回用户登录核对，不要重复提交。':err.message);
      if(unknown)setUncertain(true);
    } finally{inFlight.current=false;if(mounted.current)setBusy(false);}
  }
  const unavailable=!policy||!policy.enabled||policy.device_registered;
  return <section className="portal-login-stage" aria-labelledby={`${id}-title`}>
    <div className="portal-login-intro"><span className="portal-section-kicker">智汇 · 个人工作台</span>
      <h1 id={`${id}-title`}>创建你的智汇账号</h1><p>使用管理员提供的邀请码，开启你的个人工作台。</p></div>
    <form className="portal-login-card" onSubmit={event=>void submit(event)}>
      <div className="portal-login-fields">
        {!policy&&!error&&<p role="status">正在准备安全注册…</p>}
        {policy?.device_registered&&<p className="portal-notice" role="status">此浏览器已注册过账号，请直接登录；忘记密码请联系管理员。</p>}
        {policy&&!policy.enabled&&<p role="status">注册暂时关闭，请稍后再试。</p>}
        <div className="portal-login-field"><Label htmlFor={`${id}-username`}>用户名</Label><Input id={`${id}-username`} name="username" required minLength={3} maxLength={40}
          pattern="[a-zA-Z0-9_-]{3,40}" autoComplete="username" autoCapitalize="none" spellCheck={false} placeholder="3–40位字母、数字、下划线或短横线" disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-password`}>登录密码</Label><Input id={`${id}-password`} name="password" type="password" required minLength={12} maxLength={128}
          autoComplete="new-password" placeholder="至少12位，避免使用常见密码" disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-confirmation`}>确认密码</Label><Input id={`${id}-confirmation`} name="confirmation" type="password" required minLength={12} maxLength={128}
          autoComplete="new-password" disabled={busy||unavailable}/></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-invite`}>一次性邀请码</Label><Input id={`${id}-invite`} name="invite" required maxLength={64}
          autoComplete="off" autoCapitalize="none" spellCheck={false} placeholder="向管理员获取邀请码" disabled={busy||unavailable}/></div>
        {error&&<p className="portal-login-error" role="alert">{error}</p>}
        <Button className="portal-login-submit" disabled={busy||unavailable||uncertain}>{busy?'正在注册…':uncertain?'结果待确认，请尝试登录':'注册账号'}</Button>
        <Button type="button" variant="outline" disabled={busy} onClick={onCancel}>返回用户登录</Button>
      </div>
      <p className="portal-login-help">每个邀请码仅能成功注册一次；本浏览器仅登记一个账号。注册不会自动获得调用额度。</p>
    </form>
  </section>;
}
