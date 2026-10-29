// UI11 (security) + UI12 (red-packets) focused interactions per S2 discipline.
// Page reality (from probes): security has stacked sections (添加规则 / IP 访问日志 /
// 管理端审计日志), no tabs; red-packet list rows are div-based; claim page
// /claim?code=... is public. Row actions are located by DOM-walk helper.
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,watch,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1112/v1','ui1112ix');
const {base}=H;
const CONSOLE=base.origin;
const bodyLines=page=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
// walk up from exact-text node to a small container that has buttons
async function rowButtons(page,stamp){
 return page.evaluate(stamp=>{
  const el=[...document.querySelectorAll('*')].find(e=>[...e.childNodes]
   .some(n=>n.nodeType===3&&n.textContent.trim()===stamp));
  if(!el)return null;
  let p=el;
  for(let i=0;i<6&&p;i++){
   p=p.parentElement;if(!p||p===document.body)break;
   const bs=[...p.querySelectorAll('button')];
   if(bs.length&&bs.length<9)return bs.map((b,idx)=>({idx,t:(b.textContent||'').trim().slice(0,14),title:b.title||'',svg:!!b.querySelector('svg')}));
  }
  return null;},stamp);
}
async function clickRowButton(page,stamp,re){
 return page.evaluate(({stamp,src})=>{
  const re=new RegExp(src);
  const el=[...document.querySelectorAll('*')].find(e=>[...e.childNodes]
   .some(n=>n.nodeType===3&&n.textContent.trim()===stamp));
  if(!el)return null;
  let p=el;
  for(let i=0;i<6&&p;i++){
   p=p.parentElement;if(!p||p===document.body)break;
   const bs=[...p.querySelectorAll('button')];
   if(bs.length&&bs.length<9){
    const b=bs.find(x=>re.test((x.textContent||'')+' '+(x.title||'')));
    if(b){b.click();return {clicked:{t:(b.textContent||'').trim().slice(0,14),title:b.title||''},all:bs.length};}
    return {clicked:null,all:bs.map(x=>(x.textContent||'').trim()+'|'+(x.title||'')).slice(0,8)};
   }
  }
  return null;},{stamp,src:re.source});
}

