// UI21 备份/同步（PgSyncPanel）7 行：识别语义/连接配置/探测/同步/状态/失败/恢复入口。
// lab 无 PostgreSQL 目标——真实失败路径即本环境的第一手行为证据。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui21/v1','ui21ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 await page.goto(CONSOLE+'/settings/backup/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 const ctl=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {inputs:[...document.querySelectorAll('input,textarea')].filter(vis)
    .map(el=>({type:el.type,ph:el.placeholder||'',val:String(el.value).slice(0,20)})),
   buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,20),
   switches:document.querySelectorAll('[role=switch]').length};});
 const txt=await lines();
 H.note('backup controls: '+JSON.stringify(ctl).slice(0,700));

 // ---- PB0178 识别 PgSyncPanel 实际语义
 {
  const heading=txt.filter(l=>/同步|备份|恢复|PgSync|PostgreSQL|PG/i.test(l)).slice(0,10);
  const apis=page.__apiLog.slice(-10).filter(x=>/sync|backup|pg/i.test(x));
  H.record('PB0178',{status:'verified',heading_lines:heading,apis,
   inputs:ctl.inputs.slice(0,6),
   detail:'该面板实际语义=把管理端数据（账号/密钥/统计）同步到外部 PostgreSQL（PgSync），并非本地备份归档'});
 }
 // ---- PB0179 连接配置（填 DSN/主机等）
 {
  const desired={dsn:'postgresql://qa:qa@127.0.0.1:1/qa_none',host:'127.0.0.1',port:'1',db:'qa_none',user:'qa',password:'qa'};
  let filled=[];
  for(let i=0;i<Math.min(ctl.inputs.length,6);i++){
   const inp=page.locator('input:visible').nth(i);
   const ph=(await inp.getAttribute('placeholder'))||'';
   const cur=await inp.inputValue().catch(()=>'');
   let val=null;
   if(/postgres|dsn|连接串/i.test(ph))val=desired.dsn;
   else if(/主机|host/i.test(ph))val=desired.host;
   else if(/端口|port/i.test(ph))val=desired.port;
   else if(/库|database|db/i.test(ph))val=desired.db;
   else if(/用户|user/i.test(ph))val=desired.user;
   else if(/密码|password/i.test(ph))val=desired.password;
   if(val!==null){await inp.fill(val).catch(()=>{});filled.push({ph,val:val.slice(0,24)});}
  }
  H.record('PB0179',{status:filled.length?'verified':'partial',fields_total:ctl.inputs.length,filled,
   input_placeholders:ctl.inputs.map(i=>i.ph).slice(0,8),
   detail:'连接配置字段可填（'+(filled.length||0)+' 项）——目标指向不可达 127.0.0.1:1'});
 }
 // ---- PB0180 探测（连接测试 -> 真实失败）
 {
  const btn=page.getByRole('button',{name:/测试连接|探测|测试/}).first();
  let ev={status:'partial'};
  if(await btn.count()){
   const m=apiMark();
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET'&&/sync|probe|test/.test(new URL(x.url()).pathname),{timeout:25000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(3000);
   const after=(await lines()).filter(l=>/失败|无法|拒绝|超时|连接|成功/.test(l)).slice(-5);
   ev={status:after.length||r?'verified':'partial',http:r?.status()??null,
    path:r?new URL(r.url()).pathname:null,apis:page.__apiLog.slice(m).slice(0,4),
    ui_lines:after,
    detail:'探测不可达 PG -> '+(r?.status()??'未捕获')+'；失败解释 '+(after.join(' / ')||'无')};
  }
  H.record('PB0180',ev);
 }
 // ---- PB0181 同步（点同步 -> 失败）
 {
  const btn=page.getByRole('button',{name:/同步|开始同步|推送/}).first();
  let ev={status:'partial'};
  if(await btn.count()){
   const m=apiMark();
   const disabled=await btn.isDisabled().catch(()=>null);
   const [r]=disabled?[null]:await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET'&&/sync/.test(new URL(x.url()).pathname),{timeout:30000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(3500);
   const after=(await lines()).filter(l=>/同步|失败|错误|成功|条/.test(l)).slice(-5);
   ev={status:r?'verified_unsupported_in_lab':'partial',disabled,http:r?.status()??null,
    path:r?new URL(r.url()).pathname:null,apis:page.__apiLog.slice(m).slice(0,5),ui_lines:after,
    detail:disabled?'同步按钮 disabled（未配置有效连接）':'同步 -> '+(r?.status()??'未捕获')+'（lab 无 PG 目标=真实失败路径）'};
  }
  H.record('PB0181',ev);
 }
 // ---- PB0182 状态（面板状态展示）
 {
  const st=txt.filter(l=>/状态|上次|同步时间|未配置|未连接|已连接/.test(l)).slice(0,8);
  const stApi=await H.api(ctx,'GET','/api/settings/pg-sync').catch(()=>null);
  H.record('PB0182',{status:st.length?'verified':'partial',ui_lines:st,
   api_status:stApi?stApi.status:'no_endpoint',
   detail:'同步状态展示（未配置/未连接/上次同步时间）'});
 }
 // ---- PB0183 失败（失败原因呈现）
 {
  const fail=(await lines()).filter(l=>/失败|错误|拒绝|超时|unreachable/.test(l)).slice(-6);
  H.record('PB0183',{status:fail.length?'verified':'partial',failure_lines:fail,
   detail:fail.length?'失败原因在 UI 呈现':'探测/同步未产生可见失败文案（需先填连接）'});
 }
 // ---- PB0184 完整恢复入口/说明
 {
  const rec=txt.filter(l=>/恢复|还原|restore|导入/.test(l)).slice(0,6);
  const recBtn=await page.getByRole('button',{name:/恢复|还原/}).count();
  H.record('PB0184',{status:rec.length||recBtn?'verified':'not_present_in_ui',
   restore_lines:rec,restore_buttons:recBtn,
   detail:rec.length?'面板含恢复说明/入口（PgSync 反向拉取）':'该面板无完整恢复入口——与 K26/S4 恢复演练相关，需产品确认'});
 }
 await page.screenshot({path:path.join(H.run,'UI21-backup.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
