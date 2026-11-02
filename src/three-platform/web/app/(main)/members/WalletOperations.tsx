'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {Dialog, DialogBody, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {portalFetch, portalTime} from '@/lib/portal-api';
import {creditInputUnits, exactCredit, operationLabel, type OperationPage, type WalletPending, type WalletDebt, type WalletHistory} from '@/lib/portal-operations';

type Data = {pending: OperationPage<WalletPending>; debts: OperationPage<WalletDebt>; history: OperationPage<WalletHistory>};
type Target = {kind: 'resolve'; row: WalletPending} | {kind: 'repay' | 'waive'; row: WalletDebt};
type Submission = {path: string; body: object};
const emptyPage = <T,>(): OperationPage<T> => ({items: [], total: 0, page: 1, page_size: 30});

export default function WalletOperations({onChanged}: {onChanged: () => Promise<void>}) {
  const [data, setData] = useState<Data>({pending: emptyPage(), debts: emptyPage(), history: emptyPage()});
  const [pages, setPages] = useState({pending: 1, debts: 1, history: 1});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [target, setTarget] = useState<Target | null>(null);
  const seq = useRef(0);
  const load = useCallback(async (signal?: AbortSignal) => {
    const generation = ++seq.current;
    setLoading(true);
    try {
      const [pending, debts, history] = await Promise.all([
        portalFetch<OperationPage<WalletPending>>(`portal-admin/wallet-pending?page=${pages.pending}`, {signal}),
        portalFetch<OperationPage<WalletDebt>>(`portal-admin/wallet-debts?page=${pages.debts}`, {signal}),
        portalFetch<OperationPage<WalletHistory>>(`portal-admin/wallet-history?page=${pages.history}`, {signal}),
      ]);
      if (signal?.aborted || generation !== seq.current) return;
      setData({pending, debts, history}); setError('');
    } catch (err) {
      if (!signal?.aborted && generation === seq.current) {
        setError(err instanceof Error ? err.message : '账本读取失败');
        setTarget(null);
      }
    } finally { if (!signal?.aborted && generation === seq.current) setLoading(false); }
  }, [pages]);
  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => { controller.abort(); seq.current++; };
  }, [load]);
  async function changed(message: string) {
    setTarget(null); setNotice(message);
    await Promise.all([load(), onChanged()]);
  }
  function pagination(scope: keyof Data) {
    const page = data[scope];
    return <div className="portal-actions portal-ops-pagination"><span className="portal-small">共 {page.total} 条 · 第 {pages[scope]} 页</span>
      <button className="portal-button" disabled={loading || pages[scope] <= 1} onClick={() => setPages(current => ({...current, [scope]: current[scope] - 1}))}>上一页</button>
      <button className="portal-button" disabled={loading || pages[scope] * page.page_size >= page.total} onClick={() => setPages(current => ({...current, [scope]: current[scope] + 1}))}>下一页</button></div>;
  }
  return <section aria-labelledby="wallet-ops-title" className="portal-ops">
    <div className="portal-account"><div><p className="portal-eyebrow">Wallet controls</p><h2 id="wallet-ops-title">积分异常与欠额处理</h2>
      <p className="portal-small">不凭超时放开冻结，不修改供应商原扣费。每次处置必须记录依据，并由服务端再次核验。</p></div>
      <button className="portal-button" disabled={loading || !!target} onClick={() => void load()}>{loading ? '读取账本…' : '刷新账本'}</button></div>
    {notice && <p className="portal-notice" role="status">{notice}</p>}
    {error ? <p className="portal-error" role="alert">{error} · 已隐藏旧账本并禁止操作，请刷新重试。</p> : <>
      <div className="portal-panel"><h3>费用待核对 <span className="portal-badge pending">{data.pending.total}</span></h3>
        <p className="portal-small">请求有可信结束凭证，或所属进程已明确退出，才可凭供应商证据核对；仍在执行或无法确认结束时禁止结算。没有强制解锁入口。</p>
        {data.pending.items.map(row => <article className="portal-account" key={row.request_id}><div className="portal-ops-detail"><strong>{row.username}</strong>
          <p className="portal-small portal-ops-id">请求 {row.request_id} · 密钥 #{row.key_id}</p>
          <p className="portal-small">{portalTime(row.created_at)} · 冻结 {exactCredit(row.held_units)} · {row.request?.model || '主库暂无请求记录'}</p>
          <span className="portal-badge pending">{row.state === 'unknown' ? '费用未知' : row.state === 'reserved' ? '尚未发送' : '已发出 / 在途'}</span>
          <p className="portal-small">{row.blocked_reason || (row.end_confirmed ? '请求已结束，可凭供应商证据核对；不代表费用为0。' : '进程已退出，可凭供应商证据核对。')}{row.receipt_units !== null ? ` 已有费用回执 ${exactCredit(row.receipt_units)}，不可填写不同金额。` : ''}</p></div>
          <button className="portal-button" disabled={loading || !row.can_resolve} onClick={() => setTarget({kind: 'resolve', row})}>核对费用</button></article>)}
        {!loading && !data.pending.items.length && <p className="portal-empty">本页没有待核对请求。</p>}{pagination('pending')}
      </div>
      <div className="portal-panel"><h3>用户欠额 <span className="portal-badge pending">{data.debts.total}</span></h3>
        <p className="portal-small">抵扣只使用该用户已入池、未冻结且未过期的积分；豁免只减少欠额，不增加积分。剩余欠额或未知请求仍会阻止新调用。</p>
        {data.debts.items.map(row => <article className="portal-account" key={row.owner_id}><div><strong>{row.username}</strong>
          <p className="portal-small">欠额 {exactCredit(row.amount_units)} · 可用于抵扣 {exactCredit(row.repayable_units)} · 用户{row.enabled ? '已启用' : '已停用'}</p></div>
          <div className="portal-actions"><button className="portal-button" disabled={loading || row.repayable_units <= 0} onClick={() => setTarget({kind: 'repay', row})}>用现有积分抵扣</button>
            <button className="portal-button" disabled={loading} onClick={() => setTarget({kind: 'waive', row})}>登记豁免</button></div></article>)}
        {!loading && !data.debts.items.length && <p className="portal-empty">本页没有未处理欠额。</p>}{pagination('debts')}
      </div>
      <div className="portal-panel"><h3>处置记录</h3><p className="portal-small">只追加、不覆盖：操作者、金额、原欠额及处理后欠额、原因与凭证说明。</p>
        {data.history.items.map(row => <article className="portal-account" key={`${row.kind}:${row.id}`}><div className="portal-ops-detail">
          <strong>{row.username} · {operationLabel(row.kind)} · {exactCredit(row.amount_units)} 积分</strong>
          <p className="portal-small">{portalTime(row.created_at)} · 操作者 {row.actor}{row.before_units !== null && row.after_units !== null ? ` · 欠额 ${exactCredit(row.before_units)} → ${exactCredit(row.after_units)}` : ''}</p>
          <p className="portal-small portal-ops-id">标识 {row.id}</p><p className="portal-small">原因：{row.reason}</p>{row.expires_at !== null && <p className="portal-small">赠送到期时间：{portalTime(row.expires_at)}</p>}{row.evidence && <p className="portal-small">凭证：{row.evidence}</p>}</div></article>)}
        {!loading && !data.history.items.length && <p className="portal-empty">暂无人工处置记录。</p>}{pagination('history')}
      </div>
    </>}
    {target && <ResolutionDialog target={target} close={() => setTarget(null)} saved={changed} />}
  </section>;
}

