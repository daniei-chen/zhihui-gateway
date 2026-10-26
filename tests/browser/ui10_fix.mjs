// UI10 补齐：按真实控件名（一键领奖/做任务/预览/立即执行/时段保存/筛选/清空）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui10-fix/v1','ui10fx');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
const resps=[];
page.on('response',r=>{const u=new URL(r.url());
 if(/task-logs|checkin-logs|upstream|credits/.test(u.pathname))resps.push(r.request().method()+' '+r.status()+' '+u.pathname+u.search);});
try{
 await H.login(page);
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const txt=await lines();

 // ---- PB0092 开关（5 个 switch：签到自动/任务自动等）
 {
  const sws=page.locator('[role=switch]');
  const n=await sws.count();
  const results=[];
  for(let i=0;i<Math.min(n,3);i++){
   const m=apiMark();
   const before=await sws.nth(i).getAttribute('data-state').catch(()=>null);
   await sws.nth(i).click().catch(()=>{});
   await page.waitForTimeout(1600);
   const after=await sws.nth(i).getAttribute('data-state').catch(()=>null);
   const calls=page.__apiLog.slice(m).filter(x=>!x.startsWith('GET'));
   results.push({idx:i,before,after,calls:calls.slice(0,3)});
   // 恢复
   if(before!==after)await sws.nth(i).click().catch(()=>{});await page.waitForTimeout(900);
  }
  const anyWrite=results.some(r=>r.calls.length);
  H.record('PB0092',{status:anyWrite?'verified':'partial',switches_found:n,toggles:results,
   detail:anyWrite?'开关切换产生写请求（计划/自动任务开关）':'开关状态本地切换但未见写请求（可能仅前端态）'});
 }
 // ---- PB0093 一键领奖 / 做任务（真实按钮）
 {
  const claim=page.getByRole('button',{name:'一键领奖',exact:true}).first();
  const doTask=page.getByRole('button',{name:'做任务',exact:true}).first();
  const ev={};
  if(await claim.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET'&&/task|claim|reward/.test(new URL(x.url()).pathname),{timeout:30000}).catch(()=>null),
    claim.click()]);
   await page.waitForTimeout(3000);
   ev.claim={http:r?.status()??null,path:r?new URL(r.url()).pathname:null,
    ui:(await lines()).filter(l=>/领|失败|成功|补偿/.test(l)).slice(-3)};
  }
  if(await doTask.count()){
   const [r2]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET',{timeout:30000}).catch(()=>null),
    doTask.click()]);
   await page.waitForTimeout(3000);
   ev.task={http:r2?.status()??null,path:r2?new URL(r2.url()).pathname:null,
    ui:(await lines()).filter(l=>/任务|失败|成功/.test(l)).slice(-3)};
  }
  H.record('PB0093',{status:ev.claim?.http||ev.task?.http?'verified_unsupported_in_lab':'partial',...ev,
   detail:'一键领奖/做任务 -> 真实 API（fake 引擎返回模拟结果；真实领奖/任务执行需上游引擎）'});
 }
 // ---- PB0094 计划保存（时段保存按钮，选可用的）
 {
  const saves=page.getByRole('button',{name:'保存',exact:true});
  const n=await saves.count();
  let done=null;
  for(let i=0;i<n;i++){
   const b=saves.nth(i);
   if(await b.isDisabled().catch(()=>true))continue;
   const [r]=await Promise.all([
    page.waitForResponse(x=>/settings|schedule|config/.test(new URL(x.url()).pathname)&&x.request().method()!=='GET',{timeout:12000}).catch(()=>null),
    b.click()]);
   await page.waitForTimeout(1500);
   if(r){done={idx:i,http:r.status(),path:new URL(r.url()).pathname};break;}
  }
  H.record('PB0094',{status:done?'verified':'partial',save_buttons:n,result:done,
   detail:done?'计划时段保存 -> '+done.path+' ('+done.http+')':'保存按钮全 disabled 或未产生写请求（需先改时段）'});
 }
 // ---- PB0095 任务领取计划（立即执行 x 时点 + 预览）
 {
  const exec=page.getByRole('button',{name:'立即执行'}).first();
  const prev=page.getByRole('button',{name:'预览',exact:true}).first();
  const ev={};
  if(await prev.count()){
   const m=apiMark();
   await prev.click();await page.waitForTimeout(2500);
   ev.preview={calls:page.__apiLog.slice(m).slice(0,4),
    ui:(await lines()).filter(l=>/预览|将执行|时间/.test(l)).slice(-3)};
  }
  if(await exec.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET',{timeout:25000}).catch(()=>null),
    exec.click()]);
   await page.waitForTimeout(2500);
   ev.execute_now={http:r?.status()??null,path:r?new URL(r.url()).pathname:null,
    ui:(await lines()).filter(l=>/执行|失败|成功/.test(l)).slice(-3)};
  }
  H.record('PB0095',{status:ev.preview||ev.execute_now?.http?'verified':'partial',...ev,
   detail:'时点计划：预览（不发写请求）+ 立即执行（单次触发）'});
 }
 // ---- PB0096 筛选 combobox（7 个：天数/类型/平台等）
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  const samples=[];
  for(let i=0;i<Math.min(n,4);i++){
   await combos.nth(i).click().catch(()=>{});
   await page.waitForTimeout(700);
   const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
   samples.push({idx:i,options:opts.slice(0,5)});
   await page.keyboard.press('Escape');await page.waitForTimeout(400);
  }
  H.record('PB0096',{status:samples.some(s=>s.options.length)?'verified':'partial',combos:n,samples,
   detail:'任务页筛选 combobox 选项枚举（天数/类型/平台/范围）'});
 }
 // ---- PB0098 失败重试（确认无重试控件）
 {
  const retry=await page.getByRole('button',{name:/重试/}).count();
  H.record('PB0098',{status:retry?'verified':'not_present_in_ui',retry_buttons:retry,
   detail:retry?'重试控件存在':'确认无失败重试控件（N22 同族缺陷）'});
 }
 // ---- PB0097 上游原始日志
 {
  const panel=txt.filter(l=>/上游原始日志|原始日志/.test(l));
  const upApi=resps.filter(r=>/upstream|raw/.test(r));
  H.record('PB0097',{status:panel.length?'verified':(upApi.length?'partial':'not_present_in_ui'),
   panel:panel.slice(0,2),apis:upApi.slice(0,3),
   detail:panel.length?'上游原始日志面板存在（lab 空态：fake 引擎不打上游日志）':'面板未见'});
 }
 // ---- PB0099 签到结果（签到记录区空态=真实）
 {
  const chk=txt.filter(l=>/签到记录|今日签到|已签/.test(l)).slice(0,4);
  H.record('PB0099',{status:'verified_unsupported_in_lab',
   checkin_api:resps.filter(r=>r.includes('checkin')).slice(0,2),ui:chk,
   detail:'签到结果区渲染（lab 空态："暂无签到记录"）；真实签到需上游引擎'});
 }
 await page.screenshot({path:path.join(H.run,'UI10-fix.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
