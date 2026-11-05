'use client';

import {useCallback,useEffect,useRef,useState} from 'react';
import {portalFetch,portalTime} from '@/lib/portal-api';

type Invite={id:string;created_at:number;expires_at:number;used_at:number|null;revoked:number};
type Issued={id:string;code:string;expires_at:number};

export default function RegistrationInvites() {
  const [items,setItems]=useState<Invite[]>([]),[issued,setIssued]=useState<Issued[]>([]);
  const [count,setCount]=useState(1),[days,setDays]=useState(7);
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[uncertain,setUncertain]=useState(false);
  const flight=useRef(false),mounted=useRef(true);
  const load=useCallback(async(signal?:AbortSignal)=>{
    const result=await portalFetch<{items:Invite[]}>('portal-admin/invitations',{signal});
    if(mounted.current&&!signal?.aborted){setItems(result.items);setUncertain(false);}
  },[]);
  useEffect(()=>{
    mounted.current=true;const controller=new AbortController();
    void load(controller.signal).catch(err=>{if(!controller.signal.aborted)setError(err instanceof Error?err.message:'邀请码读取失败');});
    return ()=>{mounted.current=false;controller.abort();};
  },[load]);
  async function create() {
    if(flight.current||issued.length||uncertain)return;
    flight.current=true;setBusy(true);setError('');
    try {
      const result=await portalFetch<{items:Issued[]}>('portal-admin/invitations',{method:'POST',body:JSON.stringify({count,days})});
      if(!mounted.current)return;setIssued(result.items);
      try{await load();}catch{if(mounted.current)setError('邀请码已生成，请先保存；列表刷新失败，可以稍后重新读取。');}
    }catch(err){if(mounted.current){setError(err instanceof Error?err.message:'生成结果待确认，请先刷新核对');setUncertain(true);}}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  async function refresh() {
    if(flight.current)return;flight.current=true;setBusy(true);setError('');
    try{await load();}catch(err){if(mounted.current)setError(err instanceof Error?err.message:'读取失败');}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  async function revoke(id:string) {
    if(flight.current)return;flight.current=true;setBusy(true);setError('');
    try{await portalFetch(`portal-admin/invitations/${encodeURIComponent(id)}`,{method:'DELETE'});await load();}
    catch(err){if(mounted.current)setError(err instanceof Error?err.message:'操作结果待确认，请先刷新核对');}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  return <section className="portal-panel" aria-labelledby="registration-invites-title">
    <div className="portal-panel-heading"><div><h2 id="registration-invites-title">注册邀请码</h2><p className="portal-small">一次成功注册即失效；设备登记与IP限流由服务端执行，注册不赠送调用额度。</p></div>
      <button className="portal-button" onClick={()=>void refresh()} disabled={busy}>刷新列表</button></div>
    {error&&<p className="portal-error" role="alert">{error}</p>}
    <form className="portal-actions" onSubmit={event=>{event.preventDefault();void create();}}>
      <label className="portal-field">数量<input className="portal-input" type="number" min={1} max={20} required value={count} disabled={busy} onChange={event=>setCount(Number(event.target.value))}/></label>
      <label className="portal-field">有效天数<input className="portal-input" type="number" min={1} max={30} required value={days} disabled={busy} onChange={event=>setDays(Number(event.target.value))}/></label>
      <button className="portal-button primary" disabled={busy||!!issued.length||uncertain}>{busy?'处理中…':issued.length?'请先保存邀请码':uncertain?'请先刷新核对':'生成一次性邀请码'}</button>
    </form>
    {!!issued.length&&<div className="portal-notice"><strong>明文仅显示这一次，请通过安全渠道发送给用户。</strong>
      {issued.map(item=><label className="portal-field" key={item.id}>有效至 {portalTime(item.expires_at)}<input className="portal-input" readOnly value={item.code} autoComplete="off" spellCheck={false} onFocus={event=>event.target.select()}/></label>)}
      <button className="portal-button" onClick={()=>setIssued([])}>我已保存，隐藏明文</button></div>}
    {!items.length&&<p className="portal-empty">暂无邀请码</p>}
    {items.map(item=><article className="portal-account" key={item.id}><div><div className="portal-account-name">邀请码 {item.id.slice(0,8)}</div><p className="portal-small">{item.used_at?'已使用':item.revoked?'已撤销':item.expires_at<=Date.now()/1000?'已过期':'未使用'} · {portalTime(item.expires_at)} 到期</p></div>
      <button className="portal-button" disabled={busy||!!item.used_at||!!item.revoked||item.expires_at<=Date.now()/1000} onClick={()=>void revoke(item.id)}>撤销</button></article>)}
  </section>;
}
