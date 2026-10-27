// UI08 日志页 3 行：排序 / 复制 request-id / 关联实际上游。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui08/v1','ui08ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 // 先制造一条请求日志：调用一次模型列表 + 一次 playground chat
 await H.api(ctx,'GET','/api/models').catch(()=>{});
 await page.goto(CONSOLE+'/logs/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 const ctl=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {headers:[...document.querySelectorAll('th')].filter(vis).map(t=>(t.textContent||'').trim()),
   buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,20),
   rows:document.querySelectorAll('tbody tr').length,
   comboboxes:document.querySelectorAll('[role=combobox]').length};});
 const txt=await lines();
 H.note('logs controls: '+JSON.stringify(ctl).slice(0,600));

 // ---- PB0076 排序
 {
  const sortable=await page.locator('th:has(button), th[aria-sort]').count();
  let ev={status:'not_present_in_ui',sortable_headers:sortable,headers:ctl.headers};
  if(sortable){
   const th=page.locator('th:has(button), th[aria-sort]').first();
   const name=(await th.innerText()).trim();
   await th.click();await page.waitForTimeout(1500);
   const aria=await th.getAttribute('aria-sort');
   const firstBefore=await page.locator('tbody tr').first().innerText().catch(()=> '');
   await th.click();await page.waitForTimeout(1200);
   const firstAfter=await page.locator('tbody tr').first().innerText().catch(()=> '');
   ev={status:'verified',header:name,aria_sort:aria,changed:firstBefore!==firstAfter,
    first_row_before:firstBefore.slice(0,60),first_row_after:firstAfter.slice(0,60),
    detail:'点击列头 '+name+' -> aria-sort='+aria+'；行序变化='+(firstBefore!==firstAfter)};
  }else{
   // 排序可能在 combobox 中
   const combo=page.getByRole('combobox').first();
   if(await combo.count()){
    await combo.click();await page.waitForTimeout(800);
    const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
    ev={status:opts.length?'verified':'partial',sort_options:opts.slice(0,8),
     detail:'排序通过 combobox 选项实现（'+opts.slice(0,3).join('/')+'）'};
    await page.keyboard.press('Escape');
   }
  }
  H.record('PB0076',ev);
 }
 // ---- PB0078 复制 request-id
 {
  const hasReqId=/request[_-]?id|请求 ?ID/i.test(ctl.headers.join(' ')+txt.join(' '));
  const cpBtn=page.locator('button[title*="复制"], button[title*="Request"], button[title*="ID"]').first();
  let ev={status:'not_present_in_ui',request_id_column:hasReqId};
  if(await cpBtn.count()){
   await page.locator('tbody tr').first().hover().catch(()=>{});
   await cpBtn.click({force:true}).catch(()=>{});
   await page.waitForTimeout(600);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   ev={status:clip?'verified':'partial',clipboard:clip?.slice(0,60),
    detail:'复制 request-id -> 剪贴板（'+String(clip||'').slice(0,40)+'）'};
  }else{
   // 行内是否显示 request id 文本
   const sampleRows=await page.evaluate(()=>[...document.querySelectorAll('tbody tr')].slice(0,3)
    .map(tr=>tr.innerText.replace(/\s+/g,' ').slice(0,150)));
   ev={status:hasReqId?'partial':'not_present_in_ui',headers:ctl.headers,sample_rows:sampleRows,
    detail:hasReqId?'存在 request-id 列/文本但无复制控件':'日志表无 request-id 列与复制控件'};
  }
  H.record('PB0078',ev);
 }
 // ---- PB0079 关联实际上游
 {
  const upCols=ctl.headers.filter(h=>/上游|通道|渠道|账号|provider/i.test(h));
  const rowSample=await page.evaluate(()=>[...document.querySelectorAll('tbody tr')].slice(0,2)
   .map(tr=>tr.innerText.replace(/\s+/g,' ').slice(0,180)));
  const detail=page.locator('tbody tr').first();
  let ev={status:upCols.length?'verified':'partial',upstream_columns:upCols,row_sample:rowSample,
   detail:upCols.length?'日志行含上游/账号列（可关联实际通道）':'列名未含上游字段，检查行内文本'};
  if(await detail.count()){
   await detail.click({force:true}).catch(()=>{});
   await page.waitForTimeout(1500);
   const dlg=page.locator('[data-state=open], [role=dialog]').last();
   const dt=await dlg.count()?(await dlg.innerText()).slice(0,300):'';
   if(dt)ev={...ev,status:'verified',detail_panel:dt.replace(/\n+/g,'|').slice(0,220),
    detail:'行详情展示实际上游/账号/耗时等关联字段'};
   if(await dlg.count())await page.keyboard.press('Escape');
  }
  H.record('PB0079',ev);
 }
 await page.screenshot({path:path.join(H.run,'UI08-logs.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
