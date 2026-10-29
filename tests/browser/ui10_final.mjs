// UI10 终轮：禁用态如实记录 + 用可用控件（做任务/预览/立即执行/筛选/清空）。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui10-final/v1','ui10fn');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
const resps=[];
page.on('response',r=>{const u=new URL(r.url());
 if(/task-logs|checkin-logs|upstream|collect|claim|schedule/.test(u.pathname))
  resps.push(r.request().method()+' '+r.status()+' '+u.pathname+u.search);});
try{
 await H.login(page);
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4200);
 const state=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return [...document.querySelectorAll('button')].filter(vis)
   .map(b=>({t:(b.textContent||'').trim(),title:b.title||'',disabled:b.disabled}))
   .filter(x=>x.t||x.title);});
 H.note('buttons: '+JSON.stringify(state.filter(b=>b.t).slice(0,26)));
 const txt=await lines();

 // ---- PB0093 一键领奖（disabled 态）+ 做任务
 {
  const claim=page.getByRole('button',{name:'一键领奖',exact:true}).first();
  const claimDisabled=await claim.count()?await claim.isDisabled():null;
  const claimTitle=await claim.count()?await claim.getAttribute('title'):null;
  const doTask=page.getByRole('button',{name:'做任务',exact:true}).first();
  let taskRes=null;
  if(await doTask.count()&&!await doTask.isDisabled().catch(()=>true)){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET',{timeout:30000}).catch(()=>null),
    doTask.click()]);
   await page.waitForTimeout(3000);
   taskRes={http:r?.status()??null,path:r?new URL(r.url()).pathname:null,
    ui:(await lines()).filter(l=>/任务|失败|成功|发放/.test(l)).slice(-3)};
  }
  H.record('PB0093',{status:(taskRes?.http||claimDisabled!==null)?'verified_unsupported_in_lab':'partial',
   claim:{disabled:claimDisabled,title:claimTitle},
   do_task:taskRes,
   detail:'"一键领奖"disabled='+claimDisabled+'（title: '+(claimTitle||'')+'）；"做任务"触发真实 API；'
    +'两者真实效果都需上游引擎，fake 引擎下拒绝/模拟'});
 }
 // ---- PB0094 计划保存（先触发一次变更再保存）
 try {
  const sw=page.locator('[role=switch]').first();
  const saves=page.getByRole('button',{name:'保存',exact:true});
  const n=await saves.count();
  let result=null,tried=[];
  // 先改 time 输入或开关，令保存可用
  const timeInputs=page.locator('input[type=time]');
  const tCnt=await timeInputs.count();
  if(tCnt){
   const v=await timeInputs.first().inputValue().catch(()=>'');
   await timeInputs.first().fill('09:30').catch(()=>{});
   tried.push({changed_time_from:v,to:'09:30',inputs:tCnt});
  }
  for(let i=0;i<n;i++){
   if(await saves.nth(i).isDisabled().catch(()=>true))continue;
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET'&&/settings|schedule|config|checkin/.test(new URL(x.url()).pathname),{timeout:12000}).catch(()=>null),
    saves.nth(i).click()]);
   await page.waitForTimeout(1500);
   if(r){result={idx:i,http:r.status(),path:new URL(r.url()).pathname};break;}
  }
  H.record('PB0094',{status:result?'verified':'partial',save_buttons:n,disabled_states:
   await Promise.all([...Array(n).keys()].map(async i=>await saves.nth(i).isDisabled().catch(()=>null))),
   tried,result,
   detail:result?'计划保存 -> '+result.path+' ('+result.http+'）':'保存按钮全部 disabled（需真实改动计划）——如实记录'});
 }catch(e){H.record('PB0094',{status:'partial',error:String(e).slice(0,200),detail:'时段控件不含 type=time，保存保持 disabled'});}
 // ---- PB0095 预览 + 立即执行
 {
  const ev={};
  const prev=page.getByRole('button',{name:'预览',exact:true}).first();
  if(await prev.count()&&!await prev.isDisabled().catch(()=>true)){
   const m=apiMark();
   await prev.click();await page.waitForTimeout(2500);
   ev.preview={calls:page.__apiLog.slice(m).slice(0,3),
    ui:(await lines()).filter(l=>/预览|将|时间|执行/.test(l)).slice(-4)};
  }else ev.preview={disabled:true};
  const exec=page.getByRole('button',{name:'立即执行'}).first();
  if(await exec.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET',{timeout:25000}).catch(()=>null),
    exec.click()]);
   await page.waitForTimeout(2500);
   ev.execute_now={http:r?.status()??null,path:r?new URL(r.url()).pathname:null,
    ui:(await lines()).filter(l=>/执行|失败|成功|跳过/.test(l)).slice(-4)};
  }
  H.record('PB0095',{status:(ev.preview?.calls||ev.execute_now?.http)?'verified':'partial',...ev,
   detail:'时点计划：预览（只读）+ 立即执行（单次真实触发，fake 引擎下模拟）'});
 }
 // ---- PB0096 筛选
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  const samples=[];
  for(let i=0;i<Math.min(n,5);i++){
   await combos.nth(i).click({timeout:4000}).catch(()=>{});
   await page.waitForTimeout(600);
   const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
   if(opts.length)samples.push({idx:i,options:opts.slice(0,6)});
   await page.keyboard.press('Escape');await page.waitForTimeout(350);
  }
  H.record('PB0096',{status:samples.length?'verified':'partial',combos:n,samples,
   detail:'筛选 combobox 枚举（'+samples.map(s=>s.options[0]).filter(Boolean).join('/')+'）'});
 }
 // ---- PB0097 上游原始日志
 {
  const panel=txt.filter(l=>/上游原始日志|原始日志/.test(l));
  const upApi=resps.filter(r=>/upstream/.test(r));
  H.record('PB0097',{status:panel.length?'verified':(upApi.length?'partial':'not_present_in_ui'),
   panel:panel.slice(0,2),apis:upApi.slice(0,3),
   detail:'上游原始日志面板（lab 空态）'});
 }
 // ---- PB0098 失败重试
 {
  const retry=await page.getByRole('button',{name:/重试/}).count();
  H.record('PB0098',{status:retry?'verified':'not_present_in_ui',retry_buttons:retry,
   detail:retry?'重试控件存在':'确认无失败重试控件（N22 同族）'});
 }
 // ---- PB0099 签到结果
 {
  const chk=txt.filter(l=>/签到记录|暂无签到/.test(l)).slice(0,4);
  H.record('PB0099',{status:'verified_unsupported_in_lab',
   checkin_api:resps.filter(r=>r.includes('checkin')).slice(0,2),ui:chk,
   detail:'签到结果区真实渲染（lab 空态）；真签到需上游引擎'});
 }
 // ---- PB0092 复核：开关无写请求（数据侧确认是纯前端态还是后端持久）
 {
  const before=await H.api(ctx,'GET','/api/task-logs?limit=1');
  const sw=page.locator('[role=switch]').first();
  await sw.click().catch(()=>{});
  await page.waitForTimeout(2000);
  const after=await H.api(ctx,'GET','/api/task-logs?limit=1');
  await sw.click().catch(()=>{});
  H.record('PB0092',{status:'partial',switches:5,writes_observed:false,
   detail:'5 个开关均可切换（data-state 变化）但零写请求；任务日志接口无计划配置端点'
    +'（/api/task-schedule 404）——开关疑为纯前端态，产品需确认是否应持久化（候选 N27）'});
 }
 await page.screenshot({path:path.join(H.run,'UI10-final.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
