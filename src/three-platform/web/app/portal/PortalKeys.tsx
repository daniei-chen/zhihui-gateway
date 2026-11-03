'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {BASE_PATH} from '@/lib/base-path';
import {PortalError, portalFetch, portalTime} from '@/lib/portal-api';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';

type Key = {id: number; label: string; prefix: string; enabled: boolean; created_at: number};
type Keys = {items: Key[]; allowed_models: string[]; dispatch_enabled?: boolean; pool_enabled?: boolean; model_discovery_enabled?: boolean; dispatch_note?: string};

export default function PortalKeys({onUnauthorized}: {onUnauthorized: () => void}) {
  const [data, setData] = useState<Keys | null>(null);
  const [label, setLabel] = useState('我的调用密钥');
  const [secret, setSecret] = useState<{id: number; value: string} | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [base, setBase] = useState('');
  const [revoke, setRevoke] = useState<Key | null>(null);
  const [notice, setNotice] = useState('');
  const [loading, setLoading] = useState(true);
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const inFlight = useRef(false);
  const sequence = useRef(0);
  const invalidate = useCallback(() => {sequence.current++;}, []);
  useEffect(() => {
    const controller = new AbortController();
    const generation = ++sequence.current;
    void portalFetch<Keys>('portal/keys', {signal: controller.signal}).then(result => {
      if (!controller.signal.aborted && generation === sequence.current) {setData(result); setBase(`${window.location.origin}${BASE_PATH}/v1`);}
    }).catch(err => {
      if (controller.signal.aborted || generation !== sequence.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
      else setError(err instanceof Error ? err.message : '密钥读取失败');
    }).finally(() => {if (!controller.signal.aborted && generation === sequence.current) setLoading(false);});
    return () => {controller.abort(); invalidate();};
  }, [onUnauthorized, invalidate]);
  async function run<T>(request: () => Promise<T>, commit: (result: T) => void, success: string) {
    if (inFlight.current) return;
    inFlight.current = true;
    const generation = ++sequence.current;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await request();
      if (generation !== sequence.current) return;
      commit(result); setNotice(success);
      try {
        const fresh = await portalFetch<Keys>('portal/keys');
        if (generation === sequence.current) {setData(fresh); setNeedsRefresh(false);}
      } catch (err) {
        if (generation !== sequence.current) return;
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
        else {setNeedsRefresh(true); setError(`${success}，但列表刷新失败。不要重复操作，请重新读取列表。`);}
      }
    } catch (err) {
      if (generation !== sequence.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
      else if (!(err instanceof PortalError) || err.status >= 500) {
        setNeedsRefresh(true); setError('操作结果待确认。请先刷新列表核对，不要再次提交；系统不会自动重试。');
      } else setError(err.message);
    } finally {inFlight.current = false; if (generation === sequence.current) {setBusy(false); setLoading(false);}}
  }
  async function refresh() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError('');
    const generation = ++sequence.current;
    try {
      const result = await portalFetch<Keys>('portal/keys');
      if (generation === sequence.current) {setData(result); setNeedsRefresh(false); setNotice('列表已更新'); setBase(`${window.location.origin}${BASE_PATH}/v1`);}
    } catch (err) {
      if (generation !== sequence.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
      else setError(err instanceof Error ? err.message : '列表读取失败，请重试。');
    } finally {inFlight.current = false; if (generation === sequence.current) {setBusy(false); setLoading(false);}}
  }
  return <section className="portal-panel portal-keys"><div className="portal-panel-heading"><div><h2>我的调用密钥</h2><p className="portal-small">密钥只展示一次，请通过安全渠道保存。</p></div>
    <button className="portal-button" disabled={busy || loading} onClick={() => void refresh()}>刷新列表</button></div>
    {data?.dispatch_enabled === false && <div className="portal-notice" role="status"><strong>密钥已创建，不代表模型调用已开放。</strong><p className="portal-small">{data.model_discovery_enabled ? '已启用的密钥可读取模型目录。' : '模型目录暂不可读取。'}{data.dispatch_note}</p></div>}
    <details className="portal-key-help"><summary>接入说明与调用规则</summary>
    <p className="portal-small">由公共池统一调度，不固定使用你添加的账号。多个密钥共用你的积分与请求锁，不绑定来源 IP；生成密钥不会解除调用保护。</p>
    <p className="portal-small">仅限 DeepSeek 国内模型：{data?.allowed_models.join(' / ') || '正在读取白名单'}。模型实际可用性以网关目录为准。</p>
    <label className="portal-field">OpenAI 兼容 Base URL<input className="portal-input" value={base} readOnly onFocus={event => event.target.select()} /></label>
    <p className="portal-small">单用户一次一个计费请求；费用缺失或流中断会保留冻结，需核对后恢复，不当成免费调用。</p>
    <p className="portal-small">按调用者的实际积分消耗扣费，余额为零拒绝新请求。单次实扣不超过本次冻结余额，超出部分由平台承担，不扣未来签到或后来赠送的积分。</p>
    </details>
    {loading && <p className="portal-empty" role="status">正在读取密钥…</p>}
    {notice && <p className="portal-small" role="status">{notice}</p>}
    {error && <p className="portal-error" role="alert">{error}</p>}
    {secret && <div className="portal-notice" role="status"><strong>明文仅显示这一次，请立即保存。</strong>
      <label className="portal-field">新密钥<input className="portal-input" value={secret.value} readOnly autoComplete="off" spellCheck={false} onFocus={event => event.target.select()} /></label>
      <button className="portal-button" onClick={() => setSecret(null)}>我已保存，隐藏明文</button></div>}
    <form className="portal-key-create" onSubmit={event => {event.preventDefault(); if (secret || needsRefresh || !data || data.items.filter(key => key.enabled).length >= 5) return;
      void run(() => portalFetch<{id: number; key: string}>('portal/keys', {method: 'POST', body: JSON.stringify({label})}),
        result => setSecret({id: result.id, value: result.key}), '密钥已生成');
    }}><label className="portal-field">密钥名称<input className="portal-input" required maxLength={60} value={label} disabled={busy} onChange={event => setLabel(event.target.value)} /></label>
      <button className="portal-button primary" disabled={busy || loading || !!secret || needsRefresh || !data || data.items.filter(key => key.enabled).length >= 5}>{busy ? '处理中…' : secret ? '请先保存新密钥' : needsRefresh ? '请先刷新核对' : '生成密钥'}</button></form>
    {data?.items.map(key => <article className="portal-account" key={key.id}><div><div className="portal-account-name">{key.label}</div>
      <p className="portal-small">{key.prefix}… · {portalTime(key.created_at)} · {key.enabled ? '已启用' : '已失效'}</p></div>
      <button className="portal-button" disabled={busy || needsRefresh || !key.enabled} onClick={() => setRevoke(key)}>撤销</button></article>)}
    {data && !data.items.length && <p className="portal-empty">还没有调用密钥。密钥明文不会保存在浏览器本地存储。</p>}
    {revoke && <Dialog open onOpenChange={open => { if (!open && !inFlight.current) setRevoke(null); }}><DialogContent className="portal-ops-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => {if (inFlight.current) event.preventDefault();}} onInteractOutside={event => {if (inFlight.current) event.preventDefault();}}>
      <DialogHeader><DialogTitle>撤销密钥 · {revoke.label}</DialogTitle><DialogDescription>撤销后不能恢复。正在执行的请求仍按实际用量结算，不会因此解除费用未知冻结。</DialogDescription></DialogHeader>
      <DialogBody><div className="portal-ops-form">{error && <p className="portal-error" role="alert">{error}</p>}<div className="portal-actions"><button className="portal-button" disabled={busy} onClick={() => setRevoke(null)}>取消</button><button className="portal-button primary" disabled={busy || needsRefresh} onClick={() => void run(() => portalFetch(`portal/keys/${revoke.id}`, {method: 'DELETE'}), () => {
        if (secret?.id === revoke.id) setSecret(null);
        setData(previous => previous ? {...previous, items: previous.items.map(key => key.id === revoke.id ? {...key, enabled: false} : key)} : previous);
        setRevoke(null);
      }, '密钥已撤销')}>{busy ? '正在撤销…' : '确认撤销'}</button></div></div></DialogBody>
    </DialogContent></Dialog>}
  </section>;
}
