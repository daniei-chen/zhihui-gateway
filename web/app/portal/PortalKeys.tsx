'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {BASE_PATH} from '@/lib/base-path';
import {PortalError, portalFetch, portalTime} from '@/lib/portal-api';
import {useT} from '@/lib/i18n/provider';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';

type Key = {id: number; label: string; prefix: string; enabled: boolean; created_at: number};
type Keys = {items: Key[]; allowed_models: string[]; can_initialize?: boolean; single_key?: boolean; dispatch_enabled?: boolean; pool_enabled?: boolean; model_discovery_enabled?: boolean; dispatch_note?: string};

export default function PortalKeys({onUnauthorized}: {onUnauthorized: () => void}) {
  const t = useT();
  const [data, setData] = useState<Keys | null>(null);
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
      else setError(err instanceof Error ? err.message : t('portal.keys.errLoad'));
    }).finally(() => {if (!controller.signal.aborted && generation === sequence.current) setLoading(false);});
    return () => {controller.abort(); invalidate();};
  }, [onUnauthorized, invalidate, t]);
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
        else {setNeedsRefresh(true); setError(t('portal.keys.errRefreshAfter', {action: success}));}
      }
    } catch (err) {
      if (generation !== sequence.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
      else if (!(err instanceof PortalError) || err.status >= 500) {
        setNeedsRefresh(true); setError(t('portal.keys.errUnconfirmed'));
      } else setError(err.message);
    } finally {inFlight.current = false; if (generation === sequence.current) {setBusy(false); setLoading(false);}}
  }
  async function refresh() {
    if (inFlight.current) return;
    inFlight.current = true; setBusy(true); setError('');
    const generation = ++sequence.current;
    try {
      const result = await portalFetch<Keys>('portal/keys');
      if (generation === sequence.current) {setData(result); setNeedsRefresh(false); setNotice(t('portal.keys.refreshed')); setBase(`${window.location.origin}${BASE_PATH}/v1`);}
    } catch (err) {
      if (generation !== sequence.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {setData(null); setSecret(null); onUnauthorized();}
      else setError(err instanceof Error ? err.message : t('portal.keys.errListLoad'));
    } finally {inFlight.current = false; if (generation === sequence.current) {setBusy(false); setLoading(false);}}
  }
  return <section className="portal-panel portal-keys"><div className="portal-panel-heading"><div><h2>{t('portal.keys.title')}</h2><p className="portal-small">{t('portal.keys.desc')}</p></div>
    <button className="portal-button" disabled={busy || loading} onClick={() => void refresh()}>{t('portal.keys.refresh')}</button></div>
    {data?.dispatch_enabled === false && <div className="portal-notice" role="status"><strong>{t('portal.keys.dispatchClosed')}</strong><p className="portal-small">{data.model_discovery_enabled ? t('portal.keys.modelsReadable') : t('portal.keys.modelsUnreadable')}{data.dispatch_note}</p></div>}
    <details className="portal-key-help"><summary>{t('portal.keys.helpSummary')}</summary>
    <p className="portal-small">{t('portal.keys.helpReuse')}</p>
    <p className="portal-small">{t('portal.keys.helpModels', {models: data?.allowed_models.join(' / ') || t('portal.keys.modelsLoading')})}</p>
    <label className="portal-field">{t('portal.keys.baseUrl')}<input className="portal-input" value={base} readOnly onFocus={event => event.target.select()} /></label>
    <p className="portal-small">{t('portal.keys.helpBilling')}</p>
    <p className="portal-small">{t('portal.keys.helpCap')}</p>
    </details>
    {loading && <p className="portal-empty" role="status">{t('portal.keys.loading')}</p>}
    {notice && <p className="portal-small" role="status">{notice}</p>}
    {error && <p className="portal-error" role="alert">{error}</p>}
    {secret && <div className="portal-notice" role="status"><strong>{t('portal.keys.secretOnce')}</strong>
      <label className="portal-field">{t('portal.keys.secretLabel')}<input className="portal-input" value={secret.value} readOnly autoComplete="off" spellCheck={false} onFocus={event => event.target.select()} /></label>
      <button className="portal-button" onClick={() => setSecret(null)}>{t('portal.keys.secretSaved')}</button></div>}
    {data?.can_initialize && <form className="portal-key-create" onSubmit={event => {event.preventDefault(); if (secret || needsRefresh || !data.can_initialize) return;
      void run(() => portalFetch<{id: number; key: string | null; existing?: boolean}>('portal/keys', {method: 'POST', body: '{}'}),
        result => {if (result.key) setSecret({id: result.id, value: result.key});}, t('portal.keys.initReady'));
    }}><button className="portal-button primary" disabled={busy || loading || !!secret || needsRefresh}>{busy ? t('portal.keys.initBusy') : secret ? t('portal.keys.initSaveFirst') : needsRefresh ? t('portal.keys.initRefreshFirst') : t('portal.keys.initEnable')}</button></form>}
    {data?.items.slice(0,1).map(key => <article className="portal-account" key={key.id}><div><div className="portal-account-name">{key.label} · {t('portal.keys.defaultSuffix')}</div>
      <p className="portal-small">{key.prefix}… · {portalTime(key.created_at)} · {key.enabled ? t('portal.keys.enabled') : t('portal.keys.disabled')}</p></div>
      <button className="portal-button" disabled={busy || needsRefresh || !key.enabled} onClick={() => setRevoke(key)}>{t('portal.keys.revoke')}</button></article>)}
    {data && !data.items.length && <p className="portal-empty">{t('portal.keys.empty')}</p>}
    {!!data?.items.length && !secret && <p className="portal-small">{t('portal.keys.keepStored')}</p>}
    {revoke && <Dialog open onOpenChange={open => { if (!open && !inFlight.current) setRevoke(null); }}><DialogContent className="portal-ops-dialog" showCloseButton={!busy}
      onEscapeKeyDown={event => {if (inFlight.current) event.preventDefault();}} onInteractOutside={event => {if (inFlight.current) event.preventDefault();}}>
      <DialogHeader><DialogTitle>{t('portal.keys.revokeTitle', {label: revoke.label})}</DialogTitle><DialogDescription>{t('portal.keys.revokeDesc')}</DialogDescription></DialogHeader>
      <DialogBody><div className="portal-ops-form">{error && <p className="portal-error" role="alert">{error}</p>}<div className="portal-actions"><button className="portal-button" disabled={busy} onClick={() => setRevoke(null)}>{t('portal.keys.cancel')}</button><button className="portal-button primary" disabled={busy || needsRefresh} onClick={() => void run(() => portalFetch(`portal/keys/${revoke.id}`, {method: 'DELETE'}), () => {
        if (secret?.id === revoke.id) setSecret(null);
        setData(previous => previous ? {...previous, items: previous.items.map(key => key.id === revoke.id ? {...key, enabled: false} : key)} : previous);
        setRevoke(null);
      }, t('portal.keys.revoked'))}>{busy ? t('portal.keys.revokeBusy') : t('portal.keys.revokeConfirm')}</button></div></div></DialogBody>
    </DialogContent></Dialog>}
  </section>;
}
