'use client';

import {useId, useRef, useState} from 'react';
import {ArrowUpRight, CalendarCheck, Eye, EyeOff, ScanLine, ShieldCheck} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import PortalRegister from './PortalRegister';

export default function PortalLogin({login}: {login: (username: string, password: string) => Promise<void>}) {
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
    if (!username || !password) { setError('请填写用户名和密码。'); return; }
    inFlight.current = true; setBusy(true); setError('');
    try {
      await login(username, password);
      form.reset(); setVisible(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录结果待确认，请稍后重试。');
    } finally { inFlight.current = false; setBusy(false); }
  }

  if(registering)return <PortalRegister onCancel={()=>setRegistering(false)} onRegistered={username=>{
    setRegisteredName(username);setRegistering(false);setError('');
  }}/>;
  return <section className="portal-login-stage" aria-labelledby={`${id}-title`}>
    <div className="portal-login-intro"><span className="portal-section-kicker">智汇 · 个人工作台</span>
      <h1 id={`${id}-title`}>登录用户工作台</h1>
      <p>账号状态、积分明细和调用记录，都在这里。</p></div>
    {registeredName&&<p className="portal-notice" role="status">注册成功，请输入密码登录。</p>}
    <form className="portal-login-card" onSubmit={event => void submit(event)} noValidate>
      <div className="portal-login-fields">
        <div className="portal-login-field"><Label htmlFor={`${id}-username`}>用户名</Label>
          <Input id={`${id}-username`} name="username" autoComplete="username" autoCapitalize="none" spellCheck={false}
            defaultValue={registeredName} placeholder="输入你的用户名" disabled={busy} aria-invalid={!!error} aria-describedby={error ? `${id}-error` : undefined}
            onChange={() => setError('')} /></div>
        <div className="portal-login-field"><Label htmlFor={`${id}-password`}>登录密码</Label>
          <div className="portal-login-password"><Input id={`${id}-password`} name="password" type={visible ? 'text' : 'password'}
            autoComplete="current-password" placeholder="输入你的登录密码" disabled={busy} aria-invalid={!!error}
            aria-describedby={error ? `${id}-error` : undefined} onChange={() => setError('')} />
            <button type="button" aria-label={visible ? '隐藏密码' : '显示密码'} aria-pressed={visible} disabled={busy}
              onClick={() => setVisible(value => !value)}>{visible ? <EyeOff size={16} /> : <Eye size={16} />}</button></div></div>
        {error && <p id={`${id}-error`} className="portal-login-error" role="alert">{error}</p>}
        <Button className="portal-login-submit" disabled={busy}>{busy ? '正在登录…' : '进入我的工作台'}<ArrowUpRight size={15} /></Button>
        <Button type="button" variant="outline" disabled={busy} onClick={()=>setRegistering(true)}>使用邀请码注册账号</Button>
      </div>
      <p className="portal-login-help"><ShieldCheck size={14} />使用一次性邀请码注册；已有账号直接登录，忘记密码请联系管理员。</p>
    </form>
    <div className="portal-login-features"><span><ScanLine size={15} />官方扫码授权</span><span><CalendarCheck size={15} />签到状态同步</span><span><ShieldCheck size={15} />个人数据隔离</span></div>
    <p className="portal-login-policy">每日签到积分逐批计算，最长 30 天有效；已有积分保留原到期时间。</p>
  </section>;
}
