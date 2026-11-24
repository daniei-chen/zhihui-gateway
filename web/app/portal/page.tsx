'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {ArrowUpRight, CalendarCheck, Check, ChevronDown, Clock3, Coins, History, KeyRound, LayoutDashboard, LogOut, Plus, RefreshCw, ScanLine, ShieldCheck, UserRound, Wallet, WifiOff} from 'lucide-react';
import {PortalError, portalFetch, points, portalTime, canCheckin, checkinLabel} from '@/lib/portal-api';
import {useT} from '@/lib/i18n/provider';
import {checkinSummary} from '@/lib/portal-display';
import {usePortalWorkspace} from '@/lib/use-portal-workspace';
import './portal.css';
import PortalKeys from './PortalKeys';
import PortalLogin from './PortalLogin';
import PortalRequests from './PortalRequests';
import {BrandHome} from '@/components/common/layout/BrandHome';
import {ConsoleAtmosphere} from '@/components/common/layout/ConsoleAtmosphere';
import {StatCard} from '@/components/common/layout/StatCard';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';

type Tab = 'overview' | 'accounts' | 'credits' | 'requests' | 'keys';

export default function UserPortal() {
  const workspace = usePortalWorkspace();
  return <PortalView key={workspace.me?.id || (workspace.anonymous ? 'anonymous' : 'connecting')} workspace={workspace} />;
}

