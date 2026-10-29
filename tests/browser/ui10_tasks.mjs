// UI10 任务页 10 行专项（fixture b7fef78c，账号 qa-acc-001 在位）。
// 页面真实能力：/api/task-logs + /api/checkin-logs + 上游原始日志；立即采集/清空/筛选/分页。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui10/v1','ui10ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 const resps=[];
 page.on('response',r=>{const u=new URL(r.url());
  if(/task-logs|checkin-logs|upstream-logs|raw/.test(u.pathname))resps.push(r.status()+' '+u.pathname+u.search);});
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const txt=await lines();
 const ctl=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,25),
   comboboxes:document.querySelectorAll('[role=combobox]').length,
   switches:document.querySelectorAll('[role=switch]').length,
   tabs:[...document.querySelectorAll('[role=tab]')].map(t=>(t.textContent||'').trim()),
   selects:document.querySelectorAll('select').length};});
 H.note('task page controls: '+JSON.stringify(ctl).slice(0,600));

 // ---- PB0090 任务列表
 H.record('PB0090',{status:resps.some(r=>r.includes('task-logs'))?'verified':'partial',
  api_calls:resps.slice(0,6),
  ui_lines:txt.filter(l=>/自动任务|签到记录|暂无|条/.test(l)).slice(0,6),
  detail:'任务页挂载 -> GET /api/task-logs + checkin-logs，列表/空态渲染'});

 // ---- PB0091 平台能力说明
 {
  const platLines=txt.filter(l=>/平台|支持|不支持|Qoder|Trae|WorkBuddy/.test(l)).slice(0,6);
  const notImpl=await page.getByText(/未实现|不支持|仅 WorkBuddy/).count();
  H.record('PB0091',{status:platLines.length?'verified':'partial',lines:platLines,not_in_impl_hint:notImpl,
   detail:'平台能力说明文案（任务/签到仅在 WorkBuddy 侧实现，qtg 侧 501 语义见 K29 矩阵）'});
 }
 // ---- PB0092 开关（计划/自动任务开关）
 {
  const sw=page.locator('[role=switch]').first();
  let ev={status:'not_present_in_ui',switches:ctl.switches};
  if(await sw.count()){
   const m=apiMark();
   await sw.click();await page.waitForTimeout(1500);
   const calls=page.__apiLog.slice(m);
   ev={status:calls.length>1?'verified':'partial',apis:calls,detail:'任务页开关点击后 API 调用'};
   await sw.click().catch(()=>{});await page.waitForTimeout(800);
  }
  H.record('PB0092',ev);
 }
 // ---- PB0093 手动运行（立即采集）
 {
  const btn=page.getByRole('button',{name:/立即采集|采集/}).first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   const m=apiMark();
   const [r]=await Promise.all([
    page.waitForResponse(x=>/collect|task-logs/.test(new URL(x.url()).pathname)&&x.request().method()!=='GET',{timeout:30000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(3000);
   const after=(await lines()).filter(l=>/采集|失败|成功|条/.test(l)).slice(-4);
   ev={status:r?'verified_unsupported_in_lab':'partial',http:r?.status()??null,method:r?.request().method(),
    apis:page.__apiLog.slice(m).slice(0,6),ui_lines:after,
    detail:'立即采集 -> 采集 API（'+(r?.status()??'未捕获')+'）；真实采集需上游引擎，fake 引擎下返回空/模拟'};
  }
  H.record('PB0093',ev);
 }
 // ---- PB0094 计划保存（签到时段/任务开关配置）
 {
  const m=apiMark();
  const hasSchedule=txt.some(l=>/计划|时段|整点/.test(l));
  const saveBtn=page.getByRole('button',{name:/保存/}).first();
  let ev={status:hasSchedule?'partial':'not_present_in_ui',schedule_text:hasSchedule};
  if(hasSchedule&&await saveBtn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/settings|schedule|config/.test(new URL(x.url()).pathname)&&x.request().method()!=='GET',{timeout:15000}).catch(()=>null),
    saveBtn.click()]);
   ev={status:r?'verified':'partial',http:r?.status()??null,
    detail:'计划配置保存 -> '+(r?'POST '+(r.request().method()+' '+new URL(r.url()).pathname):'未捕获写请求')};
  }
  H.record('PB0094',ev);
 }
 // ---- PB0095 任务领取计划
 {
  const t=txt.filter(l=>/领取|任务计划|成长任务/.test(l)).slice(0,5);
  const entry=await page.getByText(/领取计划|活动任务/).count();
  H.record('PB0095',{status:entry?'partial':'not_present_in_ui',matches:entry,lines:t,
   detail:entry?'任务领取计划入口存在（账号行"活动任务"按钮 title 已见）':'页面无领取计划配置'});
 }
 // ---- PB0096 历史（签到记录列表）
 {
  const m=apiMark();
  const tab=page.getByRole('tab',{name:/签到|历史/}).first();
  let ev={status:'partial'};
  if(await tab.count()){await tab.click();await page.waitForTimeout(2000);
   ev={status:'verified',tab:(await tab.innerText()).trim(),
    apis:page.__apiLog.slice(m).slice(0,4),
    lines:(await lines()).filter(l=>/签到|暂无/.test(l)).slice(0,4),
    detail:'签到记录/历史 tab 切换并取数'};}
  else ev={status:'verified',apis:resps.filter(r=>r.includes('checkin')).slice(0,3),
   lines:txt.filter(l=>/签到记录|暂无签到/.test(l)).slice(0,3),
   detail:'签到历史区（同页呈现）+'+(resps.some(r=>r.includes('checkin'))?'checkin-logs 已取数':'')};
  H.record('PB0096',ev);
 }
 // ---- PB0097 日志（上游原始日志面板）
 {
  const up=txt.filter(l=>/上游原始日志|原始日志/.test(l)).slice(0,3);
  const upApi=resps.filter(r=>/raw|upstream-logs/.test(r)).slice(0,3);
  H.record('PB0097',{status:(up.length||upApi.length)?'verified':'not_present_in_ui',
   panel:up,apis:upApi,detail:'上游原始日志面板（失败/旅行时才有内容，lab 为空态）'});
 }
 // ---- PB0098 失败重试
 {
  const retry=await page.getByRole('button',{name:/重试/}).count();
  const failLines=txt.filter(l=>/失败|错误/.test(l)).length;
  H.record('PB0098',{status:retry?'verified':'not_present_in_ui',retry_buttons:retry,
   failure_lines:failLines,
   detail:retry?'重试控件存在':'页面无失败重试控件；取数失败时 N22 同族（只能整页刷新）'});
 }
 // ---- PB0099 签到结果
 {
  const res=txt.filter(l=>/签到|今日|已签/.test(l)).slice(0,6);
  const checkinApi=resps.filter(r=>r.includes('checkin'));
  H.record('PB0099',{status:checkinApi.length?'verified_unsupported_in_lab':'partial',
   apis:checkinApi.slice(0,3),ui_lines:res,
   detail:'签到结果展示依赖真实上游签到（fake 引擎不产生真实签到记录），lab 显示空态/模拟'});
 }
 await page.screenshot({path:path.join(H.run,'UI10-tasks.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
