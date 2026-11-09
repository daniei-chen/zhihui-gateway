'use client';

import {useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch, points, portalTime} from '@/lib/portal-api';
import {creditInputUnits} from '@/lib/portal-operations';

type Submission = {action_id: string; credit: string; valid_days: number; reason: string};
export default function BonusDialog({user, close, saved}: {
  user: {id: string; username: string}; close: () => void; saved: () => Promise<void>;
}) {
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
    } catch (err) { setError(err instanceof Error ? err.message : '赠送结果未知，请刷新记录核对'); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>赠送积分 · {user.username}</DialogTitle><DialogDescription>仅超级管理员 admin 本人可操作。实际增加积分，不是信用额度或未来签到预支。</DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)}>
        {result ? <><p className="portal-notice" role="status">{result.message}<br />已赠送 {points(result.amount_units)} 积分 · {portalTime(result.expires_at)} 到期。</p>
          <button type="button" className="portal-button primary" disabled={busy} onClick={close}>完成</button></> : <>
          <label className="portal-field">赠送积分<input className="portal-input" inputMode="decimal" required maxLength={17} value={credit} onChange={event => setCredit(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">大于0，最多6位小数；单次最多100万积分。赠送不是供应商充值，不改变公共池真实余额。</p>
          <label className="portal-field">有效天数（默认30天）<input className="portal-input" inputMode="numeric" required maxLength={3} value={days} onChange={event => setDays(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">1至365天，从本次首次入账时计算；重试不会延长到期时间。</p>
          <label className="portal-field">赠送原因（8至500字）<textarea className="portal-input" required minLength={8} maxLength={500} rows={3} value={reason} onChange={event => setReason(event.target.value)} disabled={busy || !!submitted} /></label>
          <p className="portal-small">不填写密码、Token或完整授权文件。赠送不清除历史超额费用，也不解除未知请求冻结。</p>
          <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy || !!submitted} />确认给 {user.username} 额外增加积分，金额和有效期已核对。</label>
          {error && <p className="portal-error" role="alert">{error}。内容已锁定；重试使用同一赠送标识，不会重复加分，也可关闭后刷新记录核对。</p>}
          <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>取消</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? '正在赠送…' : submitted ? '按原内容重试' : '确认赠送积分'}</button></div>
        </>}
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
