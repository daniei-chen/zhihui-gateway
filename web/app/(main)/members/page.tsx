"use client";

import {useCallback, useEffect, useRef, useState} from 'react';
import Link from 'next/link';
import {ArrowUpRight, CalendarCheck, ChevronDown, CircleHelp, Coins, Plus, RefreshCw, Search, ShieldCheck, Users, Wallet} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {PortalError, portalFetch, points, portalTime, canCheckin, checkinLabel, type PortalMember} from '@/lib/portal-api';
import {filterPortalMembers} from '@/lib/portal-members';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';
import '../../portal/portal.css';
import './members.css';
import WalletOperations from './WalletOperations';
import BonusDialog from './BonusDialog';
import PasswordDialog from './PasswordDialog';
import CreateMemberDialog from './CreateMemberDialog';
import RegistrationInvites from './RegistrationInvites';

export default function PortalMembersPage() {
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
        setError(err instanceof Error ? err.message : '读取失败');
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) setMembers(null);
      }
      throw err;
    }
  }, [isAdmin]);
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
    try { await run(); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作结果待确认，请刷新核对，不会自动重试'); }
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
  if (!isAdmin) return <section className="portal-panel"><h1>用户工作台管理</h1><p className="portal-muted">请使用管理员本人会话访问。普通用户从用户端入口登录。</p></section>;
  return <div className="portal-management member-workspace">
    <header className="member-heading"><div><p className="portal-eyebrow">CUSTOMER WORKSPACE</p><h1>用户工作台</h1><p>管理用户、绑定账号与积分钱包。权限分开，状态同步。</p></div>
      <div className="portal-actions"><Link className="portal-button" href="/portal" target="_blank" rel="noopener noreferrer">用户端入口<ArrowUpRight size={15} /></Link>
        <button className="portal-button primary" onClick={() => setCreating(true)} disabled={!!busy}><Plus size={16} />开通用户</button></div></header>
    <section className="member-overview" aria-label="用户工作台概览">
      {[{label: '已开通用户', value: rows.length, suffix: `${enabled} 位已启用`, icon: Users},
        {label: '绑定账号', value: accounts, suffix: '用户扫码添加', icon: Wallet},
        {label: '可用积分合计', value: points(balance), suffix: '各用户独立记账', icon: Coins},
        {label: '今日签到到账', value: points(checkin), suffix: '每批最长30天有效', icon: CalendarCheck}].map(item =>
        <article key={item.label}><div className="member-stat-label"><span>{item.label}</span><item.icon size={16} /></div>
          <strong>{members === null ? '—' : item.value}</strong><span className="member-stat-foot">{item.suffix}</span></article>)}
    </section>
    <div className="member-service-state"><ShieldCheck size={16} /><p><strong>用户工作台可开通</strong><span>模型调用{dispatch === null ? '状态以用户钱包为准' : dispatch ? '已开放' : '尚未开放'} · 公共池贡献{pool === null ? '状态以用户钱包为准' : pool ? '已启用' : '尚未开启'}。赠送积分不会绕过调用保护。</span></p>
      <details><summary><CircleHelp size={14} />说明</summary><p>不提供信用或透支。每天签到的积分逐批到期；现有管理员身份和IP策略不变。只有超级管理员 admin 本人可以赠送实际积分。</p></details></div>
    {error && <p className="portal-error" role="alert">{error}{members !== null && ' · 当前显示上次读取的数据，操作前请刷新确认。'}</p>}
    {notice && <p className="member-success" role="status">{notice}</p>}
    <RegistrationInvites />
    <Tabs value={tab} onValueChange={setTab} className="member-tabs">
      <TabsList aria-label="用户管理功能" className="member-tabs-list">
        <TabsTrigger value="users"><Users size={15} />用户管理</TabsTrigger>
        <TabsTrigger value="checkin"><CalendarCheck size={15} />签到管理</TabsTrigger>
        <TabsTrigger value="wallet"><Wallet size={15} />积分账本与异常</TabsTrigger>
      </TabsList>
      <TabsContent value="users">
        <section className="member-directory" aria-labelledby="member-list-title">
          <div className="member-directory-toolbar"><div><h2 id="member-list-title">用户列表 <span>{members === null ? '—' : rows.length}</span></h2><p>开通与维护用户，查看各自的账号和积分。</p></div>
            <div className="member-filters"><label className="member-search"><Search size={15} /><input aria-label="搜索用户名" placeholder="搜索用户名" value={query} onChange={event => setQuery(event.target.value)} /></label>
              <select aria-label="筛选用户状态" value={status} onChange={event => setStatus(event.target.value)}><option value="all">全部状态</option><option value="enabled">已启用</option><option value="disabled">已停用</option></select>
              <button className="portal-button member-refresh" aria-label="刷新用户列表" disabled={!!busy} onClick={() => void load().catch(() => {})}><RefreshCw size={15} /></button></div></div>
          <div className="member-list-columns" aria-hidden="true"><span>用户</span><span>绑定账号</span><span>可用积分</span><span>今日签到</span><span>管理</span></div>
          {members === null ? <p className="portal-empty" role="status">{error ? '暂时无法读取用户，请刷新重试。' : '正在读取用户…'}</p> : !filtered.length ?
            <div className="member-empty"><Users size={28} /><h3>{rows.length ? '没有匹配的用户' : '从第一位用户开始'}</h3><p>{rows.length ? '试试其他用户名，或切换用户状态。' : '开通独立工作台，用户的账号与积分只对本人可见。'}</p>
              {!rows.length && <button className="portal-button primary" onClick={() => setCreating(true)}><Plus size={15} />开通用户</button>}</div>
            : filtered.map(member => <article className="member-row" key={member.id}>
              <div className="member-row-main"><div className="member-identity"><span className="member-avatar" aria-hidden="true">{member.username.slice(0, 2).toUpperCase()}</span><div><h3>{member.username}</h3><span className={`member-status ${member.enabled ? '' : 'disabled'}`}><i />{member.enabled ? '已启用' : '已停用'}</span></div></div>
                <div className="member-value"><span className="member-mobile-label">绑定账号</span><strong>{member.account_count}<small> 个</small></strong></div>
                <div className="member-value"><span className="member-mobile-label">可用积分</span><strong>{points(member.balance_units)}</strong>{member.held_units > 0 && <small>冻结 {points(member.held_units)}</small>}</div>
                <div className="member-value"><span className="member-mobile-label">今日签到</span><strong className="member-positive">{member.today_checkin_units > 0 ? '+' : ''}{points(member.today_checkin_units)}</strong></div>
                <div className="member-row-actions">{canGiveBonus && <button className="member-text-button" disabled={!!busy || !member.enabled} onClick={() => setBonusUser(member)}>赠送积分</button>}
                  <button className="member-text-button" disabled={!!busy} onClick={() => setPasswordUser(member)}>重置密码</button>
                  <button className="member-text-button member-muted-action" disabled={!!busy} onClick={() => void action(member.id, async () => {
                    await portalFetch(`portal-admin/users/${member.id}`, {method: 'PATCH', body: JSON.stringify({enabled: !member.enabled})});
                    setNotice(member.enabled ? '用户已停用，已有用户会话同时失效' : '用户已启用，请重新登录');
                  })}>{member.enabled ? '停用' : '启用'}</button></div></div>
              <details className="member-account-details"><summary><span>{portalTime(member.created_at)} 开通{member.debt_units > 0 && ` · 待核对欠额 ${points(member.debt_units)}`}</span><span>账号明细<ChevronDown size={14} /></span></summary>
                <div className="member-account-list">{!member.accounts.length ? <p className="member-form-note">尚未添加账号。用户登录后可扫码绑定自己的账号。</p> : member.accounts.map(account =>
                  <div className="portal-account" key={account.id}><div><div className="portal-account-name">{account.label}</div><p className="portal-small">{checkinLabel(account.checkin_state)} · {account.initial_state === 'confirmed' ? '初始积分已核实' : '初始积分待核实'}</p></div>
                    <button className="portal-button" disabled={!!busy || !member.enabled || !canCheckin(account)} onClick={() => void action(account.id, async () => {
                      const result = await portalFetch<{message: string}>(`portal-admin/accounts/${account.id}/checkin`, {method: 'POST'}); setNotice(result.message);
                    })}>{busy === account.id ? '处理中…' : canCheckin(account) ? '帮他签到' : '状态已同步'}</button></div>)}</div></details>
            </article>)}
          <footer className="member-directory-footer"><ShieldCheck size={14} />普通用户不具有管理员权限，仅能查看自己的账号、积分和请求记录。</footer>
        </section>
      </TabsContent>
      <TabsContent value="checkin"><section className="portal-panel member-checkin-panel"><div className="member-section-heading"><span className="member-dialog-icon"><CalendarCheck size={22} /></span><div><h2>签到管理</h2><p className="portal-small">总台代签与用户手动签到共享每日执行权，已签到不会重复领取。</p></div></div>
        <div className="member-checkin-state"><span className={`member-status ${automatic ? '' : 'disabled'}`}><i />自动签到{automatic ? '已启用' : '未启用'}</span><p>只处理已核实的用户账号；未确认的结果保留待核对，不自动重试。</p></div>
        <div className="portal-actions"><button className="portal-button" disabled={!!busy || members === null} onClick={() => void action('automatic', async () => {
          await portalFetch('portal-admin/automatic-checkin', {method: 'PUT', body: JSON.stringify({enabled: !automatic})}); setNotice(automatic ? '自动签到已关闭' : '自动签到已启用，仅运行在后台主实例');
        })}>{automatic ? '关闭自动签到' : '开启自动签到'}</button><button className="portal-button primary" disabled={!!busy || members === null} onClick={() => void action('batch', async () => {
          const result = await portalFetch<{message: string}>('portal-admin/checkin-all', {method: 'POST'}); setNotice(result.message);
        })}>{busy === 'batch' ? '正在代签…' : '为用户账号批量签到'}</button></div></section></TabsContent>
      <TabsContent value="wallet">{tab === 'wallet' && <WalletOperations key={walletVersion} onChanged={load} />}</TabsContent>
    </Tabs>
    {creating && <CreateMemberDialog close={() => setCreating(false)} saved={load} />}
    {canGiveBonus && bonusUser && <BonusDialog user={bonusUser} close={() => setBonusUser(null)} saved={async () => { await load(); setWalletVersion(value => value + 1); }} />}
    {passwordUser && <PasswordDialog user={passwordUser} close={() => setPasswordUser(null)} saved={load} />}
  </div>;
}