function PortalView({workspace}: {workspace: ReturnType<typeof usePortalWorkspace>}) {
  const t = useT();
  const {me, data, anonymous, pending, error: readError, reload, connect, reset} = workspace;
  const [tab, setTab] = useState<Tab>('overview');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [needsReview, setNeedsReview] = useState(false);
  const [qr, setQr] = useState<{session: string; auth_url: string; expires_in: number} | null>(null);
  const [qrError, setQrError] = useState('');
  const [qrExpired, setQrExpired] = useState(false);
  const qrPaused = useRef(false);
  const actionFlight = useRef(false);
  const requiresRead = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  const load = useCallback(async () => {
    const fresh = await reload();
    if (fresh && mounted.current) {requiresRead.current = false; setNeedsReview(false); setError('');}
    return fresh;
  }, [reload]);
  const closeQr = useCallback(async () => {
    const current = qr;
    setQr(null); setQrError(''); setQrExpired(false); qrPaused.current = false;
    if (!current) return;
    requiresRead.current=true;setNeedsReview(true);
    try {
      const result = await portalFetch<{state?: string}>('portal/auth/cancel', {method: 'POST', body: JSON.stringify({session: current.session})});
      if (mounted.current && result.state === 'complete') {setNotice(t('portal.home.authDoneReading')); await load();}
    }
    catch (err) {
      if (!mounted.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) reset();
      else {requiresRead.current = true; setNeedsReview(true); setNotice(t('portal.home.closedLocalWindow'));}
    }
  }, [qr, reset, load, t]);

  useEffect(() => {
    if (!qr) return;
    let active = true, polling = false;
    const controller = new AbortController();
    const deadline = Date.now() + qr.expires_in * 1000;
    const timer = window.setInterval(async () => {
      if (!active) return;
      // An expiry of the displayed QR is not proof that an in-flight import
      // stopped. Let that exact poll finish before changing local ownership.
      if (polling) return;
      if (Date.now() >= deadline) {
        qrPaused.current=true;requiresRead.current=true;setNeedsReview(true);setQrExpired(true);
        setQrError(t('portal.home.qrExpired'));
        return;
      }
      if (qrPaused.current) return;
      polling = true;
      try {
        const result = await portalFetch<{state: string; message?: string}>('portal/auth/poll', {
          method: 'POST', body: JSON.stringify({session: qr.session}), signal: controller.signal,
        });
        if (!active) return;
        if (result.state === 'complete') { setQr(null); setNotice(result.message || t('portal.home.accountAdded')); await load(); }
        else if (result.state === 'expired') {setQr(null);requiresRead.current=true;setNeedsReview(true);setNotice(t('portal.home.qrInvalid'));}
        else if (result.state === 'failed') {setQr(null); requiresRead.current = true; setNeedsReview(true); setError(result.message || t('portal.home.authUncertain'));}
        else if (result.message) setNotice(result.message);
      } catch (err) {
        if (active) {
          if (err instanceof PortalError && (err.status === 401 || err.status === 403)) reset();
          else {qrPaused.current = true; requiresRead.current=true;setNeedsReview(true);setQrError(t('portal.home.pollInterrupted'));}
        }
      } finally { polling = false; }
    }, 2500);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [qr, load, reset, t]);

  useEffect(() => {
    if (!qr) return;
    const previous = document.activeElement as HTMLElement | null;
    const dialog = document.getElementById('portal-qr-dialog');
    const controls = () => Array.from(dialog?.querySelectorAll<HTMLElement>('a[href],button') || []);
    controls()[0]?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') void closeQr();
      if (event.key !== 'Tab') return;
      const items = controls(), first = items[0], last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener('keydown', keydown);
    return () => { document.removeEventListener('keydown', keydown); previous?.focus(); };
  }, [qr, closeQr]);

  async function action(key: string, run: () => Promise<void>) {
    if (actionFlight.current || (requiresRead.current && key !== 'logout') || !mounted.current) return;
    actionFlight.current = true;
    setBusy(key); setError(''); setNotice('');
    try { await run(); } catch (err) {
      if (mounted.current) {
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) reset();
        else if (!(err instanceof PortalError) || err.status >= 500) {
          requiresRead.current = true; setNeedsReview(true); setError(t('portal.home.uncertainAction'));
        } else setError(err.message);
      }
    }
    finally { actionFlight.current = false; if (mounted.current) setBusy(''); }
  }
  async function login(username: string, password: string) {
    await portalFetch('portal/login', {method: 'POST', body: JSON.stringify({username, password})});
    if (mounted.current) await connect();
  }

  const summary = data ? checkinSummary(data.accounts) : null;
  const actionBlocked = !!busy || needsReview;
  async function add() {
    await action('add', async () => {
      const result = await portalFetch<NonNullable<typeof qr>>('portal/auth/start', {method: 'POST'});
      const url = new URL(result.auth_url);
      if (url.protocol !== 'https:') throw new Error(t('portal.home.authLinkInvalid'));
      if (mounted.current) {qrPaused.current = false;setQrExpired(false);setQrError('');setQr(result);}
    });
  }
  async function checkin(id: string) {
    await action(id, async () => {
      const result = await portalFetch<{message: string}>(`portal/accounts/${id}/checkin`, {method: 'POST'});
      if (!mounted.current) return;
      setNotice(result.message); await load();
    });
  }

  return <div className="user-portal"><ConsoleAtmosphere /><a className="skip-link" href="#portal-main">{t('portal.home.skipToContent')}</a><div className="portal-wrap">
    <header className="portal-head"><div className="portal-brand-group"><BrandHome href="/portal" /><span className="portal-workspace-label">{t('portal.home.workspaceLabel')}</span></div>
      {me ? <div className="portal-actions"><span className="portal-session"><UserRound size={14} />{me.username}</span><button className="portal-button portal-logout" disabled={!!busy} onClick={() => void action('logout', async () => {
        await portalFetch('portal/logout', {method: 'POST'});
        if (mounted.current) reset();
      })}><LogOut size={14} />{t('portal.home.logout')}</button></div> : <span className="portal-private-label"><ShieldCheck size={14} />{t('portal.home.privateLabel')}</span>}</header>
    {(error || readError) && <div className="portal-error" role="alert">{error || readError}{data && readError && ' · ' + t('portal.home.staleSuffix')}<button className="portal-button" disabled={pending} onClick={() => void load()}>{t('portal.home.reload')}</button></div>}
    {notice && <div className="portal-notice portal-feedback" role="status">{notice}</div>}
    <main id="portal-main" tabIndex={-1}>
    {!data && pending && !anonymous ? <div className="portal-loading" role="status" aria-busy="true"><span>{t('portal.home.loading')}</span><div className="portal-loading-cards" aria-hidden="true">{[0,1,2,3].map(index => <i key={index} />)}</div></div>
      : anonymous ? <PortalLogin login={login} /> : me && data ? <>
      <section className="portal-hero"><div><p className="portal-eyebrow">{t('portal.home.eyebrow')}</p><h1 className="portal-title">{t('portal.home.greeting', {name: me.username})}</h1><p className="portal-muted">{t('portal.home.heroBody')}</p></div>
        <div className="portal-actions"><button className="portal-button" onClick={() => void load()} disabled={!!busy || pending}><RefreshCw size={15} className={pending ? 'portal-spinning' : ''} />{pending ? t('portal.home.syncing') : t('portal.home.refresh')}</button><button className="portal-button" disabled={actionBlocked || !data.accounts.some(canCheckin)} onClick={() => void action('batch', async () => {
          const result = await portalFetch<{message: string}>('portal/checkin-all', {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load();
        })}><CalendarCheck size={15} />{busy === 'batch' ? t('portal.home.checkinBusy') : summary?.ready ? t('portal.home.checkinAll', {count: summary.ready}) : summary?.pending ? t('portal.home.checkinReviewing') : summary?.total ? t('portal.home.checkinDoneToday') : t('portal.home.checkinUnavailable')}</button><button className="portal-button primary" onClick={() => void add()} disabled={actionBlocked}><Plus size={16} />{t('portal.home.scanAdd')}</button></div></section>
      <section className="portal-stats" aria-label={t('portal.home.statsLabel')}>
        <StatCard label={t('portal.home.creditsLabel')} value={points(data.balance_units, data.scale)} hint={t('portal.home.creditsHint')} icon={Coins} tone="info" />
        <StatCard label={t('portal.home.checkinToday')} value={points(data.today_checkin_units, data.scale)} hint={t('portal.home.checkinTodayHint')} icon={CalendarCheck} />
        <StatCard label={t('portal.home.accountsStat')} value={data.account_count} hint={t('portal.home.accountsStatHint', {count: data.active_account_count})} icon={UserRound} />
        <StatCard label={t('portal.home.expiringStat')} value={points(data.expiring_7d_units, data.scale)} hint={t('portal.home.expiringStatHint')} icon={Clock3} />
      </section>
      <Tabs value={tab} onValueChange={value => setTab(value as Tab)} className="portal-workspace-tabs">
      <TabsList aria-label={t('portal.home.tabsLabel')} className="portal-tabs">{([{key: 'overview', label: t('portal.home.tabOverview'), icon: LayoutDashboard}, {key: 'accounts', label: t('portal.home.tabAccounts'), icon: ScanLine}, {key: 'credits', label: t('portal.home.tabCredits'), icon: Wallet}, {key: 'keys', label: t('portal.home.tabKeys'), icon: KeyRound}, {key: 'requests', label: t('portal.home.tabRequests'), icon: History}] as const).map(item => <TabsTrigger key={item.key} value={item.key}><item.icon size={14} />{item.label}</TabsTrigger>)}</TabsList>
      <TabsContent value={tab}>
      <div className="portal-service-summary"><ShieldCheck size={16} /><div><strong>{data.dispatch_enabled ? t('portal.home.dispatchOpen') : t('portal.home.dispatchClosed')}</strong><p>{data.dispatch_enabled ? t('portal.home.dispatchOpenNote') : t('portal.home.dispatchClosedNote')}</p>
        <details className="portal-service-details"><summary>{t('portal.home.serviceDetails')}<ChevronDown size={13} /></summary><p>{data.pool_enabled ? t('portal.home.poolEnabledNote') : t('portal.home.poolDisabledNote')} {!data.dispatch_enabled && data.dispatch_note}</p><p>{t('portal.home.billingNote')}</p></details>
      </div><span>{t('portal.home.heldLabel')} <b>{points(data.held_units || 0, data.scale)}</b>{data.debt_units > 0 ? ` · ${t('portal.home.debtSuffix', {amount: points(data.debt_units, data.scale)})}` : ''}</span></div>
      {data.pending_pool_units > 0 && <p className="portal-small">{t('portal.home.pendingPoolNote', {amount: points(data.pending_pool_units, data.scale), callable: points(data.callable_units, data.scale)})}</p>}
      {data.pool_enabled && data.callable_units <= 0 && !data.held_units && !data.debt_units && <div className="portal-notice" role="status">{t('portal.home.quotaExhausted')}</div>}
      {tab === 'keys' && <PortalKeys key={me.id} onUnauthorized={reset} />}
      {(tab === 'overview' || tab === 'accounts') && <div className={tab === 'overview' ? 'portal-grid' : ''}><section className="portal-panel"><div className="portal-panel-heading"><div><h2>{t('portal.home.accountsTitle')} <span className="portal-count">{data.account_count}</span></h2><p className="portal-small">{t('portal.home.accountsDesc')}</p></div></div>
        {!!summary?.total && <div className="portal-checkin-summary"><CalendarCheck size={15} /><span>{t('portal.home.checkinSummaryLabel')} <strong>{summary.confirmed} / {summary.total}</strong></span><span>{summary.pending ? t('portal.home.checkinPendingCount', {count: summary.pending}) : summary.ready ? t('portal.home.checkinReadyCount', {count: summary.ready}) : t('portal.home.checkinAllConfirmed')}</span></div>}
        {!data.accounts.length ? <div className="portal-empty"><ScanLine size={30} style={{margin: '0 auto 14px', color: '#8fa9c5'}} />{t('portal.home.emptyAccounts')}<br /><button className="portal-button" onClick={() => void add()} disabled={actionBlocked}>{t('portal.home.addFirstAccount')}</button></div> : data.accounts.map(account => <article key={account.id} className="portal-account"><div><div className="portal-account-name">{account.label}</div><span className={`portal-badge ${account.checkin_state === 'confirmed' ? '' : 'pending'}`}>{account.checkin_state === 'confirmed' ? <Check size={12} /> : <Clock3 size={12} />}{t(checkinLabel(account.checkin_state))}</span><p className="portal-small">{t('portal.home.joinedAt', {time: portalTime(account.created_at)})} · {account.initial_state === 'confirmed' ? t('portal.home.initialConfirmed') : t('portal.home.initialPending')} · {account.pool_state === 'public' ? t('portal.home.poolContributed') : t('portal.home.poolPending')}</p></div>
          <div className="portal-actions">{account.initial_state === 'confirmed' && account.pool_state !== 'public' && <button className="portal-button" disabled={actionBlocked || !data.pool_enabled} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/join-public-pool`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>{t('portal.home.joinPool')}</button>}{account.checkin_state === 'confirmed_uncredited' && <button className="portal-button" disabled={actionBlocked} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/reconcile-checkin`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>{t('portal.home.reconcileCheckin')}</button>}{account.initial_state !== 'confirmed' ? <button className="portal-button" disabled={actionBlocked} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/verify-initial`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>{t('portal.home.verifyInitial')}</button> : <button className="portal-button" disabled={actionBlocked || !canCheckin(account)} onClick={() => void checkin(account.id)}>{busy === account.id ? t('portal.home.processing') : canCheckin(account) ? t('portal.home.checkinNow') : t('portal.home.stateSynced')}</button>}</div></article>)}</section>
        {tab === 'overview' && <section className="portal-panel"><h2>{t('portal.home.expiringTitle')}</h2><p className="portal-small">{t('portal.home.expiringDesc')}</p>{data.grants.length ? data.grants.slice(0, 6).map((grant, index) => <div key={`${grant.expires_at}-${index}`} className="portal-account"><div><div className="portal-account-name">{grant.source === 'initial' ? t('portal.home.sourceInitial') : grant.source === 'admin_bonus' ? t('portal.home.sourceBonus') : t('portal.home.sourceCheckin')}</div><p className="portal-small">{t('portal.home.grantExpiresAt', {time: portalTime(grant.expires_at)})}</p></div><strong>{points(grant.remaining_units, data.scale)}</strong></div>) : <p className="portal-empty">{t('portal.home.noGrants')}</p>}</section>}</div>}
      {tab === 'credits' && <section className="portal-panel"><h2>{t('portal.home.creditsTitle')}</h2><p className="portal-small">{t('portal.home.creditsDesc')}</p><div className="portal-table-wrap"><table className="portal-table"><thead><tr><th>{t('portal.home.colTime')}</th><th>{t('portal.home.colKind')}</th><th>{t('portal.home.colAmount')}</th><th>{t('portal.home.colExpires')}</th></tr></thead><tbody>{data.events.map((event, index) => <tr key={`${event.ts}-${index}`}><td>{portalTime(event.ts)}</td><td>{{admin_bonus: t('portal.home.kindBonus'), initial: t('portal.home.kindInitial'), checkin: t('portal.home.kindCheckin'), expired: t('portal.home.kindExpired'), usage: t('portal.home.kindUsage'), debt_repaid: t('portal.home.kindDebtRepaid'), debt_waived: t('portal.home.kindDebtWaived')}[event.kind] || event.kind}</td><td>{event.amount_units > 0 ? '+' : ''}{points(event.amount_units, data.scale)}</td><td>{event.expires_at ? portalTime(event.expires_at) : '—'}</td></tr>)}</tbody></table></div>{!data.events.length && <p className="portal-empty">{t('portal.home.noEvents')}</p>}</section>}
      {tab === 'requests' && <PortalRequests key={me.id} onUnauthorized={reset} />}
      </TabsContent></Tabs>
      <footer className="portal-data-footer"><span><ShieldCheck size={13} />{t('portal.home.footerPrivacy')}</span><span>{t('portal.home.footerUpdated', {time: portalTime(data.snapshot_at)})}</span></footer>
    </> : <section className="portal-connection" aria-labelledby="portal-connection-title"><WifiOff size={30} /><h1 id="portal-connection-title">{t('portal.home.connectTitle')}</h1><p>{t('portal.home.connectBody')}</p><button className="portal-button primary" disabled={pending} onClick={() => void load()}>{t('portal.home.reconnect')}</button></section>}
    </main>
    {qr && <div className="portal-overlay"><section id="portal-qr-dialog" className="portal-dialog" role="dialog" aria-modal="true" aria-labelledby="portal-qr-title"><p className="portal-eyebrow">{t('portal.home.qrEyebrow')}</p><h2 id="portal-qr-title" style={{fontSize: 21, fontWeight: 650}}>{t('portal.home.qrTitle')}</h2><p className="portal-small">{t('portal.home.qrDesc')}</p><div className="portal-qr"><QRCodeSVG value={qr.auth_url} size={196} /></div><p className="portal-small">{t('portal.home.qrSyncNote')}</p>{qrError && <div className="portal-error" role="alert">{qrError}{!qrExpired&&<button className="portal-button" onClick={() => {qrPaused.current = false; setQrError('');}}>{t('portal.home.retryQr')}</button>}</div>}<div className="portal-actions" style={{justifyContent: 'center', marginTop: 20}}><a className="portal-button primary" href={qr.auth_url} target="_blank" rel="noopener noreferrer">{t('portal.home.openAuth')}<ArrowUpRight size={14} /></a><button className="portal-button" onClick={() => void closeQr()}>{t('portal.home.close')}</button></div></section></div>}
  </div></div>;
}
