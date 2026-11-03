'use client';

import {useId, useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch} from '@/lib/portal-api';
import {portalPasswordLengthProblem, PORTAL_PASSWORD_HINT} from '@/lib/portal-members';

export default function PasswordDialog({user, close, saved}: {
  user: {id: string; username: string}; close: () => void; saved: () => Promise<void>;
}) {
  const id = useId();
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const [refreshError, setRefreshError] = useState(false);
  const inFlight = useRef(false);
  const valid = portalPasswordLengthProblem(password) === null && password === repeat && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || inFlight.current || done) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      await portalFetch(`portal-admin/users/${encodeURIComponent(user.id)}`, {method: 'PATCH', body: JSON.stringify({password})});
      setPassword(''); setRepeat(''); setDone(true);
      try { await saved(); } catch { setRefreshError(true); }
    } catch (err) { setError(err instanceof Error ? err.message : '结果待确认，请核对用户状态；不会自动重试'); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !inFlight.current) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => { if (inFlight.current) event.preventDefault(); }}
      onInteractOutside={event => { if (inFlight.current) event.preventDefault(); }}>
      <DialogHeader><DialogTitle>重置密码 · {user.username}</DialogTitle><DialogDescription>旧用户会话和旧代次调用密钥将失效，积分与账号不变。请通过可信渠道交付新密码。</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)} noValidate>
        {done ? <><p className="portal-notice" role="status">密码已重置，旧会话及调用密钥已失效。用户重新登录后可创建新密钥。</p>
          {refreshError && <p className="portal-small" role="status">密码已重置，但列表刷新失败。请关闭后刷新用户列表，不要重复重置。</p>}
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>完成</button></> : <>
          <label className="portal-field" htmlFor={`${id}-password`}>新密码<input id={`${id}-password`} className="portal-input" type="password" required autoComplete="new-password" value={password} onChange={event => {setPassword(event.target.value); setError('');}} disabled={busy}
            aria-invalid={!!error || (!!password && portalPasswordLengthProblem(password) !== null)} aria-describedby={`${id}-hint ${id}-length${error ? ` ${id}-error` : ''}`} /></label>
          <p id={`${id}-hint`} className="portal-small">{PORTAL_PASSWORD_HINT}</p>
          <p id={`${id}-length`} className="portal-small" role="status">{password ? portalPasswordLengthProblem(password) : null}</p>
          <label className="portal-field" htmlFor={`${id}-repeat`}>再次输入新密码<input id={`${id}-repeat`} className="portal-input" type="password" required autoComplete="new-password" value={repeat} onChange={event => {setRepeat(event.target.value); setError('');}} disabled={busy}
            aria-invalid={!!repeat && password !== repeat} aria-describedby={`${id}-mismatch`} /></label>
          <p id={`${id}-mismatch`} className="portal-small" role="status">{repeat && password !== repeat ? '两次输入的密码不一致。' : null}</p>
          <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy} />确认撤销 {user.username} 的旧会话和旧调用密钥，不清空积分与账号。</label>
          {error && <p id={`${id}-error`} className="portal-error" role="alert">{error}</p>}
          <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>取消</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? '正在重置…' : '确认重置密码'}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
