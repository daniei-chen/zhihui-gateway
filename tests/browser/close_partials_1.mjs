// 27 行 partial 收口（第 1 批）：用更强的 DOM 探测补齐行内链路。
// 覆盖 PB0030(停用) PB0035(权重) PB0040(删除) PB0041(导出) PB0092(开关写请求)
//      PB0062(选模型) PB0063(思考强度) PB0072(错误文案) PB0135(测试连接)
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close1/v1','close1');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
// 精确的行定位器：按 tr 内含 uid 文本
const rowOf=uid=>page.locator('tr').filter({hasText:uid}).first();
/**
 * 行内按钮点击：**按 title 精确包含**匹配，且要求 title 命中才算。
 *
 * 早前用「button 的全部文本 + title」做正则，结果 `权重`、`删除` 这类短词会先
 * 命中「添加备注」按钮（它的 title 是"添加备注"，正则 `备注` 属另一条；但空文本
 * 按钮参与拼接后容易误配）。这里改成只在 title 上匹配，并返回实际命中的 title
 * 供证据核对——点错按钮会立刻在证据里暴露。
 */
async function clickRowBtn(uid,titlePattern){
 await rowOf(uid).hover().catch(()=>{});
 await page.waitForTimeout(500);
 return page.evaluate(({uid,src})=>{
  const re=new RegExp(src);
  const tr=[...document.querySelectorAll('tr')].find(t=>t.textContent.includes(uid));
  if(!tr)return {err:'row not found'};
  const btns=[...tr.querySelectorAll('button')];
  const match=btns.find(x=>re.test(x.title||''));
  if(!match)return {err:'no title match',
   available_titles:btns.map(x=>x.title||('text:'+(x.textContent||'').trim())).filter(Boolean)};
  match.click();
  return {clicked:{title:match.title}};
 },{uid,src:titlePattern.source||String(titlePattern)});
}
try{
 await H.login(page);
 // 账号预置工具（放在 try 外层作用域，供后续各段使用）
 const ensure = async (uid, token) => {
    const list=await H.api(ctx,'GET','/api/accounts');
    if((list.body?.accounts||[]).some(a=>a.uid===uid)) return 'exists';
    // playwright 的 multipart 在该端点会 500；改用浏览器侧 fetch + FormData
    const res = await page.evaluate(async ({payload, name}) => {
      const fd = new FormData();
      fd.append('files', new Blob([payload], {type: 'application/json'}), name);
      const r = await fetch('/api/accounts/upload?overwrite=true',
        {method:'POST', body: fd, credentials:'include'});
      return {status: r.status, body: (await r.text()).slice(0,200)};
    }, {payload: JSON.stringify({uid, accessToken: token}), name: `workbuddy-${uid}.json`});
    return res;
 };
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 H.note('ensure1 -> '+JSON.stringify(await ensure('qa-close-001','fake-at-close1')));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 const accs=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
 const uid=accs[0]?.uid;
 H.note('using account '+uid);

 // ---- PB0030 启用/禁用
 {
  let ev={status:'partial',detail:'no account'};
  if(uid){
   const before=(await H.api(ctx,'GET','/api/accounts')).body?.accounts?.find(a=>a.uid===uid);
   const click=await clickRowBtn(uid,/临时停用|^停用|^禁用/);
   await page.waitForTimeout(2000);
   // 可能有确认对话框
   const dlg=page.getByRole('alertdialog');
   if(await dlg.count()){
    await dlg.getByRole('button').filter({hasText:/确认|停用|确定/}).first().click().catch(()=>{});
    await page.waitForTimeout(2000);
   }
   const after=(await H.api(ctx,'GET','/api/accounts')).body?.accounts?.find(a=>a.uid===uid);
   const disabledNow=after?.disabled_by_panel===true||after?.disabled===true;
   // 恢复
   if(disabledNow){
    await clickRowBtn(uid,/^启用|^恢复/);
    await page.waitForTimeout(1500);
    const d2=page.getByRole('alertdialog');
    if(await d2.count())await d2.getByRole('button').filter({hasText:/确认|启用|确定/}).first().click().catch(()=>{});
    await page.waitForTimeout(1500);
   }
   const fin=(await H.api(ctx,'GET','/api/accounts')).body?.accounts?.find(a=>a.uid===uid);
   ev={status:disabledNow?'verified':'partial',click,disabled_readback:disabledNow,
    restored:fin?.disabled_by_panel===false||fin?.disabled===false,
    before:{disabled:before?.disabled_by_panel??before?.disabled},
    detail:disabledNow?'行内停用 -> 权威 disabled=true -> 恢复':'点击未触发停用（控件可能仅改本地态）'};
  }
  H.record('PB0030',ev);
 }
 // ---- PB0035 权重
 {
  let ev={status:'partial',detail:'no account'};
  if(uid){
   const click=await clickRowBtn(uid,/^权重/);
   await page.waitForTimeout(1200);
   const dlg=page.locator('[data-state=open]').last();
   const dlgText=await dlg.count()?(await dlg.innerText()).slice(0,200):'';
   const inp=page.locator('[data-state=open] input[type=number], [data-state=open] input').last();
   let newWeight=null;
   if(await inp.count()){
    await inp.fill('7');
    const [r]=await Promise.all([
     page.waitForResponse(x=>/\/weight/.test(new URL(x.url()).pathname)||(x.request().method()!=='GET'&&/accounts/.test(new URL(x.url()).pathname)),{timeout:15000}).catch(()=>null),
     dlg.getByRole('button',{name:/保存|确定/}).last().click().catch(()=>{})]);
    await page.waitForTimeout(1500);
    const a=(await H.api(ctx,'GET','/api/accounts')).body?.accounts?.find(x=>x.uid===uid);
    newWeight=a?.weight;
    ev={status:a?.weight===7?'verified':'partial',click,dialog:dlgText.replace(/\n+/g,'|').slice(0,120),
     http:r?.status()??null,weight_after:newWeight,
     detail:a?.weight===7?'行内权重弹层 -> 保存 -> 权威 weight=7':'填写后权威未变'};
   }else ev={status:'partial',click,dialog:dlgText.replace(/\n+/g,'|').slice(0,120),
    detail:'权重按钮点击后无输入框'};
  }
  H.record('PB0035',ev);
 }
 // ---- PB0040 删除（用第二个账号，避免删掉唯一账号）
 {
  let ev={status:'partial',detail:'need 2 accounts'};
  const accs2=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
  if(accs2.length>=2){
   const victim=accs2[1];
   const click=await clickRowBtn(victim.uid,/^删除/);
   await page.waitForTimeout(1200);
   const conf=page.getByRole('alertdialog');
   const hadConfirm=await conf.count();
   if(hadConfirm)await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(2000);
   const after=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
   const gone=!after.some(a=>a.uid===victim.uid);
   ev={status:gone?'verified':'partial',click,confirm_dialog:hadConfirm>0,gone,
    detail:gone?'行内删除（含确认框）-> 权威无该账号':'删除未生效'};
  }else if(accs2.length===1){
   // 只有一个账号：造第二个再删
   await page.evaluate(async ({payload,name}) => {
     const fd=new FormData();
     fd.append('files',new Blob([payload],{type:'application/json'}),name);
     await fetch('/api/accounts/upload?overwrite=true',{method:'POST',body:fd,credentials:'include'});
   },{payload:JSON.stringify({uid:'qa-close-002',accessToken:'fake-at-close2'}),name:'workbuddy-qa-close-002.json'});
   await page.reload({waitUntil:'domcontentloaded'});await page.waitForTimeout(3000);
   const click=await clickRowBtn('qa-close-002',/^删除/);
   await page.waitForTimeout(1200);
   const conf=page.getByRole('alertdialog');
   const hadConfirm=await conf.count();
   if(hadConfirm)await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(2000);
   const after=(await H.api(ctx,'GET','/api/accounts')).body?.accounts||[];
   const gone=!after.some(a=>a.uid==='qa-close-002');
   ev={status:gone?'verified':'partial',click,confirm_dialog:hadConfirm>0,gone,
    detail:gone?'行内删除（含确认框）-> 权威无该账号':'删除未生效'};
  }
  H.record('PB0040',ev);
 }
 // ---- PB0041 导出（导入/导出菜单 -> 导出项）
 {
  const btn=page.locator('button:has-text("导入/导出")').first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   await btn.click({force:true}).catch(()=>{});
   await page.waitForTimeout(1000);
   const items=await page.evaluate(()=>[...document.querySelectorAll('[role=menuitem],[role=menuitemradio]')]
    .map(x=>(x.textContent||'').trim()).filter(Boolean));
   const exp=page.getByRole('menuitem',{name:/导出/}).first();
   if(await exp.count()){
    const [req]=await Promise.all([
     page.waitForRequest(x=>new URL(x.url()).pathname==='/api/accounts/export',{timeout:15000}).catch(()=>null),
     exp.click()]);
    await page.waitForTimeout(2000);
    ev={status:req?'verified':'partial',menu_items:items,export_requested:Boolean(req),
     detail:req?'菜单"导出" -> GET /api/accounts/export':'点击导出未发出请求'};
   }else ev={status:'partial',menu_items:items,detail:'菜单未含导出项'};
  }
  H.record('PB0041',ev);
 }
 // ---- PB0092 任务页开关写请求（再确认）
 {
  await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(3500);
  const sw=page.locator('[role=switch]').first();
  let ev={status:'partial'};
  if(await sw.count()){
   const m=apiMark();
   await sw.click();await page.waitForTimeout(2500);
   const writes=page.__apiLog.slice(m).filter(x=>!x.startsWith('GET'));
   const dlg=page.getByRole('alertdialog');
   let afterDlg=0;
   if(await dlg.count()){afterDlg=1;await dlg.getByRole('button').filter({hasText:/确认|确定/}).first().click().catch(()=>{});await page.waitForTimeout(2000);}
   const writes2=page.__apiLog.slice(m).filter(x=>!x.startsWith('GET'));
   ev={status:(writes.length||writes2.length)?'verified':'partial',
    writes_first:writes.slice(0,3),confirm_dialog:afterDlg>0,writes_after_confirm:writes2.slice(0,3),
    detail:(writes.length||writes2.length)?'开关触发写请求':'再次确认：开关无任何写请求（N27 成立）'};
   // 复位
   await sw.click().catch(()=>{});await page.waitForTimeout(800);
  }
  H.record('PB0092',ev);
 }
 // ---- PB0062/PB0063 playground（#418 修复后重试）
 {
  await page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(4000);
  const errs=[];
  page.on('pageerror',e=>errs.push(String(e.message).slice(0,200)));
  const chip=page.locator('button:has-text("fake-model")').first();
  let ev62={status:'partial'};
  if(await chip.count()){
   const before=(await chip.innerText()).trim();
   await chip.click();await page.waitForTimeout(1500);
   const opts=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    return [...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu]')]
     .filter(vis).flatMap(p=>(p.textContent||'').split('\n').map(t=>t.trim()).filter(Boolean));});
   const alt=opts.find(t=>/fake-model-large/.test(t));
   let picked=false;
   if(alt){await page.getByText(alt,{exact:true}).first().click().catch(()=>{});
    await page.waitForTimeout(900);
    picked=await page.locator('button:has-text("fake-model-large")').count()>0;}
   ev62={status:picked?'verified':'partial',before,options:opts.slice(0,6),picked,
    detail:picked?'模型 chip -> fake-model-large 选中':'选择未完成'};
  }
  H.record('PB0062',ev62);
  // 思考强度
  const pc=page.locator('button:has-text("思考强度")').first();
  let ev63={status:'not_present_in_ui'};
  if(await pc.count()){
   await pc.click();await page.waitForTimeout(1200);
   const opts=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    return [...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu]')]
     .filter(vis).flatMap(p=>(p.textContent||'').split('\n').map(t=>t.trim()).filter(Boolean));});
   const choice=opts.find(t=>/^(高|中|低|关闭|开启|off|on)$/i.test(t));
   let chosen=null;
   if(choice){await page.getByText(choice,{exact:true}).first().click().catch(()=>{});
    await page.waitForTimeout(700);chosen=choice;}
   ev63={status:chosen?'verified':'partial',options:opts.slice(0,6),chosen,
    detail:chosen?'思考强度 popper 选择成功':'popper 已开但未选中'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0063',ev63);
  // 错误文案（真实 502，不用 abort）
  await page.route('**/api/playground/chat',r=>r.fulfill({status:502,contentType:'application/json',
   body:JSON.stringify({detail:'QA 502 注入'})}));
  const ta=page.locator('textarea').first();
  await ta.fill('qa-err-close');await ta.press('Enter');
  await page.waitForTimeout(2800);
  const asst=await page.locator('div.flex.gap-2\\.5.justify-start').last().innerText().catch(()=>'');
  await page.unroute('**/api/playground/chat');
  H.record('PB0072',{status:/QA 502 注入/.test(asst)?'verified':'partial',assistant_text:asst.slice(0,120),
   detail:/QA 502 注入/.test(asst)?'真实 502 注入 -> 气泡显示后端 detail（N25 撤回依据）':'未显示错误文案'});
  H.note('playground pageerrors during retry: '+JSON.stringify(errs.slice(0,3)));
 }
 await page.screenshot({path:path.join(H.run,'close1.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
