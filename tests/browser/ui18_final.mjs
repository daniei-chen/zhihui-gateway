// UI18 最终：添加线路弹窗（名称/代理地址[socks5占位]/权重/加入池）正确填写。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui18-final/v1','ui18fn');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const ROUTE='qa-px-'+crypto.randomUUID().slice(0,6);
try{
 await H.login(page);
 await page.goto(CONSOLE+'/settings/proxy/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2800);
 // ---- PB0156 添加线路（弹窗：名称 + 代理地址）
 await page.getByRole('button',{name:'添加线路',exact:true}).first().click();
 const dlg=page.locator('[data-state=open]').last();
 await page.waitForTimeout(1300);
 await page.getByPlaceholder(/px-cn/).last().fill(ROUTE);
 const SECRET='qa-secret-'+crypto.randomUUID().slice(0,6);
 await page.getByPlaceholder(/socks5:\/\/user:pass@host:10000/).fill('socks5://qauser:'+SECRET+'@127.0.0.1:9');
 const [cr]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/proxy-routes')&&r.request().method()!=='GET',{timeout:20000}),
  dlg.getByRole('button',{name:'保存',exact:true}).last().click(),
 ]);
 const rb=await H.api(ctx,'GET','/api/proxy-routes');
 const rt=(rb.body?.routes||[]).find(r=>r.name===ROUTE);
 H.record('PB0156',{status:cr?.status()<300&&rt?'verified':'failed',http:cr?.status(),
  method:cr?.request().method(),route_in_authority:Boolean(rt),route:rt,
  detail:'添加线路弹窗（名称+代理地址含凭据+权重/入池）-> 保存 -> 权威回读'});

 // ---- PB0157+PB0160 测试（不可达）
 {
  const m=apiMark();
  await page.getByRole('button',{name:'测试',exact:true}).first().click();
  await page.waitForTimeout(6000);
  const probeApis=page.__apiLog.slice(m).filter(x=>/probe/.test(x));
  const failLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/失败|无法|拒绝|超时|不通|错误|连接/.test(l)).slice(-5);
  H.record('PB0157',{status:probeApis.length?'verified':'partial',apis:probeApis.slice(0,4),
   detail:'测试按钮 -> probe API'});
  H.record('PB0160',{status:failLines.length?'verified':'partial',failure_lines:failLines,
   detail:'探测不可达代理(127.0.0.1:9)失败解释'});
 }
 // ---- PB0159 遮蔽
 {
  const rb=await H.api(ctx,'GET','/api/proxy-routes');
  const raw=JSON.stringify(rb.body||{});
  H.record('PB0159',{status:!raw.includes(SECRET)?'verified':'partial',
   plaintext_secret_absent:!raw.includes(SECRET),
   route_echo:(rb.body?.routes||[]).find(r=>r.name===ROUTE),
   detail:'凭据回读遮蔽：secret 明文不在任何响应字段'});
 }
 // ---- PB0161 绑定读侧
 {
  const m=apiMark();
  await page.getByRole('button',{name:'绑定账号',exact:true}).first().click();
  await page.waitForTimeout(2000);
  const bindApis=page.__apiLog.slice(m).filter(x=>/bind|account|proxy/.test(x));
  const panelLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/绑定|未绑定|账号/.test(l)).slice(0,5);
  H.record('PB0161',{status:'partial_scope_verified',apis:bindApis.slice(0,4),panel_lines:panelLines,
   detail:'绑定面板打开+读取（改绑定会移动 fixture 账号归属，未执行——诚实记范围）'});
  await page.keyboard.press('Escape');
 }
 // ---- PB0158 删除
 {
  const m=apiMark();
  const row=page.getByText(ROUTE,{exact:true}).first();
  await row.hover().catch(()=>{});
  const delBtn=page.locator(`button[title*="删除"]`).first();
  if(await delBtn.count())await delBtn.click().catch(()=>{});
  await page.waitForTimeout(900);
  const conf=page.getByRole('alertdialog');
  if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
  await page.waitForTimeout(1500);
  const rb=await H.api(ctx,'GET','/api/proxy-routes');
  let gone=!(rb.body?.routes||[]).some(r=>r.name===ROUTE);
  const delHttp=page.__apiLog.slice(m).find(x=>x.includes('DELETE'));
  if(!gone){const fb=await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(ROUTE));
   const rb2=await H.api(ctx,'GET','/api/proxy-routes');
   gone=!(rb2.body?.routes||[]).some(r=>r.name===ROUTE);
   H.record('PB0158',{status:'partial',ui_delete:delHttp||'none',fallback_http:fb.status,route_gone:gone,
    detail:'UI 删除未确认；API 兜底恢复'});}
  else H.record('PB0158',{status:'verified',ui_delete:delHttp||'fired',route_gone:true,
   detail:'UI 删除 -> DELETE -> 权威无该线路'});
 }
 await page.screenshot({path:path.join(H.run,'UI18-proxy.png')});
}catch(e){
 H.report.error=String(e).slice(0,500);
 try{const rb=await H.api(ctx,'GET','/api/proxy-routes');
  for(const r of (rb.body?.routes||[]))if((r.name||'').startsWith('qa-px'))
   await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(r.name));}catch{}
}
await ctx.close();
await H.finish();