function ResolutionDialog({target, close, saved}: {target: Target; close: () => void; saved: (message: string) => Promise<void>}) {
  const [credit, setCredit] = useState(target.kind === 'resolve' ? target.row.receipt_units === null ? '' : exactCredit(target.row.receipt_units)
    : exactCredit(target.kind === 'repay' ? Math.min(target.row.amount_units, target.row.repayable_units) : target.row.amount_units));
  const [reason, setReason] = useState('');
  const [evidence, setEvidence] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [submitted, setSubmitted] = useState<Submission | null>(null);
  const actionId = useRef('');
  const inFlight = useRef(false);
  const amount = creditInputUnits(credit);
  const maximum = target.kind === 'resolve' ? null : target.kind === 'repay' ? Math.min(target.row.amount_units, target.row.repayable_units) : target.row.amount_units;
  const valid = amount !== null && (target.kind === 'resolve' || amount > 0) && (maximum === null || amount <= maximum)
    && reason.trim().length >= 8 && evidence.trim().length >= 8 && confirmed;
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (inFlight.current || !valid) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      if (!actionId.current) actionId.current = crypto.randomUUID().replaceAll('-', '');
      const operation = submitted || (target.kind === 'resolve' ? {
        path: `portal-admin/wallet-pending/${encodeURIComponent(target.row.request_id)}/resolve`,
        body: {credit, reason: reason.trim(), evidence: evidence.trim()},
      } : {
        path: `portal-admin/wallet-debts/${encodeURIComponent(target.row.owner_id)}/adjust`,
        body: {action_id: actionId.current, kind: target.kind, credit, expected_units: target.row.amount_units, reason: reason.trim(), evidence: evidence.trim()},
      });
      setSubmitted(operation);
      const result = await portalFetch<{message: string}>(operation.path, {method: 'POST', body: JSON.stringify(operation.body)});
      await saved(result.message);
    } catch (err) { setError(err instanceof Error ? err.message : '处理结果未知，请刷新记录核对，不要更换内容反复提交'); }
    finally { inFlight.current = false; setBusy(false); }
  }
  return <Dialog open onOpenChange={open => { if (!open && !busy) close(); }}>
    <DialogContent className="portal-ops-dialog" showCloseButton={!busy}>
      <DialogHeader><DialogTitle>{operationLabel(target.kind)} · {target.row.username}</DialogTitle><DialogDescription>
        {target.kind === 'resolve' ? '只有具备可信结束凭证或进程明确退出的未知请求可核对。0积分也必须有供应商依据。' : target.kind === 'repay'
          ? '扣除该用户现有积分并减少欠额，不追加调用费用、不修改原请求。' : '明确豁免欠额；不会返还或增加可用积分，也不改变原供应商费用。'}
      </DialogDescription></DialogHeader>
      <DialogBody className="max-h-[calc(100dvh-12rem)]"><form className="portal-ops-form" onSubmit={event => void submit(event)}>
        <p className="portal-small">{target.kind === 'resolve' ? `请求 ${target.row.request_id} · 冻结 ${exactCredit(target.row.held_units)}` : `当前欠额 ${exactCredit(target.row.amount_units)} · 可用抵扣 ${exactCredit(target.row.repayable_units)}`}</p>
        <label className="portal-field">{target.kind === 'resolve' ? '核实的实际费用' : '本次处置积分'}<input className="portal-input" inputMode="decimal" required maxLength={17} value={credit} onChange={event => setCredit(event.target.value)} disabled={busy || !!submitted || (target.kind === 'resolve' && target.row.receipt_units !== null)} /></label>
        {amount === null && <p className="portal-small">请填写非负十进制数，最多6位小数，不接受指数格式。</p>}
        {amount !== null && maximum !== null && amount > maximum && <p className="portal-error">金额超过本次可处置上限。</p>}
        <label className="portal-field">核对原因（8至500字）<textarea className="portal-input" required minLength={8} maxLength={500} rows={2} value={reason} onChange={event => setReason(event.target.value)} disabled={busy || !!submitted} /></label>
        <label className="portal-field">凭证说明 / 单号（8至500字）<textarea className="portal-input" required minLength={8} maxLength={500} rows={2} value={evidence} onChange={event => setEvidence(event.target.value)} disabled={busy || !!submitted} /></label>
        <p className="portal-small">仅填写证据摘要或单号，不要粘贴密码、Token或完整授权文件。</p>
        <label className="portal-ops-confirm"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} disabled={busy || !!submitted} />我已核对用户、金额和凭证，确认记录此处置。</label>
        {error && <p className="portal-error" role="alert">{error}。提交内容已锁定；重试使用同一处置标识，或关闭后先刷新处置记录。</p>}
        <div className="portal-actions"><button type="button" className="portal-button" disabled={busy} onClick={close}>取消</button><button className="portal-button primary" disabled={busy || !valid}>{busy ? '正在提交…' : submitted ? '按原内容重试' : '确认提交处置'}</button></div>
      </form></DialogBody>
    </DialogContent>
  </Dialog>;
}