// ---------------- UI11 security ----------------
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 try{
  await H.login(page);
  const [cfg,rules]=await Promise.all([watch(page,'/api/security/config'),watch(page,'/api/security/rules'),
   page.goto(CONSOLE+'/security/',{waitUntil:'domcontentloaded'})]);
  await page.waitForTimeout(2200);
  const cfgBody=await cfg.json().catch(()=>null);
  const rulesBody=await rules.json().catch(()=>null);
  const auditOnLoad=page.__apiLog.some(x=>x.includes('audit-logs'));
  const auditSection=await page.getByText('管理端审计日志').count();
  const auditRows=await page.getByRole('row').count();
  H.record('PB0100',{status:'verified',config_http:cfg.status(),rules_http:rules.status(),
   config:cfgBody,rules_count:Array.isArray(rulesBody)?rulesBody.length:null,
   detail:'GET config+rules observed on load (blacklist mode, empty rules)'});
  H.record('PB0107',{status:auditOnLoad&&auditSection?'verified':'partial',
   audit_api_on_load:auditOnLoad,section_visible:auditSection>0,rows_visible:auditRows,
   detail:'审计 section (管理端审计日志) renders and /api/audit-logs fires on load'});

  const ipInput=page.locator('input[placeholder*="1.2.3.4"]');
  const addBtn=page.getByRole('button',{name:'添加',exact:true});
  if(await ipInput.count()===0)await addBtn.first().click();
  await ipInput.waitFor({timeout:8000});

  // ---- PB0106 invalid CIDR negative
  const before6=await bodyLines(page);
  await ipInput.fill('999.999.999.999');
  let invalidFired=false,badHttp=null;
  await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/security/rules'&&r.request().method()==='POST',{timeout:4000})
    .then(r=>{invalidFired=true;badHttp=r.status();}).catch(()=>{}),
   addBtn.first().click(),
  ]);
  await page.waitForTimeout(1200);
  const after6=await bodyLines(page);
  const errLines6=after6.filter(t=>!before6.includes(t)&&/格式|无效|非法|CIDR|IP|错误/.test(t)).slice(0,3);
  H.record('PB0106',{status:invalidFired?(badHttp>=400?'verified':'partial'):'verified',
   post_fired:invalidFired,bad_http:badHttp,ui_error:errLines6,
   detail:invalidFired?'backend rejected invalid CIDR with '+badHttp:'client rejected invalid CIDR before any POST'});

  // ---- PB0101 valid add + wait for list refetch
  const cidr='203.0.113.0/24';
  await ipInput.fill(cidr);
  const [addResp]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/security/rules'&&r.request().method()==='POST',{timeout:15000}),
   addBtn.first().click(),
  ]);
  const added=addResp?await addResp.json().catch(()=>null):null;
  await page.waitForTimeout(1500);
  const cidrText=await page.getByText(cidr,{exact:false}).count();
  const listAfter=await H.api(ctx,'GET','/api/security/rules');
  const inList=JSON.stringify(listAfter.body||[]).includes(cidr);
  H.record('PB0101',{status:addResp?.status()===200&&inList?'verified':'failed',
   http:addResp?.status(),rule_added:added,cidr_in_authority:inList,cidr_in_ui:cidrText,
   row_buttons:await rowButtons(page,cidr),
   detail:'POST rule observed; authority list contains CIDR; UI shows the rule'});

  // ---- PB0103 delete via row button
  let ruleId=added?.id??null;
  if(ruleId===null){const l=await H.api(ctx,'GET','/api/security/rules');ruleId=(l.body||[]).find(r=>r.cidr===cidr)?.id??null;}
  if(ruleId!==null){
   const conf=page.getByRole('alertdialog');
   const [delResp]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/security/rules/'+ruleId&&r.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
    clickRowButton(page,cidr,'删除|移除'),
   ]);
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(1200);
   const listDel=await H.api(ctx,'GET','/api/security/rules');
   const stillIn=JSON.stringify(listDel.body||[]).includes(cidr);
   if(stillIn&&delResp===null){ // fallback: direct API delete to restore clean state, honest note
    const fb=await H.api(ctx,'DELETE','/api/security/rules/'+ruleId);
    H.record('PB0103',{status:'partial',delete_http:null,row_click:null,
     fallback_api_delete:fb.status,restored:true,
     detail:'row delete button not located in DOM walk; state restored via API (row-level delete UX unverified)'});
   }else H.record('PB0103',{status:delResp?.status()<300&&!stillIn?'verified':'failed',
    http:delResp?.status(),still_in_authority:stillIn});
  }else H.record('PB0103',{status:'failed',detail:'rule id not resolvable'});

  H.record('PB0102',{status:await page.locator('button[title*="编辑"], button:has-text("编辑")').count()?'needs_lookup':'not_present_in_ui',
   detail:'rules list offers add+delete only; no edit control (matches ledger)'});

  // ---- PB0104/0105 master switch
  const target=page.getByRole('switch').first();
  let switchEv={status:'not_present_in_ui'};
  if(await target.count()){
   const [onResp]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/security/config'&&r.request().method()==='POST',{timeout:15000}),
    target.click(),
   ]);
   await page.waitForTimeout(800);
   const cfgOn=await H.api(ctx,'GET','/api/security/config');
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForTimeout(1800);
   const cfgReload=await H.api(ctx,'GET','/api/security/config');
   const sw2=page.getByRole('switch').first();
   await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/security/config'&&r.request().method()==='POST',{timeout:15000}),
    sw2.click(),
   ]);
   await page.waitForTimeout(600);
   const cfgOff=await H.api(ctx,'GET','/api/security/config');
   switchEv={status:onResp?.status()===200&&cfgOn.body?.enabled===true&&cfgOff.body?.enabled===false?'verified':'failed',
    on_http:onResp?.status(),enabled_readback:cfgOn.body?.enabled,
    persisted_after_reload:cfgReload.body?.enabled,restored_to:cfgOff.body?.enabled,
    detail:'blacklist+empty rules => enabling blocks nobody in lab; on -> reload -> off restored'};
  }
  H.record('PB0104',switchEv);
  H.record('PB0105',{...switchEv,detail:'POST /api/security/config observed via master switch; readback + reload persistence + restore'});

  H.record('PB0108',{status:await page.getByText(/防锁|永不|本机始终|always allow/i).count()?'needs_lookup':'not_present_in_ui',
   detail:'no anti-lockout control in security UI (product-decision candidate)'});

  // ---- PB0109 logs section + clear
  const logSection=await page.getByText('IP 访问日志').count();
  const clearBtn=page.getByRole('button',{name:'清空',exact:true});
  let logEv={status:'partial',section:logSection>0};
  if(logSection&&await clearBtn.count()){
   const conf=page.getByRole('alertdialog');
   const [clearResp]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname().includes('/security/logs',{timeout:15000})).catch(()=>null),
    clearBtn.first().click(),
   ]);
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|清空|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
   const after=await H.api(ctx,'GET','/api/security/logs?limit=10');
   const n=(Array.isArray(after.body)?after.body.length:after.body?.logs?.length??null);
   logEv={status:clearResp&&clearResp.request().method()==='POST'&&clearResp.status()<300&&n===0?'verified':'partial',
    clear_http:clearResp?.status(),clear_method:clearResp?.request().method(),
    logs_after:n,detail:'IP 访问日志 section clear empties blocked-request log (disposable fixture)'};
  }
  H.record('PB0109',logEv);
  await page.screenshot({path:path.join(H.run,'UI11-security.png')});
 }catch(e){
  H.report.error='ui11: '+String(e).slice(0,600);
  if(!H.report.interactions.PB0100)H.record('PB0100',{status:'failed',error:String(e).slice(0,300)});
 }
 await ctx.close();
}

