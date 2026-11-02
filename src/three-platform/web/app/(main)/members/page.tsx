'use client';

import {useCallback, useEffect, useState} from 'react';
import Link from 'next/link';
import {ArrowUpRight, Plus, RefreshCw, Users} from 'lucide-react';
import {useAuth} from '@/lib/auth-context';
import {portalFetch, points, portalTime, canCheckin, checkinLabel, type PortalMember} from '@/lib/portal-api';
import '../../portal/portal.css';
import WalletOperations from './WalletOperations';
import BonusDialog from './BonusDialog';
import PasswordDialog from './PasswordDialog';

export default function PortalMembersPage() {
  const {isAdmin, me} = useAuth();
  const canGiveBonus = isAdmin && me?.username === 'admin';
  const [bonusUser, setBonusUser] = useState<{id: string; username: string} | null>(null);
  const [passwordUser, setPasswordUser] = useState<{id: string; username: string} | null>(null);
  const [walletVersion, setWalletVersion] = useState(0);
  const [members, setMembers] = useState<PortalMember[] | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [automatic, setAutomatic] = useState(false);
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!isAdmin) return;
    try {
      const result = await portalFetch<{users: PortalMember[]; automatic_checkin: boolean}>('portal-admin/users', {signal});
      if (!signal?.aborted) { setMembers(result.users); setAutomatic(result.automatic_checkin); setError(''); }
    } catch (err) { if (!signal?.aborted) setError(err instanceof Error ? err.message : '读取失败'); }
  }, [isAdmin]);
  useEffect(() => {
    const controller = new AbortController();
    let inFlight = false;
    const refresh = async () => {
      if (inFlight || document.hidden) return;
      inFlight = true;
      try { await load(controller.signal); } finally { inFlight = false; }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 20000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [load]);
  async function action(key: string, run: () => Promise<void>) {
    if (busy) return;
    setBusy(key); setError(''); setNotice('');
    try { await run(); await load(); } catch (err) { setError(err instanceof Error ? err.message : '操作未完成'); }
    finally { setBusy(''); }
  }
  if (!isAdmin) return <section className="portal-panel"><h1>用户工作台管理</h1><p className="portal-muted">该管理页仅超级管理员本人会话可用；原有管理员权限保持不变。</p></section>;
  return <div className="portal-management">
    <div className="portal-hero" style={{paddingTop: 10}}><div><p className="portal-eyebrow">Customer operations</p><h1 className="portal-title">用户工作台管理</h1><p className="portal-muted">为用户开通账号，代签到与用户端共享同一份状态及积分流水。</p></div><div className="portal-actions"><button className="portal-button" disabled={!!busy} onClick={() => void load()}><RefreshCw size={15} />刷新</button><Link className="portal-button" href="/portal" target="_blank">用户端入口<ArrowUpRight size={15} /></Link></div></div>
    <div className="portal-notice">本地候选 · 用户贡献账号进入公共池统一调度，仅允许 DeepSeek；消费归属用户钱包。原有管理员身份与IP策略不变，未切换线上。</div>
    <WalletOperations key={walletVersion} onChanged={load} />
    {canGiveBonus && <p className="portal-small">不提供信用或透支。用户额度用尽后提示联系管理员；可在用户卡片中赠送额外积分。此权限仅超级管理员 admin 所有。</p>}
    <section className="portal-panel"><h2>用户账号签到</h2><p className="portal-small">自动签到默认关闭。启用后仅处理用户端已核实账号；与用户手动操作共用每日执行权。</p><div className="portal-actions" style={{marginTop: 14}}><button className="portal-button" disabled={!!busy} onClick={() => void action('automatic', async () => {
      await portalFetch('portal-admin/automatic-checkin', {method: 'PUT', body: JSON.stringify({enabled: !automatic})}); setNotice(automatic ? '自动签到已关闭' : '自动签到已启用，仅运行在后台主实例');
    })}>{automatic ? '关闭自动签到' : '开启自动签到'}</button><button className="portal-button primary" disabled={!!busy} onClick={() => void action('batch', async () => {
      const result = await portalFetch<{message: string}>('portal-admin/checkin-all', {method: 'POST'}); setNotice(result.message);
    })}>{busy === 'batch' ? '正在代签…' : '为用户账号批量签到'}</button><span className="portal-small">当前：{automatic ? '已启用' : '未启用'}</span></div></section>
    {error && <p className="portal-error" role="alert">{error}</p>}{notice && <p className="portal-notice" role="status">{notice}</p>}
    <section className="portal-panel"><h2>开通普通用户</h2><p className="portal-small">普通用户只能登录用户端，不具有管理员或全池读取权限。请通过可信渠道交付初始密码。</p><form className="portal-form-row" onSubmit={event => {
      event.preventDefault(); void action('create', async () => {
        await portalFetch('portal-admin/users', {method: 'POST', body: JSON.stringify({username, password})});
        setPassword(''); setUsername(''); setNotice('用户已开通，可从用户端入口登录');
      });
    }}><label className="portal-field">用户名<input className="portal-input" required minLength={3} maxLength={40} pattern="[a-zA-Z0-9_-]+" value={username} onChange={event => setUsername(event.target.value)} autoComplete="off" /></label>
      <label className="portal-field">初始密码<input className="portal-input" type="password" required minLength={8} maxLength={128} value={password} onChange={event => setPassword(event.target.value)} autoComplete="new-password" /></label>
      <button className="portal-button primary" style={{marginBottom: 20}} disabled={!!busy}><Plus size={15} />{busy === 'create' ? '正在开通…' : '开通用户'}</button></form></section>
    {members === null ? <p className="portal-empty">正在读取用户列表…</p> : !members.length ? <section className="portal-panel portal-empty"><Users size={28} style={{margin: '0 auto 12px'}} />尚未开通普通用户。</section> : members.map(member => <section className="portal-panel" key={member.id}>
      <div className="portal-account" style={{borderTop: 0, paddingTop: 0}}><div><h2>{member.username}<span className={`portal-badge ${member.enabled ? '' : 'pending'}`} style={{marginLeft: 10}}>{member.enabled ? '已启用' : '已停用'}</span></h2><p className="portal-small">{member.account_count} 个账号 · 可用 {points(member.balance_units)} 积分 · 冻结 {points(member.held_units)} · 欠额 {points(member.debt_units)} · 今日到账 {points(member.today_checkin_units)} · {portalTime(member.created_at)} 开通</p></div>
        <div className="portal-actions">{canGiveBonus && <button className="portal-button primary" disabled={!!busy || !member.enabled} onClick={() => setBonusUser({id: member.id, username: member.username})}>赠送积分</button>}
        <button className="portal-button" disabled={!!busy} onClick={() => setPasswordUser({id: member.id, username: member.username})}>重置密码</button>
        <button className="portal-button" disabled={!!busy} onClick={() => void action(member.id, async () => {
          await portalFetch(`portal-admin/users/${member.id}`, {method: 'PATCH', body: JSON.stringify({enabled: !member.enabled})});
          setNotice(member.enabled ? '用户已停用，已有用户会话同时失效' : '用户已启用，请重新登录');
        })}>{member.enabled ? '停用用户' : '启用用户'}</button></div></div>
      {!member.accounts.length ? <p className="portal-small">该用户尚未扫码添加账号。</p> : member.accounts.map(account => <div className="portal-account" key={account.id}><div><div className="portal-account-name">{account.label}</div><p className="portal-small">{checkinLabel(account.checkin_state)} · {account.initial_state === 'confirmed' ? '初始积分已核实' : '初始积分待核实'}</p></div><button className="portal-button" disabled={!!busy || !member.enabled || !canCheckin(account)} onClick={() => void action(account.id, async () => {
        const result = await portalFetch<{message: string}>(`portal-admin/accounts/${account.id}/checkin`, {method: 'POST'}); setNotice(result.message);
      })}>{busy === account.id ? '处理中…' : canCheckin(account) ? '帮他签到' : '状态已同步'}</button></div>)}
    </section>)}
    {canGiveBonus && bonusUser && <BonusDialog user={bonusUser} close={() => setBonusUser(null)} saved={async () => { await load(); setWalletVersion(value => value + 1); }} />}
    {passwordUser && <PasswordDialog user={passwordUser} close={() => setPasswordUser(null)} saved={load} />}
  </div>;
}
