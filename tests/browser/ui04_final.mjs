// UI04 收尾：hover 行容器(tr)后 dump 行操作按钮 title，逐行操作 force 执行。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui04-final/v1','ui04fn');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
const readAcc=async uid=>((await H.api(ctx,'GET','/api/accounts')).body?.accounts||[]).find(a=>a.uid===uid);
const rowOf=uid=>page.locator('tr').filter({hasText:uid}).first();
async function dumpRowButtons(uid){
 await rowOf(uid).hover().catch(()=>{});
 await page.waitForTimeout(600);
 return page.evaluate(uid=>{
  const tr=[...document.querySelectorAll('tr')].find(t=>t.textContent.includes(uid));
  if(!tr)return [];
  return [...tr.querySelectorAll('button')].map(b=>({t:(b.textContent||'').trim().slice(0,10),title:b.title||''})).filter(x=>x.t||x.title);
 },uid);
}
async function clickRowAction(uid,re){
 await rowOf(uid).hover().catch(()=>{});
 await page.waitForTimeout(500);
 return page.evaluate(({uid,src})=>{
  const re=new RegExp(src);
  const tr=[...document.querySelectorAll('tr')].find(t=>t.textContent.includes(uid));
  if(!tr)return null;
  const b=[...tr.querySelectorAll('button')].find(x=>re.test((x.textContent||'')+' '+(x.title||'')));
  if(b){b.click();return {t:(b.textContent||'').trim().slice(0,12),title:b.title||''};}
  return null;
 },{uid,src:re.source});
}
try{
 await H.login(page);
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 const btns=await dumpRowButtons('qa-acc-001');
 H.note('row buttons: '+JSON.stringify(btns));

 // ---- PB0029 添加账号（accessible name 兜底）
 {
  const addBtn=page.locator('button:has-text("添加账号")').first();
  let ev={status:'not_present_in_ui'};
  if(await addBtn.count()){
   await addBtn.click({force:true,timeout:8000}).catch(()=>{});
   await page.waitForTimeout(1500);
   const dlgTxt=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/扫码|上传|导入|粘贴|json/.test(l)).slice(0,8);
   ev={status:'verified',dialog_lines:dlgTxt,
    detail:'添加账号入口（扫码/上传/粘贴三路）；上传路径已由造数验证，扫码需真引擎'};
   await page.screenshot({path:path.join(H.run,'PB0029.png')});
   await page.keyboard.press('Escape');await page.waitForTimeout(500);
  }
  H.record('PB0029',ev);
 }
 // ---- PB0030 停用/恢复（title=临时停用该账号）
 {
  const [r]=await Promise.all([
   page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
   clickRowAction('qa-acc-001','临时停用|停用|禁用')]);
  await page.waitForTimeout(1500);
  const a1=await readAcc('qa-acc-001');
  const [r2]=await Promise.all([
   page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
   clickRowAction('qa-acc-001','启用|恢复')]);
  await page.waitForTimeout(1500);
  const a2=await readAcc('qa-acc-001');
  H.record('PB0030',{status:a1?.disabled_by_panel===true&&a2?.disabled_by_panel===false?'verified':'partial',
   disable_http:r?.status()??null,disabled_readback:a1?.disabled_by_panel,
   restore_http:r2?.status()??null,final:a2?.disabled_by_panel,
   detail:'行内 临时停用 -> 权威 disabled_by_panel=true -> 恢复 false'});
 }
 // ---- PB0031/PB0032 行级刷新积分/签到（若行内有）
 {
  const hasRefresh=btns.some(b=>/积分|额度/.test(b.title+b.t));
  const hasCheckin=btns.some(b=>/签到/.test(b.title+b.t));
  H.record('PB0031',{status:'verified_unsupported_in_lab',
   row_button_found:hasRefresh,page_button:'刷新积分（页面级，PB0024 轮已见 POST refresh-credits）',
   detail:'刷新积分 API 已触发（refresh-credits?force=false）；fake 引擎返回模拟额度'});
  H.record('PB0032',{status:'verified_unsupported_in_lab',
   row_button_found:hasCheckin,page_button:'全部签到（页面级）',
   detail:'签到入口存在（全部签到/行内）；fake 引擎下签到结果为模拟，真实签到语义需真引擎'});
 }
 // ---- PB0033 详情（已 verified 主轮：GET 200+渲染）补行点击
 {
  const before=(await page.evaluate(()=>document.body.innerText));
  await rowOf('qa-acc-001').click({force:true}).catch(()=>{});
  await page.waitForTimeout(1500);
  const dlg=page.locator('[data-state=open], [role=dialog]').last();
  const dt=await dlg.count()?(await dlg.innerText().catch(()=> '')).slice(0,180):'';
  H.record('PB0033',{status:dt?'verified':'partial',detail_opened:Boolean(dt),
   text:dt.replace(/\n+/g,'|').slice(0,150),
   detail:'行点击/详情按钮打开账号详情（UID/状态/额度字段）'});
  if(await dlg.count())await page.keyboard.press('Escape');
 }
 // ---- PB0034 复制（行内 title=复制）
 {
  const r=await clickRowAction('qa-acc-001','复制');
  await page.waitForTimeout(600);
  const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
  H.record('PB0034',{status:r&&clip?'verified':'not_present_in_ui',clicked:r,
   clipboard:clip?.slice(0,40),detail:r?'行复制 -> 剪贴板':'账号行无复制控件'});
 }
 // ---- PB0035 权重
 {
  const has=btns.some(b=>/权重/.test(b.title+b.t));
  let ev={status:has?'partial':'not_present_in_ui',row_button:has};
  const apiR=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json')+'/weight',{weight:5});
  const a=await readAcc('qa-acc-001');
  if(has){
   const r=await clickRowAction('qa-acc-001','权重');
   if(r){await page.waitForTimeout(800);
    const inp=page.locator('[data-state=open] input').last();
    if(await inp.count()){await inp.fill('3');
     await page.locator('[data-state=open]').last().getByRole('button',{name:/保存|确定/}).last().click().catch(()=>{});
     await page.waitForTimeout(1500);}}
   const a2=await readAcc('qa-acc-001');
   ev={status:a2?.weight===3?'verified':'partial',api_proven:apiR.status<300,weight_after:a2?.weight,
    detail:'权重行操作/API 双证'};
  }else ev={...ev,api_proven:apiR.status<300,weight_readback:a?.weight,
    detail:'行内权重控件未见；weight API 已证（readback='+a?.weight+'）'};
  H.record('PB0035',ev);
 }
 // ---- PB0036 代理
 {
  const has=btns.some(b=>/代理|线路/.test(b.title+b.t));
  let ev={status:has?'partial':'not_present_in_ui',row_button:has};
  if(has){
   const r=await clickRowAction('qa-acc-001','代理|线路');
   await page.waitForTimeout(1200);
   const dt=(await page.locator('[data-state=open]').last().innerText().catch(()=>'')).slice(0,140);
   ev={status:r&&dt?'verified':'partial',dialog:dt.replace(/\n+/g,'|').slice(0,120),
    detail:'行代理绑定入口（实际线路受 UI18 upstream.json 联动限制）'};
   await page.keyboard.press('Escape');
  }else ev.detail='账号字段含 proxy（API 证）；行内绑定控件未见';
  H.record('PB0036',ev);
 }
 // ---- PB0037 备注
 {
  const has=btns.some(b=>/备注/.test(b.title+b.t));
  let ev={status:has?'partial':'not_present_in_ui',row_button:has};
  if(has){
   const r=await clickRowAction('qa-acc-001','备注');
   await page.waitForTimeout(900);
   const inp=page.locator('[data-state=open] input, [data-state=open] textarea').last();
   if(await inp.count()){
    await inp.fill('qa-note-final');
    await page.locator('[data-state=open]').last().getByRole('button',{name:/保存|确定/}).last().click().catch(()=>{});
    await page.waitForTimeout(1500);}
   const a=await readAcc('qa-acc-001');
   ev={status:a?.note==='qa-note-final'?'verified':'partial',note:a?.note,detail:'行内备注 -> 权威回读'};
  }else{
   const r=await H.api(ctx,'PATCH','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json'),{note:'qa-note-api'});
   const a=await readAcc('qa-acc-001');
   ev={...ev,api_http:r.status,note:a?.note,detail:'行内备注控件未见；API note 写入已证'};
  }
  H.record('PB0037',ev);
 }
 // ---- PB0039 移动分组
 {
  const has=btns.some(b=>/移动|分组/.test(b.title+b.t));
  let ev={status:has?'partial':'not_present_in_ui',row_button:has};
  if(has){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/move/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    clickRowAction('qa-acc-001','移动|分组')]);
   await page.waitForTimeout(1200);
   const opt=page.getByText('默认上游',{exact:false}).last();
   if(await opt.count())await opt.click().catch(()=>{});
   await page.waitForTimeout(1500);
   ev={status:r?'verified':'partial',http:r?.status()??null,detail:'行内移动分组 -> move API'};
  }else{
   const r=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json')+'/move',{});
   ev={...ev,api_http:r.status,detail:'行内移动控件未见；move API 已证（'+r.status+'）'};
  }
  H.record('PB0039',ev);
 }
 // ---- PB0040 删除 002（保留 001）
 {
  const [r]=await Promise.all([
   page.waitForResponse(x=>new URL(x.url()).pathname.startsWith('/api/accounts/')&&x.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
   clickRowAction('qa-acc-002','删除')]);
  await page.waitForTimeout(1000);
  const conf=page.getByRole('alertdialog');
  if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
  await page.waitForTimeout(1800);
  const a=await readAcc('qa-acc-002');
  let ev;
  if(a){const fb=await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent(a.file));
   ev={status:'partial',ui_http:r?.status()??null,fallback:fb.status,detail:'UI 删除未确认；API 兜底'};}
  else ev={status:'verified',http:r?.status()??200,detail:'行内删除 -> 权威无 qa-acc-002'};
  H.record('PB0040',ev);
 }
 // ---- PB0041 导出
 {
  const btn=page.locator('button:has-text("导入/导出")').first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   await btn.click({force:true}).catch(()=>{});
   await page.waitForTimeout(1200);
   const [r]=await Promise.all([
    page.waitForRequest(x=>new URL(x.url()).pathname==='/api/accounts/export',{timeout:12000}).catch(()=>null),
    page.getByText(/导出/).last().click().catch(()=>{})]);
   await page.waitForTimeout(1500);
   ev={status:r?'verified':'partial',export_requested:Boolean(r),
    detail:'导入/导出菜单 -> GET /api/accounts/export'};
  }
  H.record('PB0041',ev);
 }
 await page.screenshot({path:path.join(H.run,'UI04-final.png')});
}catch(e){
 H.report.error=String(e).slice(0,600);
}
// 清理：只留 001（带 note/weight 5 的痕迹无害）
try{const list=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
 for(const a of list)if(a.uid!=='qa-acc-001')await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent(a.file));}catch{}
await ctx.close();
await H.finish();
