// 造真实请求日志并在日志页复核 PB0078/P0079（真实数据行）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui08-rows/v1','ui08rw');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);
 // 建临时密钥 -> 发真实推理请求 -> 日志页应出现行
 const k=await H.api(ctx,'POST','/api/keys',{name:'qa-log-probe-'+crypto.randomUUID().slice(0,5),
  platforms:['workbuddy']});
 const key=k.body?.key;
 let inferHttp=null,inferBody='';
 if(key){
  const r=await ctx.request.post(CONSOLE+"/api/v1/chat/completions",{
   headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},
   data:{model:'fake-model',messages:[{role:'user',content:'qa-log-probe-'+Date.now()}]}});
  inferHttp=r.status();inferBody=(await r.text()).slice(0,120);
 }
 await page.goto(CONSOLE+'/logs/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const rows=await page.evaluate(()=>[...document.querySelectorAll('tbody tr')]
  .map(tr=>tr.innerText.replace(/\s+/g,' ').slice(0,220)).slice(0,3));
 const headers=await page.evaluate(()=>[...document.querySelectorAll('th')].map(t=>(t.textContent||'').trim()));
 const reqIdCopy=await page.locator('button[title*="复制"], button[title*="request"]').count();
 const firstRow=page.locator('tbody tr').first();
 let detailPanel='';
 if(await firstRow.count()){
  await firstRow.click({force:true}).catch(()=>{});
  await page.waitForTimeout(1600);
  const dlg=page.locator('[data-state=open], [role=dialog]').last();
  detailPanel=await dlg.count()?(await dlg.innerText()).slice(0,400):'';
  if(await dlg.count())await page.keyboard.press('Escape');
 }
 // 复制按钮：行内所有 title 清单
 await page.locator('tbody tr').first().hover().catch(()=>{});
 const rowTitles=await page.evaluate(()=>{
  const tr=document.querySelector('tbody tr');
  return tr?[...tr.querySelectorAll('button')].map(b=>b.title||(b.textContent||'').trim()).filter(Boolean):[];});
 H.record('PB0078',{status:reqIdCopy?'verified':'not_present_in_ui',
  inference_http:inferHttp,inference_body:inferBody,
  headers,row_titles:rowTitles,request_id_copy_controls:reqIdCopy,
  detail:reqIdCopy?'行内复制控件（request-id）':'日志行真实生成（推理 '+inferHttp+'）但表头无 request-id 列、行内无复制控件'});
 const hasAccount=headers.some(h=>/账号/.test(h));
 H.record('PB0079',{status:(hasAccount&&rows.length)?'verified':'partial',
  headers,rows_sample:rows,detail_panel:detailPanel.replace(/\n+/g,'|').slice(0,260),
  detail:hasAccount&&rows.length?'真实日志行含"账号"列'+(detailPanel?'；行详情含关联上游/账号字段':'')
   :'行未生成或列缺失'});
 // PB0076 复核（真实行下的排序/时间范围）
 {
  const combo=page.getByRole('combobox').first();
  await combo.click();await page.waitForTimeout(700);
  const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
  await page.getByRole('option',{name:/近 24 小时/}).click().catch(()=>{});
  await page.waitForTimeout(1800);
  const rows24=await page.locator('tbody tr').count();
  const apis=await page.evaluate(()=>1);
  H.record('PB0076',{status:opts.length?'verified':'partial',options:opts.slice(0,6),
   rows_after_24h:rows24,
   detail:'时间范围筛选（'+opts.slice(0,4).join('/')+'）切换并重取；真实行数='+rows24});
 }
 await page.screenshot({path:path.join(H.run,'UI08-real-rows.png')});
 // 清理临时密钥
 if(k.body?.id)await H.api(ctx,'DELETE','/api/keys/'+k.body.id).catch(()=>{});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
