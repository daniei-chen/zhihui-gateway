'use client';

import {useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch} from '@/lib/portal-api';

export default function PasswordDialog({user, close, saved}: {
  user: {id: string; username: string}; close: () => void; saved: () => Promise<void>;
}) {
  const [password, setPassword] = useState('');
  const [repeat, setRepeat] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  const inFlight = useRef(false);
  const valid = password.length >= 8 && password.length <= 128 && password === repeat && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!valid || inFlight.current || done) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      await portalFetch(`portal-admin/users/${encodeURIComponent(user.id)}`, {method: 'PATCH', body: JSON.stringify({password})});
      setPassword(''); setRepeat(''); setDone(true);
      await saved();
    } catch (err) { setError(err instanceof Error ? err.message : '结果待确认，请核对用户状态；不会自动重试'); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>重置密码 · {user.username}</DialogTitle><DialogDescription>旧用户会话和旧代次调用密钥将失效，积分与账号不变。请通过可信渠道交付新密码。</DialogDescription></DialogHeader>
      <DialogBody><form className="portal-ops-form" onSubmit={event => void submit(event)}>
        {done ? <><p className="portal-notice" role="status">密码已重置，旧会话及调用密钥已失效。用户重新登录后可创建新密钥。</p><button type="button" className="portal-button primary" disabled={busy} onClick={close}>完成</button></> : <>
          <label className="portal-field">新密码<input className="portal-input" type="password" required minLength={8} maxLength={128} autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} disabled={busy} /></label>
          <label className="portal-field">再次输入新密码<input className="portal-input" type="password" required minLength={8} maxLength={128} autoComplete="new-password" value={repeat} onChange={event => setRepeat(event.target.value)} disabled={busy} /></label>
          <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy} />确认撤销 {user.username} 的旧会话和旧调用密钥，不清空积分与账号。</label>
          {error && <p className="portal-error" role="alert">{error}</p>}
          <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>取消</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? '正在重置…' : '确认重置密码'}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
