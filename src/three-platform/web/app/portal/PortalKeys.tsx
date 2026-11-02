'use client';

import {useEffect, useRef, useState} from 'react';
import {BASE_PATH} from '@/lib/base-path';
import {portalFetch, portalTime} from '@/lib/portal-api';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';

type Key = {id: number; label: string; prefix: string; enabled: boolean; created_at: number};
type Keys = {items: Key[]; allowed_models: string[]};

export default function PortalKeys() {
  const [data, setData] = useState<Keys | null>(null);
  const [label, setLabel] = useState('我的调用密钥');
  const [secret, setSecret] = useState<{id: number; value: string} | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [base, setBase] = useState('');
  const [revoke, setRevoke] = useState<Key | null>(null);
  const inFlight = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    void portalFetch<Keys>('portal/keys', {signal: controller.signal}).then(result => {
      if (!controller.signal.aborted) { setData(result); setBase(`${window.location.origin}${BASE_PATH}/v1`); }
    }).catch(err => { if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '密钥读取失败'); });
    return () => controller.abort();
  }, []);
  async function run(action: () => Promise<void>) {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true); setError('');
    try { await action(); setData(await portalFetch<Keys>('portal/keys')); }
    catch (err) { setError(err instanceof Error ? err.message : '密钥操作失败'); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <section className="portal-panel"><h2>我的调用密钥</h2>
    <p className="portal-small">公共池统一调度，不固定使用你添加的账号。多个密钥共用你的积分与请求锁，不绑定来源 IP。</p>
    <p className="portal-small">仅限 DeepSeek 国内模型：{data?.allowed_models.join(' / ') || '正在读取白名单'}。模型实际可用性以网关目录为准。</p>
    <label className="portal-field">OpenAI 兼容 Base URL<input className="portal-input" value={base} readOnly onFocus={event => event.target.select()} /></label>
    <p className="portal-small">单用户一次一个计费请求；费用缺失或流中断会保留冻结，需核对后恢复，不当成免费调用。</p>
    {error && <p className="portal-error" role="alert">{error}</p>}
    {secret && <div className="portal-notice" role="status"><strong>明文仅显示这一次，请立即保存。</strong>
      <label className="portal-field">新密钥<input className="portal-input" value={secret.value} readOnly autoComplete="off" spellCheck={false} onFocus={event => event.target.select()} /></label>
      <button className="portal-button" onClick={() => setSecret(null)}>我已保存，隐藏明文</button></div>}
    <form className="portal-actions" onSubmit={event => { event.preventDefault(); void run(async () => {
      const result = await portalFetch<{id: number; key: string}>('portal/keys', {method: 'POST', body: JSON.stringify({label})});
      setSecret({id: result.id, value: result.key});
    }); }}><label className="portal-field">密钥名称<input className="portal-input" required maxLength={60} value={label} onChange={event => setLabel(event.target.value)} /></label>
      <button className="portal-button primary" disabled={busy || !data || data.items.filter(key => key.enabled).length >= 5}>生成密钥</button></form>
    {data?.items.map(key => <article className="portal-account" key={key.id}><div><div className="portal-account-name">{key.label}</div>
      <p className="portal-small">{key.prefix}… · {portalTime(key.created_at)} · {key.enabled ? '有效' : '已失效'}</p></div>
      <button className="portal-button" disabled={busy || !key.enabled} onClick={() => setRevoke(key)}>撤销</button></article>)}
    {data && !data.items.length && <p className="portal-empty">还没有调用密钥。密钥明文不会保存在浏览器本地存储。</p>}
    {revoke && <Dialog open onOpenChange={open => { if (!open && !busy) setRevoke(null); }}><DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>撤销密钥 · {revoke.label}</DialogTitle><DialogDescription>撤销后不能恢复。正在执行的请求仍按实际用量结算，不会因此解除费用未知冻结。</DialogDescription></DialogHeader>
      <DialogBody>{error && <p className="portal-error" role="alert">{error}</p>}<div className="portal-actions"><button className="portal-button" disabled={busy} onClick={() => setRevoke(null)}>取消</button><button className="portal-button primary" disabled={busy} onClick={() => void run(async () => {
        await portalFetch(`portal/keys/${revoke.id}`, {method: 'DELETE'});
        if (secret?.id === revoke.id) setSecret(null);
        setRevoke(null);
      })}>{busy ? '正在撤销…' : '确认撤销'}</button></div></DialogBody>
    </DialogContent></Dialog>}
  </section>;
}
