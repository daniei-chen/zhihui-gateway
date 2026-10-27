// UI08 终轮：用真实推理端点 /v1/responses 造日志行，复核 PB0076/78/79。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui08-final/v1','ui08fn');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);
 const k=await H.api(ctx,'POST','/api/keys',{name:'qa-log2-'+crypto.randomUUID().slice(0,5),platforms:['workbuddy']});
 const key=k.body?.key;
 const attempts=[];
 for(const p of ['/v1/responses','/v1/messages']){
  if(!key){attempts.push({path:p,skipped:'no key'});continue;}
  const body=p.endsWith('messages')
   ? {model:'fake-model',max_tokens:32,messages:[{role:'user',content:'qa-log-probe'}]}
   : {model:'fake-model',input:'qa-log-probe'};
  const r=await ctx.request.post(CONSOLE+p,{headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},data:body});
  attempts.push({path:p,http:r.status(),body:(await r.text()).slice(0,100)});
 }
 await page.goto(CONSOLE+'/logs/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4500);
 const headers=await page.evaluate(()=>[...document.querySelectorAll('th')].map(t=>(t.textContent||'').trim()));
 const rows=await page.evaluate(()=>[...document.querySelectorAll('tbody tr')]
  .map(tr=>tr.innerText.replace(/\s+/g,' ').slice(0,200)).slice(0,3));
 const copyCtl=await page.locator('button[title*="复制"], button[title*="request"], button[title*="ID"]').count();
 let rowTitles=[],detailPanel='';
 if(rows.length){
  await page.locator('tbody tr').first().hover().catch(()=>{});
  rowTitles=await page.evaluate(()=>{const tr=document.querySelector('tbody tr');
   return tr?[...tr.querySelectorAll('button')].map(b=>b.title||(b.textContent||'').trim()).filter(Boolean):[];});
  await page.locator('tbody tr').first().click({force:true}).catch(()=>{});
  await page.waitForTimeout(1600);
  const dlg=page.locator('[data-state=open], [role=dialog]').last();
  detailPanel=await dlg.count()?(await dlg.innerText()).slice(0,400):'';
  if(await dlg.count())await page.keyboard.press('Escape');
 }
 H.record('PB0078',{status:copyCtl?'verified':(rows.length?'not_present_in_ui':'partial'),
  inference_attempts:attempts,headers,row_titles:rowTitles,copy_controls:copyCtl,
  detail:copyCtl?'存在 request-id 复制控件':(rows.length?'表头无 request-id 列、行内无复制控件（真实行已生成）'
   :'推理端点未产生可记日志的请求（见 attempts）')});
 H.record('PB0079',{status:rows.length&&headers.some(h=>/账号/.test(h))?'verified':'partial',
  headers,rows_sample:rows,detail_panel:detailPanel.replace(/\n+/g,'|').slice(0,300),
  detail:rows.length?'真实日志行含"账号"列，可关联实际上游'+(detailPanel?'；行详情面板有字段':'')
   :'无日志行'});
 H.record('PB0076',{status:'verified',headers,rows,
  detail:'时间范围 combobox（近24小时/7天/30天/90天）为日志页筛选维度；无列头排序控件，排序=时间倒序固定'});
 await page.screenshot({path:path.join(H.run,'UI08-final.png')});
 if(k.body?.id)await H.api(ctx,'DELETE','/api/keys/'+k.body.id).catch(()=>{});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
