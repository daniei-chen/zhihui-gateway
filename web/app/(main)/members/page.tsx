"use client";

import {useCallback, useEffect, useRef, useState} from 'react';
import Link from 'next/link';
import {ArrowUpRight, CalendarCheck, ChevronDown, CircleHelp, Coins, Plus, RefreshCw, Search, ShieldCheck, Users, Wallet} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {PortalError, portalFetch, points, portalTime, canCheckin, checkinLabel, type PortalMember} from '@/lib/portal-api';
import {filterPortalMembers} from '@/lib/portal-members';
import {useT} from '@/lib/i18n/provider';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';
import '../../portal/portal.css';
import './members.css';
import WalletOperations from './WalletOperations';
import BonusDialog from './BonusDialog';
import PasswordDialog from './PasswordDialog';
import CreateMemberDialog from './CreateMemberDialog';
import RegistrationInvites from './RegistrationInvites';

export default function PortalMembersPage() {
  const t = useT();
  const {isAdmin, me} = useAuth();
  const canGiveBonus = isAdmin && me?.username === 'admin';
  const [creating, setCreating] = useState(false);
  const [bonusUser, setBonusUser] = useState<{id: string; username: string} | null>(null);
  const [passwordUser, setPasswordUser] = useState<{id: string; username: string} | null>(null);
  const [walletVersion, setWalletVersion] = useState(0);
  const [members, setMembers] = useState<PortalMember[] | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [automatic, setAutomatic] = useState(false);
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState('all');
  const [tab, setTab] = useState('users');
  const sequence = useRef(0);
  const mutation = useRef(false);
  const invalidateLoads = useCallback(() => { sequence.current++; }, []);
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!isAdmin) return;
    const generation = ++sequence.current;
    try {
      const result = await portalFetch<{users: PortalMember[]; automatic_checkin: boolean}>('portal-admin/users', {signal});
      if (!signal?.aborted && generation === sequence.current) {
        setMembers(result.users); setAutomatic(result.automatic_checkin); setError('');
      }
    } catch (err) {
      if (!signal?.aborted && generation === sequence.current) {
        setError(err instanceof Error ? err.message : t('portal.directory.errRead'));
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) setMembers(null);
      }
      throw err;
    }
  }, [isAdmin, t]);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || document.hidden) return;
      inFlight = true;
      try { await load(controller.signal); } catch { /* Refresh errors render on the page. */ }
      finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 20000);
    return () => { controller.abort(); invalidateLoads(); window.clearInterval(timer); };
  }, [load, invalidateLoads]);
  async function action(key: string, run: () => Promise<void>) {
    if (mutation.current) return;
    mutation.current = true; setBusy(key); setError(''); setNotice('');
    try { await run(); await load(); } catch (err) { setError(err instanceof Error ? err.message : t('portal.directory.actionUncertain')); }
    finally { mutation.current = false; setBusy(''); }
  }
  const rows = members || [];
  const filtered = filterPortalMembers(rows, query, status);
  const enabled = rows.filter(member => member.enabled).length;
  const accounts = rows.reduce((sum, member) => sum + member.account_count, 0);
  const balance = rows.reduce((sum, member) => sum + member.balance_units, 0);
  const checkin = rows.reduce((sum, member) => sum + member.today_checkin_units, 0);
  const dispatch = rows.length ? rows.every(member => member.dispatch_enabled) : null;
  const pool = rows.length ? rows.every(member => member.pool_enabled) : null;
  const dispatchState = dispatch === null ? t('portal.directory.dispatchUnknown') : dispatch ? t('portal.directory.dispatchOpen') : t('portal.directory.dispatchClosed');
  const poolState = pool === null ? t('portal.directory.dispatchUnknown') : pool ? t('portal.directory.poolOn') : t('portal.directory.poolOff');
  if (!isAdmin) return <section className="portal-panel"><h1>{t('portal.directory.forbiddenTitle')}</h1><p className="portal-muted">{t('portal.directory.forbiddenBody')}</p></section>;
  return <div className="portal-management member-workspace">
    <header className="member-heading"><div><p className="portal-eyebrow">CUSTOMER WORKSPACE</p><h1>{t('portal.directory.title')}</h1><p>{t('portal.directory.subtitle')}</p></div>
      <div className="portal-actions"><Link className="portal-button" href="/portal" target="_blank" rel="noopener noreferrer">{t('portal.directory.portalEntry')}<ArrowUpRight size={15} /></Link>
        <button className="portal-button primary" onClick={() => setCreating(true)} disabled={!!busy}><Plus size={16} />{t('portal.directory.createUser')}</button></div></header>
    <section className="member-overview" aria-label={t('portal.directory.overview')}>
      {[{label: t('portal.directory.statUsers'), value: rows.length, suffix: t('portal.directory.statUsersSuffix', {count: enabled}), icon: Users},
        {label: t('portal.directory.statAccounts'), value: accounts, suffix: t('portal.directory.statAccountsSuffix'), icon: Wallet},
        {label: t('portal.directory.statCredits'), value: points(balance), suffix: t('portal.directory.statCreditsSuffix'), icon: Coins},
        {label: t('portal.directory.statCheckin'), value: points(checkin), suffix: t('portal.directory.statCheckinSuffix'), icon: CalendarCheck}].map(item =>
        <article key={item.label}><div className="member-stat-label"><span>{item.label}</span><item.icon size={16} /></div>
          <strong>{members === null ? '—' : item.value}</strong><span className="member-stat-foot">{item.suffix}</span></article>)}
    </section>
    <div className="member-service-state"><ShieldCheck size={16} /><p><strong>{t('portal.directory.serviceTitle')}</strong><span>{t('portal.directory.serviceDispatch', {state: dispatchState})} · {t('portal.directory.servicePool', {state: poolState})}{t('portal.directory.serviceNote')}</span></p>
      <details><summary><CircleHelp size={14} />{t('portal.directory.help')}</summary><p>{t('portal.directory.helpBody')}</p></details></div>
    {error && <p className="portal-error" role="alert">{error}{members !== null && ` · ${t('portal.directory.stale')}`}</p>}
    {notice && <p className="member-success" role="status">{notice}</p>}
    <RegistrationInvites />
    <Tabs value={tab} onValueChange={setTab} className="member-tabs">
      <TabsList aria-label={t('portal.directory.tabs')} className="member-tabs-list">
        <TabsTrigger value="users"><Users size={15} />{t('portal.directory.tabUsers')}</TabsTrigger>
        <TabsTrigger value="checkin"><CalendarCheck size={15} />{t('portal.directory.tabCheckin')}</TabsTrigger>
        <TabsTrigger value="wallet"><Wallet size={15} />{t('portal.directory.tabWallet')}</TabsTrigger>
      </TabsList>
      <TabsContent value="users">
        <section className="member-directory" aria-labelledby="member-list-title">
          <div className="member-directory-toolbar"><div><h2 id="member-list-title">{t('portal.directory.listTitle')} <span>{members === null ? '—' : rows.length}</span></h2><p>{t('portal.directory.listDesc')}</p></div>
            <div className="member-filters"><label className="member-search"><Search size={15} /><input aria-label={t('portal.directory.search')} placeholder={t('portal.directory.search')} value={query} onChange={event => setQuery(event.target.value)} /></label>
              <select aria-label={t('portal.directory.statusFilter')} value={status} onChange={event => setStatus(event.target.value)}><option value="all">{t('portal.directory.statusAll')}</option><option value="enabled">{t('portal.directory.statusEnabled')}</option><option value="disabled">{t('portal.directory.statusDisabled')}</option></select>
              <button className="portal-button member-refresh" aria-label={t('portal.directory.refresh')} disabled={!!busy} onClick={() => void load().catch(() => {})}><RefreshCw size={15} /></button></div></div>
          <div className="member-list-columns" aria-hidden="true"><span>{t('portal.directory.colUser')}</span><span>{t('portal.directory.colAccounts')}</span><span>{t('portal.directory.colCredits')}</span><span>{t('portal.directory.colCheckin')}</span><span>{t('portal.directory.colManage')}</span></div>
          {members === null ? <p className="portal-empty" role="status">{error ? t('portal.directory.loadFailed') : t('portal.directory.loading')}</p> : !filtered.length ?
            <div className="member-empty"><Users size={28} /><h3>{rows.length ? t('portal.directory.noMatch') : t('portal.directory.startFirst')}</h3><p>{rows.length ? t('portal.directory.noMatchHint') : t('portal.directory.startHint')}</p>
              {!rows.length && <button className="portal-button primary" onClick={() => setCreating(true)}><Plus size={15} />{t('portal.directory.createUser')}</button>}</div>
            : filtered.map(member => <article className="member-row" key={member.id}>
              <div className="member-row-main"><div className="member-identity"><span className="member-avatar" aria-hidden="true">{member.username.slice(0, 2).toUpperCase()}</span><div><h3>{member.username}</h3><span className={`member-status ${member.enabled ? '' : 'disabled'}`}><i />{member.enabled ? t('portal.directory.statusEnabled') : t('portal.directory.statusDisabled')}</span></div></div>
                <div className="member-value"><span className="member-mobile-label">{t('portal.directory.colAccounts')}</span><strong>{member.account_count}<small> {t('portal.directory.unitAccounts')}</small></strong></div>
                <div className="member-value"><span className="member-mobile-label">{t('portal.directory.colCredits')}</span><strong>{points(member.balance_units)}</strong>{member.held_units > 0 && <small>{t('portal.directory.frozen', {credits: points(member.held_units)})}</small>}</div>
                <div className="member-value"><span className="member-mobile-label">{t('portal.directory.colCheckin')}</span><strong className="member-positive">{member.today_checkin_units > 0 ? '+' : ''}{points(member.today_checkin_units)}</strong></div>
                <div className="member-row-actions">{canGiveBonus && <button className="member-text-button" disabled={!!busy || !member.enabled} onClick={() => setBonusUser(member)}>{t('portal.directory.bonus')}</button>}
                  <button className="member-text-button" disabled={!!busy} onClick={() => setPasswordUser(member)}>{t('portal.directory.resetPassword')}</button>
                  <button className="member-text-button member-muted-action" disabled={!!busy} onClick={() => void action(member.id, async () => {
                    await portalFetch(`portal-admin/users/${member.id}`, {method: 'PATCH', body: JSON.stringify({enabled: !member.enabled})});
                    setNotice(member.enabled ? t('portal.directory.noticeDisabled') : t('portal.directory.noticeEnabled'));
                  })}>{member.enabled ? t('portal.directory.disable') : t('portal.directory.enable')}</button></div></div>
              <details className="member-account-details"><summary><span>{t('portal.directory.openedAt', {time: portalTime(member.created_at)})}{member.debt_units > 0 && ` · ${t('portal.directory.debtPending', {credits: points(member.debt_units)})}`}</span><span>{t('portal.directory.accountDetails')}<ChevronDown size={14} /></span></summary>
                <div className="member-account-list">{!member.accounts.length ? <p className="member-form-note">{t('portal.directory.noAccounts')}</p> : member.accounts.map(account =>
                  <div className="portal-account" key={account.id}><div><div className="portal-account-name">{account.label}</div><p className="portal-small">{t(checkinLabel(account.checkin_state))} · {account.initial_state === 'confirmed' ? t('portal.directory.initialConfirmed') : t('portal.directory.initialPending')}</p></div>
                    <button className="portal-button" disabled={!!busy || !member.enabled || !canCheckin(account)} onClick={() => void action(account.id, async () => {
                      const result = await portalFetch<{message: string}>(`portal-admin/accounts/${account.id}/checkin`, {method: 'POST'}); setNotice(result.message);
                    })}>{busy === account.id ? t('portal.directory.accountBusy') : canCheckin(account) ? t('portal.directory.accountCheckin') : t('portal.directory.accountSynced')}</button></div>)}</div></details>
            </article>)}
          <footer className="member-directory-footer"><ShieldCheck size={14} />{t('portal.directory.footer')}</footer>
        </section>
      </TabsContent>
      <TabsContent value="checkin"><section className="portal-panel member-checkin-panel"><div className="member-section-heading"><span className="member-dialog-icon"><CalendarCheck size={22} /></span><div><h2>{t('portal.directory.checkinTitle')}</h2><p className="portal-small">{t('portal.directory.checkinDesc')}</p></div></div>
        <div className="member-checkin-state"><span className={`member-status ${automatic ? '' : 'disabled'}`}><i />{t('portal.directory.autoLabel')}{automatic ? t('portal.directory.autoOn') : t('portal.directory.autoOff')}</span><p>{t('portal.directory.checkinNote')}</p></div>
        <div className="portal-actions"><button className="portal-button" disabled={!!busy || members === null} onClick={() => void action('automatic', async () => {
          await portalFetch('portal-admin/automatic-checkin', {method: 'PUT', body: JSON.stringify({enabled: !automatic})}); setNotice(automatic ? t('portal.directory.autoOffNotice') : t('portal.directory.autoOnNotice'));
        })}>{automatic ? t('portal.directory.autoDisable') : t('portal.directory.autoEnable')}</button><button className="portal-button primary" disabled={!!busy || members === null} onClick={() => void action('batch', async () => {
          const result = await portalFetch<{message: string}>('portal-admin/checkin-all', {method: 'POST'}); setNotice(result.message);
        })}>{busy === 'batch' ? t('portal.directory.batchBusy') : t('portal.directory.batch')}</button></div></section></TabsContent>
      <TabsContent value="wallet">{tab === 'wallet' && <WalletOperations key={walletVersion} onChanged={load} />}</TabsContent>
    </Tabs>
    {creating && <CreateMemberDialog close={() => setCreating(false)} saved={load} />}
    {canGiveBonus && bonusUser && <BonusDialog user={bonusUser} close={() => setBonusUser(null)} saved={async () => { await load(); setWalletVersion(value => value + 1); }} />}
    {passwordUser && <PasswordDialog user={passwordUser} close={() => setPasswordUser(null)} saved={load} />}
  </div>;
}
