'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {ArrowUpRight, CalendarCheck, Check, ChevronDown, Clock3, Coins, History, KeyRound, LayoutDashboard, LogOut, Plus, RefreshCw, ScanLine, ShieldCheck, UserRound, Wallet, WifiOff} from 'lucide-react';
import {PortalError, portalFetch, points, portalTime, canCheckin, checkinLabel} from '@/lib/portal-api';
import {checkinSummary} from '@/lib/portal-display';
import {usePortalWorkspace} from '@/lib/use-portal-workspace';
import './portal.css';
import PortalKeys from './PortalKeys';
import PortalLogin from './PortalLogin';
import PortalRequests from './PortalRequests';
import {BrandHome} from '@/components/common/layout/BrandHome';
import {StatCard} from '@/components/common/layout/StatCard';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';

type Tab = 'overview' | 'accounts' | 'credits' | 'requests' | 'keys';

export default function UserPortal() {
  const workspace = usePortalWorkspace();
  return <PortalView key={workspace.me?.id || (workspace.anonymous ? 'anonymous' : 'connecting')} workspace={workspace} />;
}

function PortalView({workspace}: {workspace: ReturnType<typeof usePortalWorkspace>}) {
  const {me, data, anonymous, pending, error: readError, reload, connect, reset} = workspace;
  const [tab, setTab] = useState<Tab>('overview');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [needsReview, setNeedsReview] = useState(false);
  const [qr, setQr] = useState<{session: string; auth_url: string; expires_in: number} | null>(null);
  const [qrError, setQrError] = useState('');
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
    setQr(null); setQrError(''); qrPaused.current = false;
    if (!current) return;
    try {
      const result = await portalFetch<{state?: string}>('portal/auth/cancel', {method: 'POST', body: JSON.stringify({session: current.session})});
      if (mounted.current && result.state === 'complete') {setNotice('授权已经完成，正在核对账号列表。'); await load();}
    }
    catch (err) {
      if (!mounted.current) return;
      if (err instanceof PortalError && (err.status === 401 || err.status === 403)) reset();
      else {requiresRead.current = true; setNeedsReview(true); setNotice('关闭了本地窗口；授权是否完成仍需刷新账号列表核对。');}
    }
  }, [qr, reset, load]);

  useEffect(() => {
    if (!qr) return;
    let active = true, polling = false;
    const controller = new AbortController();
    const deadline = Date.now() + qr.expires_in * 1000;
    const timer = window.setInterval(async () => {
      if (!active) return;
      if (Date.now() >= deadline) { setNotice('二维码已到期，请重新添加'); setQr(null); return; }
      if (polling || qrPaused.current) return;
      polling = true;
      try {
        const result = await portalFetch<{state: string; message?: string}>('portal/auth/poll', {
          method: 'POST', body: JSON.stringify({session: qr.session}), signal: controller.signal,
        });
        if (!active) return;
        if (result.state === 'complete') { setQr(null); setNotice(result.message || '账号已添加'); await load(); }
        else if (result.state === 'expired') { setQr(null); setNotice('二维码已失效，请重新添加'); }
        else if (result.state === 'failed') {setQr(null); requiresRead.current = true; setNeedsReview(true); setError(result.message || '授权结果待核对，请刷新账号列表。');}
        else if (result.message) setNotice(result.message);
      } catch (err) {
        if (active) {
          if (err instanceof PortalError && (err.status === 401 || err.status === 403)) reset();
          else {qrPaused.current = true; setQrError('扫码连接中断，已暂停轮询。可以重试当前二维码；若关闭，请先核对账号列表，不要重复扫码。');}
        }
      } finally { polling = false; }
    }, 2500);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [qr, load, reset]);

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
          requiresRead.current = true; setNeedsReview(true); setError('操作结果待确认，请先重新读取核对，不要重复提交；不会自动重试。');
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
      if (url.protocol !== 'https:') throw new Error('授权链接异常，请联系管理员');
      if (mounted.current) {qrPaused.current = false; setQrError(''); setQr(result);}
    });
  }
  async function checkin(id: string) {
    await action(id, async () => {
      const result = await portalFetch<{message: string}>(`portal/accounts/${id}/checkin`, {method: 'POST'});
      if (!mounted.current) return;
      setNotice(result.message); await load();
    });
  }

  return <div className="user-portal"><a className="skip-link" href="#portal-main">跳转到工作台内容</a><div className="portal-wrap">
    <header className="portal-head"><div className="portal-brand-group"><BrandHome href="/portal" /><span className="portal-workspace-label">用户工作台</span></div>
      {me ? <div className="portal-actions"><span className="portal-session"><UserRound size={14} />{me.username}</span><button className="portal-button portal-logout" disabled={!!busy} onClick={() => void action('logout', async () => {
        await portalFetch('portal/logout', {method: 'POST'});
        if (mounted.current) reset();
      })}><LogOut size={14} />退出登录</button></div> : <span className="portal-private-label"><ShieldCheck size={14} />独立账号 · 仅自己可见</span>}</header>
    {(error || readError) && <div className="portal-error" role="alert">{error || readError}{data && readError && ' · 当前显示上次读取的数据。'}<button className="portal-button" disabled={pending} onClick={() => void load()}>重新读取</button></div>}
    {notice && <div className="portal-notice portal-feedback" role="status">{notice}</div>}
    <main id="portal-main" tabIndex={-1}>
    {!data && pending && !anonymous ? <div className="portal-loading" role="status" aria-busy="true"><span>正在读取你的工作台…</span><div className="portal-loading-cards" aria-hidden="true">{[0,1,2,3].map(index => <i key={index} />)}</div></div>
      : anonymous ? <PortalLogin login={login} /> : me && data ? <>
      <section className="portal-hero"><div><p className="portal-eyebrow">Personal workspace</p><h1 className="portal-title">你好，{me.username}。</h1><p className="portal-muted">账号、签到、积分，在这里一目了然。总台代签的结果也会同步显示。</p></div>
        <div className="portal-actions"><button className="portal-button" onClick={() => void load()} disabled={!!busy || pending}><RefreshCw size={15} className={pending ? 'portal-spinning' : ''} />{pending ? '正在同步' : '刷新'}</button><button className="portal-button" disabled={actionBlocked || !data.accounts.some(canCheckin)} onClick={() => void action('batch', async () => {
          const result = await portalFetch<{message: string}>('portal/checkin-all', {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load();
        })}><CalendarCheck size={15} />{busy === 'batch' ? '正在签到…' : summary?.ready ? `一键签到 · ${summary.ready}` : summary?.pending ? '签到待核对' : summary?.total ? '今日已签到' : '暂无可签到账号'}</button><button className="portal-button primary" onClick={() => void add()} disabled={actionBlocked}><Plus size={16} />扫码添加账号</button></div></section>
      <section className="portal-stats" aria-label="我的账户概览">
        <StatCard label="可用积分" value={points(data.balance_units, data.scale)} hint="保留每批积分的原到期时间" icon={Coins} tone="info" />
        <StatCard label="今日签到到账" value={points(data.today_checkin_units, data.scale)} hint="只计入已核实的签到积分" icon={CalendarCheck} />
        <StatCard label="我的账号" value={data.account_count} hint={`${data.active_account_count} 个初始积分已核实`} icon={UserRound} />
        <StatCard label="7天内将过期" value={points(data.expiring_7d_units, data.scale)} hint="未使用的部分到期后失效" icon={Clock3} />
      </section>
      <Tabs value={tab} onValueChange={value => setTab(value as Tab)} className="portal-workspace-tabs">
      <TabsList aria-label="用户工作台页面" className="portal-tabs">{([{key: 'overview', label: '总览', icon: LayoutDashboard}, {key: 'accounts', label: '我的账号', icon: ScanLine}, {key: 'credits', label: '积分明细', icon: Wallet}, {key: 'keys', label: '调用密钥', icon: KeyRound}, {key: 'requests', label: '请求记录', icon: History}] as const).map(item => <TabsTrigger key={item.key} value={item.key}><item.icon size={14} />{item.label}</TabsTrigger>)}</TabsList>
      <TabsContent value={tab}>
      <div className="portal-service-summary"><ShieldCheck size={16} /><div><strong>{data.dispatch_enabled ? 'DeepSeek 调用已开放' : '模型调用暂未开放'}</strong><p>{data.dispatch_enabled ? '使用自己的密钥调用，费用计入你的积分钱包。' : '账号、签到和积分可以正常管理；调用开放后再使用密钥。'}</p>
        <details className="portal-service-details"><summary>调用与积分说明<ChevronDown size={13} /></summary><p>{data.pool_enabled ? '账号贡献公共池，调用统一调度，按调用者的密钥归属单独扣分。' : '公共池贡献暂未开启，账号与积分仍可在这里管理。'} {!data.dispatch_enabled && data.dispatch_note}</p><p>余额为零拒绝新请求，不提供信用或透支。单次超出冻结余额的部分由平台承担，不扣未来签到积分；费用未知仍须核对。</p></details>
      </div><span>冻结 / 待核对 <b>{points(data.held_units || 0, data.scale)}</b>{data.debt_units > 0 ? ` · 超额待处理 ${points(data.debt_units, data.scale)}` : ''}</span></div>
      {data.pending_pool_units > 0 && <p className="portal-small">待加入公共池的账号积分 {points(data.pending_pool_units, data.scale)}；这部分已记录，但暂不能用于调用。当前已入池可调用积分 {points(data.callable_units, data.scale)}。</p>}
      {data.pool_enabled && data.callable_units <= 0 && !data.held_units && !data.debt_units && <div className="portal-notice" role="status">额度已用尽，请联系管理员赠送额度。不支持信用或透支。</div>}
      {tab === 'keys' && <PortalKeys key={me.id} onUnauthorized={reset} />}
      {(tab === 'overview' || tab === 'accounts') && <div className={tab === 'overview' ? 'portal-grid' : ''}><section className="portal-panel"><div className="portal-panel-heading"><div><h2>我的账号 <span className="portal-count">{data.account_count}</span></h2><p className="portal-small">WorkBuddy 国内个人账号 · 总台代签结果同步显示</p></div></div>
        {!!summary?.total && <div className="portal-checkin-summary"><CalendarCheck size={15} /><span>今日签到 <strong>{summary.confirmed} / {summary.total}</strong></span><span>{summary.pending ? `${summary.pending} 个待核对` : summary.ready ? `${summary.ready} 个待签到` : '全部已核实到账'}</span></div>}
        {!data.accounts.length ? <div className="portal-empty"><ScanLine size={30} style={{margin: '0 auto 14px', color: '#8fa9c5'}} />还没有账号。扫码添加后，先核实余额与到期明细。<br /><button className="portal-button" onClick={() => void add()} disabled={actionBlocked}>添加第一个账号</button></div> : data.accounts.map(account => <article key={account.id} className="portal-account"><div><div className="portal-account-name">{account.label}</div><span className={`portal-badge ${account.checkin_state === 'confirmed' ? '' : 'pending'}`}>{account.checkin_state === 'confirmed' ? <Check size={12} /> : <Clock3 size={12} />}{checkinLabel(account.checkin_state)}</span><p className="portal-small">{portalTime(account.created_at)} 加入 · {account.initial_state === 'confirmed' ? '初始积分已核实' : '初始积分待核实'} · {account.pool_state === 'public' ? '已贡献公共池' : '待加入公共池'}</p></div>
          <div className="portal-actions">{account.initial_state === 'confirmed' && account.pool_state !== 'public' && <button className="portal-button" disabled={actionBlocked || !data.pool_enabled} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/join-public-pool`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>加入公共池</button>}{account.checkin_state === 'confirmed_uncredited' && <button className="portal-button" disabled={actionBlocked} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/reconcile-checkin`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>核对到账</button>}{account.initial_state !== 'confirmed' ? <button className="portal-button" disabled={actionBlocked} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/verify-initial`, {method: 'POST'}); if (!mounted.current) return; setNotice(result.message); await load(); })}>核实初始积分</button> : <button className="portal-button" disabled={actionBlocked || !canCheckin(account)} onClick={() => void checkin(account.id)}>{busy === account.id ? '处理中…' : canCheckin(account) ? '今日签到' : '状态已同步'}</button>}</div></article>)}</section>
        {tab === 'overview' && <section className="portal-panel"><h2>即将到期</h2><p className="portal-small">每笔积分单独到期，不清空后来领取的积分。</p>{data.grants.length ? data.grants.slice(0, 6).map((grant, index) => <div key={`${grant.expires_at}-${index}`} className="portal-account"><div><div className="portal-account-name">{grant.source === 'initial' ? '初始积分' : grant.source === 'admin_bonus' ? '管理员赠送' : '每日签到'}</div><p className="portal-small">{portalTime(grant.expires_at)} 到期</p></div><strong>{points(grant.remaining_units, data.scale)}</strong></div>) : <p className="portal-empty">尚无已核实的积分批次</p>}</section>}</div>}
      {tab === 'credits' && <section className="portal-panel"><h2>积分流水</h2><p className="portal-small">到账、过期均可追溯。积分不会因为重新扫码而延长有效期。</p><div className="portal-table-wrap"><table className="portal-table"><thead><tr><th>时间</th><th>类型</th><th>积分变动</th><th>原到期时间</th></tr></thead><tbody>{data.events.map((event, index) => <tr key={`${event.ts}-${index}`}><td>{portalTime(event.ts)}</td><td>{{admin_bonus: '管理员赠送积分', initial: '初始余额入账', checkin: '每日签到', expired: '积分过期', usage: '模型调用消费', debt_repaid: '现有积分抵欠额', debt_waived: '管理员豁免欠额（不加分）'}[event.kind] || event.kind}</td><td>{event.amount_units > 0 ? '+' : ''}{points(event.amount_units, data.scale)}</td><td>{event.expires_at ? portalTime(event.expires_at) : '—'}</td></tr>)}</tbody></table></div>{!data.events.length && <p className="portal-empty">还没有积分流水，不将待核实余额显示为已到账。</p>}</section>}
      {tab === 'requests' && <PortalRequests key={me.id} onUnauthorized={reset} />}
      </TabsContent></Tabs>
      <footer className="portal-data-footer"><span><ShieldCheck size={13} />仅显示你的账号、积分和请求记录</span><span>更新于 {portalTime(data.snapshot_at)} · 北京时间</span></footer>
    </> : <section className="portal-connection" aria-labelledby="portal-connection-title"><WifiOff size={30} /><h1 id="portal-connection-title">暂时无法读取工作台</h1><p>没有将读取失败当作退出登录，也没有更改你的账号和积分。</p><button className="portal-button primary" disabled={pending} onClick={() => void load()}>重新连接</button></section>}
    </main>
    {qr && <div className="portal-overlay"><section id="portal-qr-dialog" className="portal-dialog" role="dialog" aria-modal="true" aria-labelledby="portal-qr-title"><p className="portal-eyebrow">Connect an account</p><h2 id="portal-qr-title" style={{fontSize: 21, fontWeight: 650}}>扫码添加你的账号</h2><p className="portal-small">只使用平台官方授权，不接收账号密码。</p><div className="portal-qr"><QRCodeSVG value={qr.auth_url} size={196} /></div><p className="portal-small">授权完成后自动同步。初始积分核实前不会赠送每日积分。</p>{qrError && <div className="portal-error" role="alert">{qrError}<button className="portal-button" onClick={() => {qrPaused.current = false; setQrError('');}}>重试当前二维码</button></div>}<div className="portal-actions" style={{justifyContent: 'center', marginTop: 20}}><a className="portal-button primary" href={qr.auth_url} target="_blank" rel="noopener noreferrer">打开官方授权<ArrowUpRight size={14} /></a><button className="portal-button" onClick={() => void closeQr()}>关闭</button></div></section></div>}
  </div></div>;
}
