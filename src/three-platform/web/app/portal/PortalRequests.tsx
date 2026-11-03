'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {ChevronLeft, ChevronRight, History, RefreshCw} from 'lucide-react';
import {PortalError, portalFetch, portalTime, type PortalRequest} from '@/lib/portal-api';
import {requestCredit, requestLatency, requestStatus, requestTokens} from '@/lib/portal-display';
import {useHeartbeat} from '@/lib/use-heartbeat';

type Page = {items: PortalRequest[]; total: number; page: number};

export default function PortalRequests({onUnauthorized}: {onUnauthorized: () => void}) {
  const [page, setPage] = useState(1);
  const [data, setData] = useState<Page | null>(null);
  const [pending, setPending] = useState(true);
  const [error, setError] = useState('');
  const sequence = useRef(0);
  const active = useRef<{controller: AbortController; promise: Promise<void>} | null>(null);
  const invalidate = useCallback(() => {
    sequence.current++; active.current?.controller.abort(); active.current = null;
  }, []);
  const read = useCallback((background = false) => {
    if (background && active.current) return active.current.promise;
    invalidate();
    const generation = sequence.current;
    const controller = new AbortController();
    const current = () => !controller.signal.aborted && generation === sequence.current;
    setPending(true);
    const promise = Promise.resolve().then(async () => {
      if (!current()) return;
      try {
        const result = await portalFetch<Page>(`portal/requests?page=${page}`, {signal: controller.signal});
        if (current()) {setData(result); setError('');}
      } catch (err) {
        if (!current()) return;
        if (err instanceof PortalError && (err.status === 401 || err.status === 403)) {
          setData(null); onUnauthorized();
        } else setError(err instanceof Error ? err.message : '请求记录读取失败。');
      } finally {
        if (current()) {setPending(false); active.current = null;}
      }
    });
    active.current = {controller, promise};
    return promise;
  }, [page, onUnauthorized, invalidate]);
  useEffect(() => {void read(); return invalidate;}, [read, invalidate]);
  useHeartbeat(() => read(true), 20000);
  const visible = data?.page === page ? data : null;
  const previousDisabled = pending || page <= 1;
  const nextDisabled = pending || !visible || page >= 10000 || page * 30 >= visible.total;

  return <section className="portal-panel portal-history" aria-labelledby="portal-history-title" aria-busy={pending}>
    <div className="portal-panel-heading"><div><h2 id="portal-history-title">我的请求记录</h2>
      <p className="portal-small">积分列显示你实际扣除的积分。费用待核实时不会显示为 0；平台承担的超额费用不计入你的消耗。</p></div>
      <button className="portal-button" disabled={pending} onClick={() => void read()}><RefreshCw size={14} />刷新记录</button></div>
    {error && <div className="portal-error" role="alert">{error}{visible && ' · 以下为上次读取的记录。'}
      <button className="portal-button" disabled={pending} onClick={() => void read()}>重试记录</button></div>}
    {!visible ? <p className="portal-empty" role="status">{pending ? '正在读取请求记录…' : '记录暂时不可用，不影响账号与积分。'}</p>
      : !visible.items.length ? <div className="portal-empty"><History size={25} /><p>还没有你的请求记录</p><span className="portal-small">调用开放后，使用自己的密钥发起请求即可在这里查看。</span></div>
      : <><nav className="portal-history-quick-pages" aria-label="请求记录快捷翻页"><span>共 {visible.total} 条 · 第 {visible.page} 页</span>
        <button className="portal-button" aria-label="上一页记录" disabled={previousDisabled} onClick={() => setPage(page - 1)}><ChevronLeft size={14} /></button>
        <button className="portal-button" aria-label="下一页记录" disabled={nextDisabled} onClick={() => setPage(page + 1)}><ChevronRight size={14} /></button></nav>
      <div className="portal-request-list"><table className="portal-table portal-request-table"><caption className="sr-only">第 {visible.page} 页请求记录</caption>
        <thead><tr><th scope="col">时间</th><th scope="col">模型</th><th scope="col">状态</th><th scope="col">Token</th><th scope="col">耗时</th><th scope="col">积分</th></tr></thead>
        <tbody>{visible.items.map((row, index) => <tr key={`${row.ts}-${index}`}>
          <td data-label="时间">{portalTime(row.ts)}</td><td data-label="模型" className="portal-request-model">{row.model}</td>
          <td data-label="状态"><span className={`portal-request-status ${requestStatus(row.status).tone}`}>{requestStatus(row.status).label}<small>{row.status}</small></span></td>
          <td data-label="Token">{requestTokens(row.prompt_tokens, row.completion_tokens)}</td>
          <td data-label="耗时">{requestLatency(row.latency_ms)}</td>
          <td data-label="积分" className="portal-request-credit">{requestCredit(row.credit)}{requestCredit(row.credit) === '待核实' && <span className="sr-only">，不是免费请求</span>}</td>
        </tr>)}</tbody></table></div></>}
    <nav className="portal-actions portal-ops-pagination" aria-label="请求记录分页">
      <span className="portal-small" role="status">{visible ? `共 ${visible.total} 条 · 第 ${visible.page} 页` : `第 ${page} 页`}{pending ? ' · 正在读取…' : ''}</span>
      <button className="portal-button" disabled={previousDisabled} onClick={() => setPage(page - 1)}>上一页</button>
      <button className="portal-button" disabled={nextDisabled} onClick={() => setPage(page + 1)}>下一页</button>
    </nav>
  </section>;
}
