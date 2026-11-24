'use client';

import {useCallback,useEffect,useRef,useState} from 'react';
import {portalFetch,portalTime} from '@/lib/portal-api';
import {useT} from '@/lib/i18n/provider';

type Invite={id:string;created_at:number;expires_at:number;used_at:number|null;revoked:number};
type Issued={id:string;code:string;expires_at:number};

export default function RegistrationInvites() {
  const t=useT();
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
    void load(controller.signal).catch(err=>{if(!controller.signal.aborted)setError(err instanceof Error?err.message:t('portal.invites.errLoad'));});
    return ()=>{mounted.current=false;controller.abort();};
  },[load,t]);
  async function create() {
    if(flight.current||issued.length||uncertain)return;
    flight.current=true;setBusy(true);setError('');
    try {
      const result=await portalFetch<{items:Issued[]}>('portal-admin/invitations',{method:'POST',body:JSON.stringify({count,days})});
      if(!mounted.current)return;setIssued(result.items);
      try{await load();}catch{if(mounted.current)setError(t('portal.invites.createdRefreshFailed'));}
    }catch(err){if(mounted.current){setError(err instanceof Error?err.message:t('portal.invites.createUncertain'));setUncertain(true);}}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  async function refresh() {
    if(flight.current)return;flight.current=true;setBusy(true);setError('');
    try{await load();}catch(err){if(mounted.current)setError(err instanceof Error?err.message:t('portal.invites.errRead'));}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  async function revoke(id:string) {
    if(flight.current)return;flight.current=true;setBusy(true);setError('');
    try{await portalFetch(`portal-admin/invitations/${encodeURIComponent(id)}`,{method:'DELETE'});await load();}
    catch(err){if(mounted.current)setError(err instanceof Error?err.message:t('portal.invites.revokeUncertain'));}
    finally{flight.current=false;if(mounted.current)setBusy(false);}
  }
  const stateOf=(item:Invite)=>item.used_at?t('portal.invites.used'):item.revoked?t('portal.invites.revoked'):item.expires_at<=Date.now()/1000?t('portal.invites.expired'):t('portal.invites.unused');
  return <section className="portal-panel" aria-labelledby="registration-invites-title">
    <div className="portal-panel-heading"><div><h2 id="registration-invites-title">{t('portal.invites.title')}</h2><p className="portal-small">{t('portal.invites.desc')}</p></div>
      <button className="portal-button" onClick={()=>void refresh()} disabled={busy}>{t('portal.invites.refresh')}</button></div>
    {error&&<p className="portal-error" role="alert">{error}</p>}
    <form className="portal-actions" onSubmit={event=>{event.preventDefault();void create();}}>
      <label className="portal-field">{t('portal.invites.count')}<input className="portal-input" type="number" min={1} max={20} required value={count} disabled={busy} onChange={event=>setCount(Number(event.target.value))}/></label>
      <label className="portal-field">{t('portal.invites.days')}<input className="portal-input" type="number" min={1} max={30} required value={days} disabled={busy} onChange={event=>setDays(Number(event.target.value))}/></label>
      <button className="portal-button primary" disabled={busy||!!issued.length||uncertain}>{busy?t('portal.invites.busy'):issued.length?t('portal.invites.saveFirst'):uncertain?t('portal.invites.refreshFirst'):t('portal.invites.generate')}</button>
    </form>
    {!!issued.length&&<div className="portal-notice"><strong>{t('portal.invites.secretOnce')}</strong>
      {issued.map(item=><label className="portal-field" key={item.id}>{t('portal.invites.expiresAt',{time:portalTime(item.expires_at)})}<input className="portal-input" readOnly value={item.code} autoComplete="off" spellCheck={false} onFocus={event=>event.target.select()}/></label>)}
      <button className="portal-button" onClick={()=>setIssued([])}>{t('portal.invites.secretSaved')}</button></div>}
    {!items.length&&<p className="portal-empty">{t('portal.invites.empty')}</p>}
    {items.map(item=><article className="portal-account" key={item.id}><div><div className="portal-account-name">{t('portal.invites.item',{id:item.id.slice(0,8)})}</div><p className="portal-small">{t('portal.invites.expiryLine',{state:stateOf(item),time:portalTime(item.expires_at)})}</p></div>
      <button className="portal-button" disabled={busy||!!item.used_at||!!item.revoked||item.expires_at<=Date.now()/1000} onClick={()=>void revoke(item.id)}>{t('portal.invites.revoke')}</button></article>)}
  </section>;
}
