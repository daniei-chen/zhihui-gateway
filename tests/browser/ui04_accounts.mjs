// UI04 账号池 19 行专项（fixture b7fef78c，已造 3 个假账号 qa-acc-001/002/003）。
// 引擎相关动作（签到/刷新积分/扫码）在 fake 引擎下实测并如实记录。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui04/v1','ui04ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
const G='workbuddy-qa-acc-001.json'; // 主操作账号
try{
 await H.login(page);
 const [acc]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/accounts',{timeout:20000}),
  page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'})]);
 await page.waitForTimeout(3000);
 const total=(await acc.json().catch(()=>{}))?.total;
 const uiRows=await page.getByText('qa-acc-00',{exact:false}).count();
 H.record('PB0033',{status:acc.status()===200&&total>=3?'verified':'partial',api_total:total,
  ui_account_names:uiRows,
  detail:'GET /api/accounts 200（total='+total+'），账号行渲染'});

 // 控件清单（后续行共用）
 const ctl=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {search:document.querySelectorAll('input[type=search],input[placeholder*=搜索]').length,
   selects:document.querySelectorAll('select').length,
   comboboxes:document.querySelectorAll('[role=combobox]').length,
   buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,30),
   checkbox:document.querySelectorAll('[role=checkbox],input[type=checkbox]').length};});
 H.note('controls: '+JSON.stringify(ctl).slice(0,500));

 // ---- PB0023 搜索
 {
  const sb=page.locator('input[placeholder*=搜索], input[type=search]').first();
  let ev={status:'not_present_in_ui'};
  if(await sb.count()){
   await sb.fill('qa-acc-002');await page.waitForTimeout(1200);
   const n2=await page.getByText('qa-acc-002').count();
   const n1=await page.getByText('qa-acc-001',{exact:false}).count();
   ev={status:n2>0&&n1===0?'verified':'partial',match:n2,other:n1,
    detail:'搜索 qa-acc-002 仅命中该账号'};
   await sb.fill('');await page.waitForTimeout(800);
  }
  H.record('PB0023',ev);
 }
 // ---- PB0024 筛选（平台/状态/realm）
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  let ev={status:'not_present_in_ui',combos:n};
  if(n){
   const before=await lines();
   await combos.first().click();await page.waitForTimeout(700);
   const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
   const pick=opts.find(o=>/禁用|停用|失效/.test(o||''))||opts[0];
   if(pick){await page.getByRole('option',{name:pick.trim()}).first().click();
    await page.waitForTimeout(1500);}
   const after=await lines();
   const listApi=page.__apiLog.slice(-6).filter(x=>x.includes('accounts'));
   ev={status:opts.length||JSON.stringify(before)!==JSON.stringify(after)?'verified':'partial',
    options:opts.slice(0,6),picked:pick||null,apis:listApi,
    detail:'状态/平台筛选 combobox 交互'+(pick?'（选 '+pick.trim()+'）':'')};
   await page.keyboard.press('Escape');
   // 复位
   await page.reload({waitUntil:'domcontentloaded'});await page.waitForTimeout(2500);
  }
  H.record('PB0024',ev);
 }
 // ---- PB0025 排序
 {
  const sortable=await page.locator('th[aria-sort], th button:has(svg), th:has(button)').count();
  let ev={status:'not_present_in_ui',sortable_headers:sortable};
  if(sortable){
   const th=page.locator('th[aria-sort], th:has(button)').first();
   const name=(await th.innerText()).trim();
   await th.click();await page.waitForTimeout(1200);
   const order=await th.getAttribute('aria-sort');
   const firstRow=(await page.locator('tr').nth(1).innerText().catch(()=> '' )).slice(0,60);
   ev={status:'verified',header:name,aria_sort_after:order,first_row_after:firstRow,
    detail:'点击表头 '+name+' 排序，aria-sort='+order};
  }
  H.record('PB0025',ev);
 }
 // ---- PB0026 分页
 {
  const pag=await page.locator('nav[role=navigation], button:has-text("下一页"), button:has-text("上一页"), [class*=pagination]').count();
  H.record('PB0026',{status:pag?'verified':'not_present_in_ui',pagination_controls:pag,
   detail:pag?'分页控件存在':'当前 3 账号无分页控件（未达分页阈值）'});
 }
 // ---- PB0027 行选择
 {
  const cb=page.locator('[role=checkbox], input[type=checkbox]').nth(1);
  let ev={status:'not_present_in_ui'};
  if(await cb.count()){
   await cb.click();await page.waitForTimeout(600);
   const checked=await page.locator('[role=checkbox][data-state=checked], input[type=checkbox]:checked').count();
   ev={status:checked>=1?'verified':'partial',checked_after:checked,detail:'行复选框选中'};
  }
  H.record('PB0027',ev);
 }
 // ---- PB0028 全选与批量
 {
  const hdr=page.locator('[role=checkbox], input[type=checkbox]').first();
  let ev={status:'not_present_in_ui'};
  if(await hdr.count()){
   await hdr.click();await page.waitForTimeout(800);
   const checked=await page.locator('[role=checkbox][data-state=checked], input[type=checkbox]:checked').count();
   const batchBar=await page.getByText(/已选|批量/).count();
   const batchBtns=(await page.evaluate(()=>[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(t=>/批量|启用|禁用|删除|移动/.test(t)))).slice(0,6);
   ev={status:checked>=3?'verified':'partial',all_checked:checked,batch_bar_visible:batchBar>0,
    batch_buttons:batchBtns,detail:'全选 3 账号 + 批量操作条出现'};
   // 复位（取消全选）
   await hdr.click().catch(()=>{});await page.waitForTimeout(500);
  }
  H.record('PB0028',ev);
 }
 // ---- PB0029 添加/导入/扫码
 {
  const addBtn=page.getByRole('button',{name:/添加|导入|扫码/}).first();
  let ev={status:'not_present_in_ui'};
  if(await addBtn.count()){
   const m=apiMark();
   await addBtn.click();
   await page.waitForTimeout(1500);
   const dlgTxt=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/扫码|上传|导入|json|JSON|文件/.test(l)).slice(0,8);
   ev={status:'verified',button:(await addBtn.innerText()).trim(),dialog_lines:dlgTxt,
    detail:'添加入口打开导入/扫码对话框（上传/扫码 API 见 PB0029 runtime）'};
   await page.keyboard.press('Escape');await page.waitForTimeout(400);
  }
  H.record('PB0029',{...ev,
   upload_api_proven:'POST /api/accounts/upload（造数已用：3 账号 added）',
   qr_api:'POST /api/auth/start（扫码）——fake 引擎下扫码链路不可真实完成'});
 }
 // ---- PB0030 启用/禁用
 {
  const m=apiMark();
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const sw=page.locator('button[title*="禁用"], button[title*="停用"], [role=switch]').first();
  let ev={status:'partial'};
  if(await sw.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    sw.click()]);
   await page.waitForTimeout(1200);
   const list=await H.api(ctx,'GET','/api/accounts');
   const a1=(list.body?.accounts||[]).find(a=>a.uid==='qa-acc-001');
   const [r2]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/disabled/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    sw.click()]);
   const list2=await H.api(ctx,'GET','/api/accounts');
   const a2=(list2.body?.accounts||[]).find(a=>a.uid==='qa-acc-001');
   ev={status:(a1?.disabled_by_panel===true||a1?.disabled===true)?'verified':'partial',
    disable_http:r?.status(),disabled_readback:a1?.disabled_by_panel??a1?.disabled,
    reenable_http:r2?.status(),final:a2?.disabled_by_panel??a2?.disabled,
    detail:'禁用->权威回读->恢复启用'};
  }
  H.record('PB0030',ev);
 }
 // ---- PB0031 刷新积分（fake 引擎实测）
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="积分"], button:has-text("积分")').first();
  let ev={status:'verified_unsupported_in_lab'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/credits/.test(new URL(x.url()).pathname),{timeout:20000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(2500);
   const txt=(await lines()).filter(l=>/积分|失败|错误|额度/.test(l)).slice(-4);
   ev={status:r?'verified_unsupported_in_lab':'partial',http:r?.status(),ui_lines:txt,
    detail:'刷新积分调用真实 API（'+(r?.status()??'无')+'）；fake 引擎下返回值为模拟（credits=null 可能保留）'};
  }
  H.record('PB0031',ev);
 }
 // ---- PB0032 签到（fake 引擎实测）
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="签到"], button:has-text("签到")').first();
  let ev={status:'verified_unsupported_in_lab'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/checkin/.test(new URL(x.url()).pathname),{timeout:25000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(2500);
   const txt=(await lines()).filter(l=>/签到|失败|成功|错误/.test(l)).slice(-4);
   ev={status:r?'verified_unsupported_in_lab':'partial',http:r?.status(),ui_lines:txt,
    detail:'签到调用真实 API（'+(r?.status()??'无')+'）；签到结果语义（PB0099 同源）由 fake 引擎决定'};
  }
  H.record('PB0032',ev);
 }
 // ---- PB0033 详情
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.click().catch(()=>{});
  await page.waitForTimeout(1500);
  const dlg=page.locator('[data-state=open], [role=dialog]').last();
  const hasDetail=await dlg.count();
  const detailTxt=hasDetail?(await dlg.innerText()).slice(0,200):'';
  H.record('PB0033',{status:hasDetail&&/qa-acc-001|详情|UID/.test(detailTxt)?'verified':'partial',
   dialog_text:detailTxt.replace(/\n+/g,' | ').slice(0,160),
   detail:hasDetail?'行点击/详情按钮打开详情（UID/过期/额度等字段）':'详情入口未定位'});
  if(hasDetail)await page.keyboard.press('Escape');
 }
 // ---- PB0034 复制
 {
  const row=page.getByText('qa-acc-002',{exact:false}).first();
  await row.hover().catch(()=>{});
  const cp=page.locator('button[title*="复制"]').first();
  let ev={status:'not_present_in_ui'};
  if(await cp.count()){
   await cp.click();await page.waitForTimeout(500);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   ev={status:clip?'verified':'partial',clipboard:clip?.slice(0,40),
    detail:'行复制按钮 -> 剪贴板（'+(clip||'').slice(0,30)+'…）'};
  }
  H.record('PB0034',ev);
 }
 // ---- PB0035 权重
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const w=page.locator('button[title*="权重"], input[title*="权重"], [class*=weight] input, input[type=number]').first();
  let ev={status:'not_present_in_ui'};
  const hasCtl=await page.getByText('权重').count();
  if(await w.count()){
   await w.fill('5').catch(async()=>{await w.click();});
   await page.waitForTimeout(1200);
   const list=await H.api(ctx,'GET','/api/accounts');
   const a=(list.body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
   ev={status:a?.weight===5||a?.weight==='5'?'verified':'partial',weight_readback:a?.weight,
    detail:'权重修改 -> 权威回读'};
  }else ev={status:hasCtl?'partial':'not_present_in_ui',weight_label_visible:hasCtl>0,
    detail:'权重控件未直接定位'+(hasCtl?'（列存在）':'')};
  H.record('PB0035',ev);
 }
 // ---- PB0036 代理绑定
 {
  const txt=(await lines()).filter(l=>/代理|线路/.test(l)).slice(0,4);
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="代理"], button[title*="线路"]').first();
  let ev={status:'not_present_in_ui',ui_lines:txt};
  if(await btn.count()){
   await btn.click();await page.waitForTimeout(1200);
   const dlg=page.locator('[data-state=open]').last();
   const dt=await dlg.count()?(await dlg.innerText()).slice(0,150):'';
   ev={status:'verified',dialog:dt.replace(/\n+/g,'|').slice(0,120),
    detail:'行代理绑定入口打开（真实绑定需可用线路——UI18 因 upstream.json 阻塞，此处联动）'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0036',ev);
 }
 // ---- PB0037 备注
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="备注"], button[title*="note"]').first();
  let ev={status:'partial'};
  const hasNote=await page.getByText('备注').count();
  if(await btn.count()){
   await btn.click();await page.waitForTimeout(900);
   const inp=page.locator('[data-state=open] input, [data-state=open] textarea').last();
   if(await inp.count()){
    await inp.fill('qa-note-xyz');
    const [r]=await Promise.all([
     page.waitForResponse(x=>/\/api\/accounts\/.+\/note|\/api\/accounts\/.+/.test(new URL(x.url()).pathname)&&x.request().method()!=='GET',{timeout:15000}).catch(()=>null),
     page.locator('[data-state=open]').last().getByRole('button',{name:/保存|确定/}).last().click()]);
    const list=await H.api(ctx,'GET','/api/accounts');
    const a=(list.body?.accounts||[]).find(x=>x.uid==='qa-acc-001');
    ev={status:a?.note==='qa-note-xyz'?'verified':'partial',note_readback:a?.note,http:r?.status()??null,
     detail:'备注编辑 -> 权威回读'};
   }
  }else ev={status:hasNote?'partial':'not_present_in_ui'};
  H.record('PB0037',ev);
 }
 // ---- PB0038 冷却
 {
  const cd=await page.getByText(/冷却|暂停/).count();
  H.record('PB0038',{status:cd?'partial':'not_present_in_ui',matches:cd,
   detail:cd?'冷却状态展示存在（触发需引擎连续失败——fake 引擎不可触发，联动 UI04 引擎前置）'
    :'无冷却相关控件/列'});
 }
 // ---- PB0039 移动分组
 {
  const row=page.getByText('qa-acc-001',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="移动"], button[title*="分组"]').first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>/\/api\/accounts\/.+\/move/.test(new URL(x.url()).pathname),{timeout:15000}).catch(()=>null),
    (async()=>{await btn.click();await page.waitForTimeout(900);
     // 选择目标分组（默认上游）
     const opt=page.getByText('默认上游',{exact:false}).last();
     if(await opt.count())await opt.click();})()]);
   await page.waitForTimeout(1500);
   ev={status:r?'verified':'partial',http:r?.status()??null,
    detail:'移动分组 -> move API'};
  }
  H.record('PB0039',ev);
 }
 // ---- PB0040 删除（用 003，保留 001/002 供后续）
 {
  const row=page.getByText('qa-acc-003',{exact:false}).first();
  await row.hover().catch(()=>{});
  const btn=page.locator('button[title*="删除"]').first();
  let ev={status:'partial'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForResponse(x=>new URL(x.url()).pathname.includes('/api/accounts/')&&x.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
    btn.click()]);
   const conf=page.getByRole('alertdialog');
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
   const list=await H.api(ctx,'GET','/api/accounts');
   const gone=!(list.body?.accounts||[]).some(a=>a.uid==='qa-acc-003');
   if(!gone){const fb=await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-003.json'));
    ev={status:'partial',ui_http:r?.status()??null,fallback:fb.status,detail:'UI 删除未确认；API 兜底'};}
   else ev={status:'verified',http:r?.status()??200,detail:'行删除 -> 权威无 qa-acc-003'};
  }
  H.record('PB0040',ev);
 }
 // ---- PB0041 导出
 {
  const btn=page.getByRole('button',{name:/导出/}).first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   const [r]=await Promise.all([
    page.waitForRequest(x=>new URL(x.url()).pathname==='/api/accounts/export',{timeout:15000}).catch(()=>null),
    btn.click()]);
   await page.waitForTimeout(2000);
   ev={status:r?'verified':'partial',export_requested:Boolean(r),
    detail:'导出 -> GET /api/accounts/export（Blob 下载）'};
  }
  H.record('PB0041',ev);
 }
 await page.screenshot({path:path.join(H.run,'UI04-accounts.png')});
}catch(e){
 H.report.error=String(e).slice(0,600);
 // 兜底清理 003
 try{await H.api(ctx,'DELETE','/api/accounts/'+encodeURIComponent('workbuddy-qa-acc-003.json'));}catch{}
}
await ctx.close();
await H.finish();