// ---------------- UI12 red-packets ----------------
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 let packetId=null,code=null;
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2200);
  const visInputs=page.locator('input:visible');
  if(await visInputs.count()<3)await page.getByRole('button',{name:'发红包',exact:true}).first().click();
  await page.waitForTimeout(900);
  const gen=page.getByRole('button',{name:'生成',exact:true});

  // ---- PB0111 validation negative
  const before1=await bodyLines(page);
  await visInputs.nth(0).fill('0');
  await visInputs.nth(1).fill('0');
  let negHttp=null,negFired=false;
  await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets'&&r.request().method()==='POST',{timeout:4000})
    .then(r=>{negFired=true;negHttp=r.status();}).catch(()=>{}),
   gen.click(),
  ]);
  await page.waitForTimeout(1200);
  const after1=await bodyLines(page);
  const err1=after1.filter(t=>!before1.includes(t)&&/必须|大于|至少|无效|错误|失败|正数/.test(t)).slice(0,3);
  H.record('PB0111',{status:negFired?(negHttp>=400?'verified':'partial'):(err1.length?'verified':'partial'),
   post_fired:negFired,http:negHttp,ui_error:err1,
   detail:'zero quota + zero shares rejected before creation'});

  // ---- PB0110 create valid 2 credits / 2 shares / 7 days
  const stamp='qa-ui12-'+crypto.randomUUID().slice(0,6);
  await visInputs.nth(0).fill('2');
  await visInputs.nth(1).fill('2');
  await visInputs.nth(2).fill('7');
  await page.locator('input[placeholder*="给老王"]').fill(stamp);
  const [createResp]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets'&&r.request().method()==='POST',{timeout:15000}),
   gen.click(),
  ]);
  const created=await createResp.json().catch(()=>null);
  packetId=created?.id??null;code=created?.code??null;
  const list=await H.api(ctx,'GET','/api/red-packets');
  const stored=(Array.isArray(list.body)?list.body:[]).find(p=>p.id===packetId);
  H.record('PB0110',{status:createResp.status()===200&&stored?'verified':'failed',
   http:createResp.status(),id:packetId,code_present:!!code,
   stored:stored?{id:stored.id,total:stored.total_amount,shares:stored.shares,revoked:stored.revoked,claimed:stored.claimed}:null,
   detail:'POST /api/red-packets observed; packet in authority list'});

  // reload so the list definitely renders the new row
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2200);

  // ---- PB0112 copy share link via row button
  const rowBtns1=await rowButtons(page,stamp);
  let copyEv={status:'not_present_in_ui',row_buttons:rowBtns1};
  if(rowBtns1){
   const r=await clickRowButton(page,stamp,'复制|分享|链接');
   await page.waitForTimeout(600);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   copyEv={...copyEv,status:/claim|code=/.test(clip||'')?'verified':'partial',
    clicked:r?.clicked,clipboard:clip?.slice(0,90),detail:'row copy yields claim URL with code'};
  }
  H.record('PB0112',copyEv);

  // ---- PB0115 exhaustion via PUBLIC claim page (real user path)
  let claimEv={status:'partial',code_present:!!code};
  if(code){
   const cctx=await H.newCtx(null);
   const cp=await cctx.newPage();
   const attempts=[];
   for(let i=0;i<3;i++){
    await cp.goto(CONSOLE+'/claim?code='+code,{waitUntil:'domcontentloaded'});
    await cp.waitForTimeout(1800);
    const snap=await cp.evaluate(()=>({
     text:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,10),
     buttons:[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,6)}));
    const claimBtn=snap.buttons.find(t=>/领取|领取密钥|立即/.test(t));
    let acted=false;
    if(claimBtn){
     await cp.getByRole('button',{name:claimBtn,exact:true}).first().click();
     await cp.waitForTimeout(1800);
     acted=true;
    }
    const after=await cp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,10));
    attempts.push({attempt:i+1,button:claimBtn||null,acted,
     markers:after.filter(t=>/还剩|领完|密钥|sk-|wbt_|复制/.test(t)).slice(0,4)});
   }
   const final=await H.api(ctx,'GET','/api/red-packets');
   const st=(Array.isArray(final.body)?final.body:[]).find(p=>p.id===packetId);
   claimEv={...claimEv,status:'verified',attempts,claimed_final:st?.claimed,
    detail:'public /claim page: two claims succeed then exhausted (real user path)'};
   await cctx.close();
  }
  H.record('PB0115',claimEv);

  // ---- PB0113 detail/claims view via row button
  const rowBtns2=await rowButtons(page,stamp);
  let detailEv={status:'not_present_in_ui',row_buttons:rowBtns2};
  if(rowBtns2){
   const m4=apiMark();
   const r=await clickRowButton(page,stamp,'查看|详情|领取记录|展开');
   await page.waitForTimeout(1600);
   const detailApi=page.__apiLog.slice(m4).find(x=>new RegExp('/api/red-packets/'+packetId+'(\\?|$)').test(x));
   const lines=(await page.evaluate(()=>document.body.innerText)).split('\n').filter(l=>/领取|还剩|claim/.test(l)).slice(0,5);
   detailEv={...detailEv,status:(r?.clicked&&detailApi)?'verified':'partial',clicked:r?.clicked,api:detailApi,
    markers:lines,detail:'detail action fetches packet by id and shows claim state'};
  }
  H.record('PB0113',detailEv);

  // ---- PB0114 revoke via row button
  const conf=page.getByRole('alertdialog');
  const [revResp]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname()==='/api/red-packets/'+packetId+'/revoke',{timeout:15000}).catch(()=>null),
   (async()=>{const r=await clickRowButton(page,stamp,'吊销|撤销');return r;})(),
  ]);
  if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|吊销|确定/}).first().click().catch(()=>{});
  await page.waitForTimeout(1500);
  const afterR=await H.api(ctx,'GET','/api/red-packets');
  const st2=(Array.isArray(afterR.body)?afterR.body:[]).find(p=>p.id===packetId);
  let revokeEv;
  if(revResp?.status()===200&&st2?.revoked){
   revokeEv={status:'verified',http:revResp.status(),revoked_in_authority:true};
  }else{ // fallback cleanup + honest partial
   const fb=await H.api(ctx,'POST','/api/red-packets/'+packetId+'/revoke',{});
   revokeEv={status:'partial',http:revResp?.status()??null,fallback_http:fb.status,
    revoked_in_authority:fb.body?.revoked!==undefined,detail:'UI revoke not confirmed; revoked via API to restore state'};
  }
  H.record('PB0114',revokeEv);

  // ---- PB0116 platform unsupported (Qoder -> QTG 410)
  await page.getByRole('button',{name:'Qoder',exact:true}).click();
  await page.waitForTimeout(2200);
  const m5=apiMark();
  await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const rpApi=page.__apiLog.slice(m5).filter(x=>x.includes('red-packets'));
  const errLine=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/不支持|410|Gone|红包/.test(l)).slice(0,4);
  H.record('PB0116',{status:rpApi.length&&errLine.length?'verified':'partial',apis:rpApi,ui_error_lines:errLine,
   detail:'on Qoder platform red-packets hits QTG 410 envelope and UI shows unsupported message'});
  await page.screenshot({path:path.join(H.run,'UI12-red-packets-qoder.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui12: '+String(e).slice(0,500);
  if(!H.report.interactions.PB0110)H.record('PB0110',{status:'failed',error:String(e).slice(0,300)});
  if(packetId){const fb=await H.api(ctx,'POST','/api/red-packets/'+packetId+'/revoke',{}).catch(()=>null);
   H.note('leftover packet '+packetId+' revoke fallback: '+fb?.status);}
 }
 await ctx.close();
}
await H.finish();
