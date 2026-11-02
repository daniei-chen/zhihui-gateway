'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {ArrowUpRight, Check, Clock3, LogOut, Plus, RefreshCw, ScanLine} from 'lucide-react';
import {portalFetch, PortalError, points, portalTime, canCheckin, checkinLabel,
  type PortalOverview, type PortalRequest} from '@/lib/portal-api';
import './portal.css';
import PortalKeys from './PortalKeys';

type Identity = {username: string; id: string};
type Tab = 'overview' | 'accounts' | 'credits' | 'requests' | 'keys';

export default function UserPortal() {
  const [me, setMe] = useState<Identity | null>(null);
  const [loading, setLoading] = useState(true);
  const [data, setData] = useState<PortalOverview | null>(null);
  const [requests, setRequests] = useState<{items: PortalRequest[]; total: number; page: number}>({items: [], total: 0, page: 1});
  const [requestPage, setRequestPage] = useState(1);
  const [requestsLoading, setRequestsLoading] = useState(false);
  const [tab, setTab] = useState<Tab>('overview');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [qr, setQr] = useState<{session: string; auth_url: string; expires_in: number} | null>(null);
  const epoch = useRef(0);
  const refreshFlight = useRef<symbol | null>(null);
  const closeQr = useCallback(async () => {
    const current = qr;
    setQr(null);
    if (!current) return;
    try { await portalFetch('portal/auth/cancel', {method: 'POST', body: JSON.stringify({session: current.session})}); }
    catch (err) { setNotice(err instanceof Error ? err.message : '授权结果仍需核对'); }
  }, [qr]);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (refreshFlight.current || signal?.aborted) return;
    const flight = Symbol();
    refreshFlight.current = flight;
    setRequestsLoading(true);
    const generation = epoch.current;
    try {
      const identity = await portalFetch<Identity>('portal/me', {signal});
      const [overview, logs] = await Promise.all([
        portalFetch<PortalOverview>('portal/overview', {signal}),
        portalFetch<typeof requests>(`portal/requests?page=${requestPage}`, {signal}),
      ]);
      if (generation !== epoch.current || signal?.aborted) return;
      setMe(identity); setData(overview); setRequests(logs); setError('');
    } catch (err) {
      if (generation !== epoch.current || signal?.aborted) return;
      if (err instanceof PortalError && err.status === 401) { setMe(null); setData(null); setQr(null); }
      else setError(err instanceof Error ? err.message : '读取失败，请重试');
    } finally {
      if (refreshFlight.current === flight) refreshFlight.current = null;
      if (generation === epoch.current && !signal?.aborted) { setLoading(false); setRequestsLoading(false); }
    }
  }, [requestPage]);

  useEffect(() => {
    const controller = new AbortController();
    void Promise.resolve().then(() => load(controller.signal));
    const interval = window.setInterval(() => { if (!document.hidden) void load(controller.signal); }, 20000);
    return () => { controller.abort(); refreshFlight.current = null; window.clearInterval(interval); };
  }, [load]);

  useEffect(() => {
    if (!qr) return;
    let active = true, polling = false;
    const controller = new AbortController();
    const deadline = Date.now() + qr.expires_in * 1000;
    const timer = window.setInterval(async () => {
      if (polling || !active) return;
      if (Date.now() >= deadline) { setNotice('二维码已到期，请重新添加'); setQr(null); return; }
      polling = true;
      try {
        const result = await portalFetch<{state: string; message?: string}>('portal/auth/poll', {
          method: 'POST', body: JSON.stringify({session: qr.session}), signal: controller.signal,
        });
        if (!active) return;
        if (result.state === 'complete') { setQr(null); setNotice(result.message || '账号已添加'); await load(); }
        else if (result.state === 'expired') { setQr(null); setNotice('二维码已失效，请重新添加'); }
        else if (result.message) setNotice(result.message);
      } catch (err) {
        if (active) { setError(err instanceof Error ? err.message : '扫码结果待确认'); setQr(null); }
      } finally { polling = false; }
    }, 2500);
    return () => { active = false; controller.abort(); window.clearInterval(timer); };
  }, [qr, load]);

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
    if (busy) return;
    setBusy(key); setError(''); setNotice('');
    try { await run(); } catch (err) { setError(err instanceof Error ? err.message : '操作未完成'); }
    finally { setBusy(''); }
  }
  async function login(event: React.FormEvent) {
    event.preventDefault();
    await action('login', async () => {
      await portalFetch('portal/login', {method: 'POST', body: JSON.stringify({username, password})});
      setPassword(''); await load();
    });
  }
  async function add() {
    await action('add', async () => {
      const result = await portalFetch<NonNullable<typeof qr>>('portal/auth/start', {method: 'POST'});
      const url = new URL(result.auth_url);
      if (url.protocol !== 'https:') throw new Error('授权链接异常，请联系管理员');
      setQr(result);
    });
  }
  async function checkin(id: string) {
    await action(id, async () => {
      const result = await portalFetch<{message: string}>(`portal/accounts/${id}/checkin`, {method: 'POST'});
      setNotice(result.message); await load();
    });
  }

  return <div className="user-portal"><div className="portal-wrap">
    <header className="portal-head"><div className="portal-brand"><span className="portal-mark">Z</span><div>智汇 <span className="portal-small">用户工作台</span><p className="portal-eyebrow" style={{margin: '4px 0 0'}}>Your accounts. Your credits.</p></div></div>
      {me ? <div className="portal-actions"><span className="portal-small">{me.username}</span><button className="portal-button" disabled={!!busy} onClick={() => void action('logout', async () => {
        await portalFetch('portal/logout', {method: 'POST'});
        epoch.current++; setMe(null); setData(null); setQr(null); setRequestPage(1); setRequests({items: [], total: 0, page: 1});
      })}><LogOut size={14} />退出</button></div> : <span className="portal-small">账号与积分，仅自己可见</span>}</header>
    {error && <div className="portal-error" role="alert">{error}<button className="portal-button" onClick={() => void load()}>重新读取</button></div>}
    {notice && <div className="portal-notice" role="status">{notice}</div>}
    {loading ? <p className="portal-empty" role="status">正在读取你的工作台…</p> : !me ? <section className="portal-login">
      <p className="portal-eyebrow">Welcome back</p><h1 className="portal-title">你的账号，<br />每一分都有记录。</h1>
      <p className="portal-muted">登录后添加账号、查看签到与积分。每日领取的积分独立计算30天有效期。</p>
      <form onSubmit={login}><label className="portal-field">用户名<input className="portal-input" autoComplete="username" required maxLength={40} value={username} onChange={event => setUsername(event.target.value)} /></label>
        <label className="portal-field">密码<input className="portal-input" autoComplete="current-password" type="password" required maxLength={128} value={password} onChange={event => setPassword(event.target.value)} /></label>
        <button className="portal-button primary" style={{width: '100%'}} disabled={!!busy}>{busy === 'login' ? '正在登录…' : '进入我的工作台'}<ArrowUpRight size={16} /></button></form>
      <p className="portal-small" style={{marginTop: 18}}>首版由管理员开通用户账号，不开放公开注册。</p>
    </section> : data ? <main id="main-content">
      <section className="portal-hero"><div><p className="portal-eyebrow">Personal workspace</p><h1 className="portal-title">你好，{me.username}。</h1><p className="portal-muted">账号、签到、积分，在这里一目了然。总台代签的结果也会同步显示。</p></div>
        <div className="portal-actions"><button className="portal-button" onClick={() => void load()} disabled={!!busy}><RefreshCw size={15} />刷新</button><button className="portal-button" disabled={!!busy || !data.accounts.some(canCheckin)} onClick={() => void action('batch', async () => {
          const result = await portalFetch<{message: string}>('portal/checkin-all', {method: 'POST'}); setNotice(result.message); await load();
        })}>签到未完成账号</button><button className="portal-button primary" onClick={() => void add()} disabled={!!busy}><Plus size={16} />扫码添加账号</button></div></section>
      <section className="portal-stats" aria-label="我的账户概览">
        {[['可用积分', points(data.balance_units, data.scale), '按到账批次保留到期时间'], ['今日签到到账', points(data.today_checkin_units, data.scale), '仅计已经核实的积分'], ['我的账号', String(data.account_count), `${data.active_account_count} 个初始积分已核实`], ['7天内将过期', points(data.expiring_7d_units, data.scale), '未使用的部分到期后失效']].map(([label, value, hint]) => <div className="portal-stat" key={label}><span className="portal-small">{label}</span><strong className="portal-number">{value}</strong><span className="portal-small">{hint}</span></div>)}
      </section>
      <nav className="portal-tabs" aria-label="用户工作台页面">{([['overview', '总览'], ['accounts', '我的账号'], ['credits', '积分明细'], ['keys', '调用密钥'], ['requests', '请求记录']] as const).map(([key, label]) => <button key={key} className="portal-tab" aria-pressed={tab === key} onClick={() => setTab(key)}>{label}</button>)}</nav>
      <div className="portal-notice">公共池统一调度 · 仅限 DeepSeek · 冻结/待核对 {points(data.held_units || 0, data.scale)} 积分{data.debt_units > 0 ? ` · 超额待处理 ${points(data.debt_units, data.scale)} 积分` : ''}</div>
      {data.pending_pool_units > 0 && <p className="portal-small">待加入公共池的账号积分 {points(data.pending_pool_units, data.scale)}；这部分已记录，但暂不能用于调用。当前已入池可调用积分 {points(data.callable_units, data.scale)}。</p>}
      {data.pool_enabled && data.callable_units <= 0 && !data.held_units && !data.debt_units && <div className="portal-notice" role="status">额度已用尽，请联系管理员赠送额度。不支持信用或透支。</div>}
      {tab === 'keys' && <PortalKeys key={me.id} />}
      {!data.dispatch_enabled && <div className="portal-notice">本地候选 · {data.dispatch_note} 当前积分不会被当成已开放的调用额度。</div>}
      {(tab === 'overview' || tab === 'accounts') && <div className={tab === 'overview' ? 'portal-grid' : ''}><section className="portal-panel"><h2>我的账号</h2><p className="portal-small">WorkBuddy 国内个人账号 · 归属绑定后不重复领取初始积分</p>
        {!data.accounts.length ? <div className="portal-empty"><ScanLine size={30} style={{margin: '0 auto 14px', color: '#8fa9c5'}} />还没有账号。扫码添加后，先核实余额与到期明细。<br /><button className="portal-button" onClick={() => void add()} disabled={!!busy}>添加第一个账号</button></div> : data.accounts.map(account => <article key={account.id} className="portal-account"><div><div className="portal-account-name">{account.label}</div><span className={`portal-badge ${account.checkin_state === 'confirmed' ? '' : 'pending'}`}>{account.checkin_state === 'confirmed' ? <Check size={12} /> : <Clock3 size={12} />}{checkinLabel(account.checkin_state)}</span><p className="portal-small">{portalTime(account.created_at)} 加入 · {account.initial_state === 'confirmed' ? '初始积分已核实' : '初始积分待核实'} · {account.pool_state === 'public' ? '已贡献公共池' : '待加入公共池'}</p></div>
          <div className="portal-actions">{account.initial_state === 'confirmed' && account.pool_state !== 'public' && <button className="portal-button" disabled={!!busy || !data.pool_enabled} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/join-public-pool`, {method: 'POST'}); setNotice(result.message); await load(); })}>加入公共池</button>}{account.checkin_state === 'confirmed_uncredited' && <button className="portal-button" disabled={!!busy} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/reconcile-checkin`, {method: 'POST'}); setNotice(result.message); await load(); })}>核对到账</button>}{account.initial_state !== 'confirmed' ? <button className="portal-button" disabled={!!busy} onClick={() => void action(account.id, async () => { const result = await portalFetch<{message: string}>(`portal/accounts/${account.id}/verify-initial`, {method: 'POST'}); setNotice(result.message); await load(); })}>核实初始积分</button> : <button className="portal-button" disabled={!!busy || !canCheckin(account)} onClick={() => void checkin(account.id)}>{busy === account.id ? '处理中…' : canCheckin(account) ? '今日签到' : '状态已同步'}</button>}</div></article>)}</section>
        {tab === 'overview' && <section className="portal-panel"><h2>即将到期</h2><p className="portal-small">每笔积分单独到期，不清空后来领取的积分。</p>{data.grants.length ? data.grants.slice(0, 6).map((grant, index) => <div key={`${grant.expires_at}-${index}`} className="portal-account"><div><div className="portal-account-name">{grant.source === 'initial' ? '初始积分' : grant.source === 'admin_bonus' ? '管理员赠送' : '每日签到'}</div><p className="portal-small">{portalTime(grant.expires_at)} 到期</p></div><strong>{points(grant.remaining_units, data.scale)}</strong></div>) : <p className="portal-empty">尚无已核实的积分批次</p>}</section>}</div>}
      {tab === 'credits' && <section className="portal-panel"><h2>积分流水</h2><p className="portal-small">到账、过期均可追溯。积分不会因为重新扫码而延长有效期。</p><div className="portal-table-wrap"><table className="portal-table"><thead><tr><th>时间</th><th>类型</th><th>积分变动</th><th>原到期时间</th></tr></thead><tbody>{data.events.map((event, index) => <tr key={`${event.ts}-${index}`}><td>{portalTime(event.ts)}</td><td>{{admin_bonus: '管理员赠送积分', initial: '初始余额入账', checkin: '每日签到', expired: '积分过期', usage: '模型调用消费', debt_repaid: '现有积分抵欠额', debt_waived: '管理员豁免欠额（不加分）'}[event.kind] || event.kind}</td><td>{event.amount_units > 0 ? '+' : ''}{points(event.amount_units, data.scale)}</td><td>{event.expires_at ? portalTime(event.expires_at) : '—'}</td></tr>)}</tbody></table></div>{!data.events.length && <p className="portal-empty">还没有积分流水，不将待核实余额显示为已到账。</p>}</section>}
      {tab === 'requests' && <section className="portal-panel"><h2>我的请求记录</h2><p className="portal-small">仅展示明确归属到你的调用记录，不展示其他用户和总池的请求。</p><div className="portal-table-wrap"><table className="portal-table"><thead><tr><th>时间</th><th>模型</th><th>状态</th><th>Token</th><th>耗时</th><th>积分</th></tr></thead><tbody>{requests.items.map((row, index) => <tr key={`${row.ts}-${index}`}><td>{portalTime(row.ts)}</td><td>{row.model}</td><td>{row.status}</td><td>{row.prompt_tokens + row.completion_tokens}</td><td>{row.latency_ms}ms</td><td>{row.credit === null ? '待核实' : row.credit}</td></tr>)}</tbody></table></div>{!requests.items.length && <p className="portal-empty">暂无你的请求记录。使用自己的密钥调用后，这里会显示实际记录。</p>}<nav className="portal-actions portal-ops-pagination" aria-label="请求记录分页"><span className="portal-small">共 {requests.total} 条 · 第 {requests.page} 页{requestsLoading ? ' · 正在读取…' : ''}</span><button className="portal-button" disabled={requestsLoading || !!busy || requestPage <= 1} onClick={() => setRequestPage(page => page - 1)}>上一页</button><button className="portal-button" disabled={requestsLoading || !!busy || requestPage >= 10000 || requests.page !== requestPage || requestPage * 30 >= requests.total} onClick={() => setRequestPage(page => page + 1)}>下一页</button></nav></section>}
      <p className="portal-small" style={{marginTop: 22}}>数据更新于 {portalTime(data.snapshot_at)}（北京时间） · 仅显示你的账号与积分</p>
    </main> : <p className="portal-empty">数据暂不可用，请重新读取。</p>}
    {qr && <div className="portal-overlay"><section id="portal-qr-dialog" className="portal-dialog" role="dialog" aria-modal="true" aria-labelledby="portal-qr-title"><p className="portal-eyebrow">Connect an account</p><h2 id="portal-qr-title" style={{fontSize: 21, fontWeight: 650}}>扫码添加你的账号</h2><p className="portal-small">只使用平台官方授权，不接收账号密码。</p><div className="portal-qr"><QRCodeSVG value={qr.auth_url} size={196} /></div><p className="portal-small">授权完成后自动同步。初始积分核实前不会赠送每日积分。</p><div className="portal-actions" style={{justifyContent: 'center', marginTop: 20}}><a className="portal-button primary" href={qr.auth_url} target="_blank" rel="noopener noreferrer">打开官方授权<ArrowUpRight size={14} /></a><button className="portal-button" onClick={() => void closeQr()}>关闭</button></div></section></div>}
  </div></div>;
}
