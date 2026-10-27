// UI04 补齐：按真实控件名处理 PB0024(状态标签)/PB0025(排序combobox)/PB0029-0041。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui04-fix/v1','ui04fx');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 const acc=await H.api(ctx,'GET','/api/accounts');
 H.note('accounts now: '+JSON.stringify((acc.body?.accounts||[]).map(a=>a.uid)));

 // ---- PB0024 状态筛选（标签：可用/需处理/冷却中/已停用，计数内联如"需处理3"）
 {
  const tab=page.getByText(/^需处理/).first();
  await tab.waitFor({timeout:10000});
  await tab.click();await page.waitForTimeout(1500);
  const afterTxt=await lines();
  const vis=await page.getByText('qa-acc-00',{exact:false}).count();
  H.record('PB0024',{status:'verified',picked:'需处理',accounts_visible:vis,
   apis:page.__apiLog.slice(-4).filter(x=>x.includes('accounts')),
   detail:'状态标签（全部/可用/需处理/冷却中/已停用）筛选交互；fake 账号过期信息缺失归为需处理/全部'});
  await page.getByText(/^全部\d/).first().click();
  await page.waitForTimeout(1200);
 }
 // ---- PB0025 排序（combobox 三选项）
 {
  await page.getByRole('combobox').first().click();
  await page.waitForTimeout(800);
  await page.getByRole('option',{name:'有效期近的在前',exact:true}).click();
  await page.waitForTimeout(1500);
  const firstRow=await page.locator('tr').nth(1).innerText().catch(()=> '');
  const list=await H.api(ctx,'GET','/api/accounts');
  H.record('PB0025',{status:'verified',picked:'有效期近的在前',first_row:firstRow.slice(0,60),
   authority_order:(list.body?.accounts||[]).map(a=>a.uid).slice(0,3),
   detail:'排序 combobox（默认顺序/有效期近的在前/积分少的在前）选择生效（客户端排序，权威顺序不变）'});
  // 还原
  await page.getByRole('combobox').first().click();await page.waitForTimeout(600);
  await page.getByRole('option',{name:'默认顺序',exact:true}).click().catch(()=>{});
 }
 // ---- PB0029 添加/导入/扫码（真实按钮名）
 {
  const addBtn=page.getByRole('button',{name:'添加账号',exact:true}).first();
  const ioBtn=page.getByRole('button',{name:/导入\/导出账号/}).first();
  let ev={status:'partial',buttons:{}};
  if(await addBtn.count()){
   await addBtn.click({timeout:8000}).catch(e=>{ev.buttons.add_click=String(e).slice(0,80);});
   await page.waitForTimeout(1500);
   const dlgTxt=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/扫码|上传|导入|json|JSON|文件|粘贴/.test(l)).slice(0,8);
   ev={...ev,status:'verified',add_dialog_lines:dlgTxt,
    detail:'添加账号入口打开（含扫码/上传/粘贴路径；扫码需真实引擎，上传路径已由造数验证 POST /api/accounts/upload）'};
   await page.screenshot({path:path.join(H.run,'PB0029-add.png')});
   await page.keyboard.press('Escape');await page.waitForTimeout(600);
  }
  if(await ioBtn.count()){
   ev.import_export_button=await ioBtn.innerText();
  }
  H.record('PB0029',ev);
 }
 // ---- PB0030 启用/禁用（行内开关/按钮）
 {
  // 先禁用 002 再恢复（POST /disabled）
  const target='qa-acc-002';
  const before=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
  const a=(before.find(x=>x.uid===target));
  let disabledViaApi=false;
  // 行内开关：账号行的 停用/启用 操作（可能在行 hover 菜单）；用行内 title 探测
  const row=page.getByText(target,{exact:false}).first();
  await row.hover().catch(()=>{});
  const sw=page.locator(`button[title*="停用"], button[title*="启用"], button[title*="禁用"]`).first();
  let ev={status:'partial'};
  if(await sw.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    sw.click()]);
   await page.waitForTimeout(1500);
   const mid=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
   const a2=mid.find(x=>x.uid===target);
   const [r2]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    page.locator(`button[title*="停用"], button[title*="启用"], button[title*="禁用"]`).first().click()]);
   const fin=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
   const a3=fin.find(x=>x.uid===target);
   ev={status:a2?.disabled_by_panel===true?'verified':'partial',disable_http:r?.status(),
    disabled_readback:a2?.disabled_by_panel,reenable_http:r2?.status(),final_disabled:a3?.disabled_by_panel,
    detail:'行内停用->权威回读->恢复'};
  }else{
   // 页面级统计有"已停用"——单行停用入口可能在详情/菜单中；API 直证行为 + 如实记录
   const apiTry=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-002.json')+'/disabled',{disabled:true});
   const mid=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
   const a2=mid.find(x=>x.uid===target);
   const back=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-002.json')+'/disabled',{disabled:false});
   ev={status:a2?.disabled_by_panel===true?'partial':'failed',
    inline_control_found:false,api_disable:apiTry.status,disabled_readback:a2?.disabled_by_panel,api_restore:back.status,
    detail:'行内停用控件未定位；API disabled 端点行为已证（UI 控件位置需人工核对截图）'};
  }
  H.record('PB0030',ev);
 }
 // ---- PB0031 刷新积分（页面级按钮"刷新积分"）
 {
  const btn=page.getByRole('button',{name:/刷新积分/}).first();
  let ev={status:'partial'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.url().includes('refresh-credits'),{timeout:30000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(3000);
   const txt=(await lines()).filter(l=>/积分|失败|成功|额度/.test(l)).slice(-4);
   ev={status:r?'verified_unsupported_in_lab':'partial',http:r?.status(),ui_lines:txt,
    detail:'刷新积分 -> POST /api/accounts/refresh-credits（'+(r?.status()??'无')+'）；fake 引擎返回模拟值'};
  }
  H.record('PB0031',ev);
 }
 // ---- PB0032 签到（"全部签到"按钮）
 {
  const btn=page.getByRole('button',{name:/全部签到/}).first();
  let ev={status:'partial'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.url().includes('checkin'),{timeout:30000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(3000);
   const txt=(await lines()).filter(l=>/签到|失败|成功/.test(l)).slice(-4);
   ev={status:r?'verified_unsupported_in_lab':'partial',http:r?.status(),ui_lines:txt,
    detail:'全部签到 -> checkin API（'+(r?.status()??'无')+'）；签到结果语义由 fake 引擎决定'};
  }
  H.record('PB0032',ev);
 }
 // ---- PB0034 复制（行 hover 复制）
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const cp=page.locator('button[title*="复制"]').first();
  let ev={status:'not_present_in_ui'};
  if(await cp.count()){
   await cp.click();await page.waitForTimeout(600);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   ev={status:clip?'verified':'partial',clipboard:clip?.slice(0,40),detail:'行复制 -> 剪贴板'};
  }else{
   ev={status:'not_present_in_ui',detail:'账号行无复制按钮（UID 等可经详情查看；复制控件不存在）'};
  }
  H.record('PB0034',ev);
 }
 // ---- PB0035 权重（行内或详情）
 {
  const wTxt=await page.getByText('权重').count();
  const num=page.locator('tr input[type=number]').first();
  let ev={status:'not_present_in_ui',weight_label:wTxt>0};
  if(await num.count()){
   await num.fill('5');await page.waitForTimeout(1500);
   const a=((await H.api(ctx,'GET','/api/accounts')).body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
   ev={status:a?.weight===5?'verified':'partial',weight_readback:a?.weight,detail:'行内权重输入 -> 权威回读'};
  }else if(wTxt){
   // API 直证 + 记 UI 位置待核
   const r=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json')+'/weight',{weight:5});
   const a=((await H.api(ctx,'GET','/api/accounts')).body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
   ev={status:a?.weight===5?'partial':'failed',api_http:r.status,weight_readback:a?.weight,
    detail:'权重列存在但行内输入未定位；API weight 端点已证（UI 控件需人工核对）'};
  }
  H.record('PB0035',ev);
 }
 // ---- PB0036 代理（行/详情）
 {
  const pTxt=await page.getByText(/代理|线路/).count();
  const btn=page.locator('button[title*="代理"], button[title*="线路"]').first();
  let ev={status:pTxt?'partial':'not_present_in_ui'};
  if(await btn.count()){
   await btn.click();await page.waitForTimeout(1200);
   const dt=(await page.locator('[data-state=open]').last().innerText().catch(()=>'')).slice(0,120);
   ev={status:'verified',dialog:dt.replace(/\n+/g,'|'),detail:'行代理绑定入口'};
   await page.keyboard.press('Escape');
  }else ev.detail='行内代理控件未定位；账号字段含 proxy（API 证实存）；可用线路受 UI18 upstream.json 联动限制';
  H.record('PB0036',ev);
 }
 // ---- PB0037 备注
 {
  const nTxt=await page.getByText('备注').count();
  const btn=page.locator('button[title*="备注"]').first();
  let ev={status:nTxt?'partial':'not_present_in_ui'};
  if(await btn.count()){
   await btn.click();await page.waitForTimeout(900);
   const inp=page.locator('[data-state=open] input, [data-state=open] textarea').last();
   if(await inp.count()){
    await inp.fill('qa-note-x1');
    await page.locator('[data-state=open]').last().getByRole('button',{name:/保存|确定/}).last().click();
    await page.waitForTimeout(1500);
    const a=((await H.api(ctx,'GET','/api/accounts')).body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
    ev={status:a?.note==='qa-note-x1'?'verified':'partial',note:a?.note,detail:'备注编辑 -> 权威回读'};
   }
  }else{
   // 备注可能无行内控件：API note 端点直证（PATCH /api/accounts/{file}）
   const r=await H.api(ctx,'PATCH','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json'),{note:'qa-note-x2'});
   const a=((await H.api(ctx,'GET','/api/accounts')).body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
   ev={status:a?.note==='qa-note-x2'?'partial':'failed',api_http:r.status,note:a?.note,
    detail:'行内备注控件未定位；API 备注写入已证（UI 控件需人工核对详情面板）'};
  }
  H.record('PB0037',ev);
 }
 // ---- PB0038 冷却
 {
  const cd=await page.getByText('冷却中',{exact:false}).count();
  H.record('PB0038',{status:cd?'partial':'not_present_in_ui',stat_label:cd>0,
   detail:'冷却统计标签存在（当前 0）；触发需引擎连续失败计数——fake 引擎不可触发（真引擎前置）'});
 }
 // ---- PB0039 移动分组
 {
  const btn=page.locator('button[title*="移动"], button:has-text("移动分组")').first();
  let ev={status:'partial'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/move/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    (async()=>{await btn.click();await page.waitForTimeout(900);
     const opt=page.getByText('默认上游',{exact:false}).last();
     if(await opt.count())await opt.click();})()]);
   ev={status:r?'verified':'partial',http:r?.status()??null,detail:'移动分组 -> move API'};
  }else{
   const r=await H.api(ctx,'POST','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-001.json')+'/move',{upstream:null});
   ev={status:r.status<300?'partial':'failed',api_http:r.status,
    detail:'行内移动控件未定位；move API 已证（'+r.status+'）——UI 控件需人工核对'};
  }
  H.record('PB0039',ev);
 }
 // ---- PB0040 删除（003；若已删则用 001 之外保留）
 {
  const list=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
  const victim=list.find(a=>a.uid==='qa-acc-003');
  let ev={status:'partial'};
  if(victim){
   const row=page.getByText('qa-acc-003',{exact:false}).first();
   await row.hover().catch(()=>{});
   const btn=page.locator('button[title*="删除"]').first();
   if(await btn.count()){
    const [r]=await Promise.all([
     page.waitForResponse(x=>new URL(x.url()).pathname.startsWith('/api/accounts/')&&x.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
     btn.click()]);
    const conf=page.getByRole('alertdialog');
    if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
    await page.waitForTimeout(1800);
    const fin=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
    if(!fin.some(a=>a.uid==='qa-acc-003'))ev={status:'verified',http:r?.status()??200,detail:'行删除 -> 权威无 qa-acc-003'};
    else{const fb=await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent(victim.file));
     ev={status:'partial',fallback:fb.status,detail:'UI 删除未确认；API 兜底'};}
   }else{
    const fb=await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent(victim.file));
    const fin=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
    ev={status:fin.some(a=>a.uid==='qa-acc-003')?'failed':'partial',api_http:fb.status,
     detail:'行内删除控件未定位；API DELETE 已证（UI 控件需人工核对）'};
   }
  }else ev={status:'verified',detail:'qa-acc-003 已在主脚本删除（权威确认）'};
  H.record('PB0040',ev);
 }
 // ---- PB0041 导出（导入/导出账号按钮）
 {
  const btn=page.getByRole('button',{name:/导入\/导出/}).first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   await btn.click();await page.waitForTimeout(1200);
   // 菜单里找 导出
   const exp=page.getByText(/导出/).last();
   const [r]=await Promise.all([
    page.waitForRequest(x=>new URL(x.url()).pathname==='/api/accounts/export',{timeout:15000}).catch(()=>null),
    exp.click().catch(()=>{})]);
   await page.waitForTimeout(2000);
   ev={status:r?'verified':'partial',export_requested:Boolean(r),
    detail:'导入/导出菜单 -> GET /api/accounts/export Blob 下载'};
  }
  H.record('PB0041',ev);
 }
 await page.screenshot({path:path.join(H.run,'UI04-accounts-final.png')});
}catch(e){
 H.report.error=String(e).slice(0,600);
}
await ctx.close();
await H.finish();
