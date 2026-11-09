'use client';

import {useId, useRef, useState} from 'react';
import {Check, Eye, EyeOff, ShieldCheck, Sparkles, UserPlus} from 'lucide-react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {PortalError, portalFetch} from '@/lib/portal-api';
import {passwordCharacters, portalPasswordLengthProblem, portalUsernameProblem, PORTAL_PASSWORD_HINT} from '@/lib/portal-members';

export default function CreateMemberDialog({close, saved}: {close: () => void; saved: () => Promise<void>}) {
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
    if (problem) { setError(problem); setField(portalUsernameProblem(username) ? 'username' : 'password'); return; }
    inFlight.current = true; setBusy(true); edit();
    try {
      const result = await portalFetch<{id: string; username: string}>('portal-admin/users', {
        method: 'POST', body: JSON.stringify({username, password}),
      });
      setCreated(result.username); setPassword(''); setVisible(false);
      try { await saved(); } catch { setRefreshError(true); }
    } catch (err) {
      const message = err instanceof Error ? err.message : '开通结果待确认，请先刷新用户列表，不要重复提交。';
      setError(message);
      setField(err instanceof PortalError && message.includes('密码') ? 'password'
        : err instanceof PortalError && message.includes('用户名') ? 'username' : '');
    } finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !inFlight.current) close(); }}>
    <DialogContent className="portal-ops-dialog member-create-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => { if (inFlight.current) event.preventDefault(); }}
      onInteractOutside={event => { if (inFlight.current) event.preventDefault(); }}>
      <DialogHeader><div className="member-dialog-icon"><UserPlus size={21} /></div><DialogTitle>开通普通用户</DialogTitle>
        <DialogDescription>为用户创建独立工作台，不授予管理权限，也不开放他人的账号或记录。</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-14rem)]"><form className="portal-ops-form member-create-form" onSubmit={event => void submit(event)} noValidate>
        {created ? <div className="member-created"><span className="member-created-mark"><Check size={26} /></span><h3>用户已开通</h3>
          <p><strong>{created}</strong> 可以从用户端入口登录。请通过可信渠道交付你刚设置的初始密码。</p>
          {refreshError && <p role="status">用户创建成功，但列表刷新失败。关闭后刷新即可，不要重复开通。</p>}
          <p className="member-form-note">这不会解除当前模型调用与公共池贡献的限制。</p>
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>完成</button></div> : <>
          <label className="portal-field" htmlFor={`${id}-username`}>用户名<span className="member-field-caption">用于用户端登录</span>
            <input id={`${id}-username`} className="portal-input" value={username} onChange={event => {setUsername(event.target.value); edit();}}
              autoComplete="off" autoCapitalize="none" spellCheck={false} disabled={busy} aria-invalid={field === 'username'}
              aria-describedby={`${id}-username-hint${field === 'username' ? ` ${id}-error` : ''}`} /></label>
          <p id={`${id}-username-hint`} className="member-form-note">3–40位字母、数字、下划线或短横线，不含空格。</p>
          <div className="member-password-heading"><label htmlFor={`${id}-password`}>初始密码</label>
            <button type="button" className="member-text-button" disabled={busy} onClick={generate}><Sparkles size={13} />生成随机密码</button></div>
          <div className="member-password-wrap"><input id={`${id}-password`} className="portal-input" type={visible ? 'text' : 'password'}
            value={password} onChange={event => {setPassword(event.target.value); edit();}} autoComplete="new-password" disabled={busy}
            aria-invalid={field === 'password'} aria-describedby={`${id}-password-hint${field === 'password' ? ` ${id}-error` : ''}`} />
            <button type="button" className="member-password-toggle" disabled={busy} aria-label={visible ? '隐藏初始密码' : '显示初始密码'}
              aria-pressed={visible} onClick={() => setVisible(value => !value)}>{visible ? <EyeOff size={17} /> : <Eye size={17} />}</button></div>
          <div className="member-password-meta"><span id={`${id}-password-hint`}>{PORTAL_PASSWORD_HINT}</span><span>{passwordCharacters(password)}/128</span></div>
          {error && <p id={`${id}-error`} className="portal-error" role="alert">{error}</p>}
          <div className="member-form-assurance"><ShieldCheck size={17} /><span>密码不会以明文保存。用户积分和已有管理员身份不受影响。</span></div>
          <div className="member-dialog-footer"><button type="button" className="portal-button" disabled={busy} onClick={close}>取消</button>
            <button className="portal-button primary" disabled={busy}>{busy ? '正在开通…' : '确认开通用户'}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
