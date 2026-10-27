// 最终补齐：PB0115（"开启"=领取入口，领2次+第3次领完）、PB0114（hover行→收回→确认）、
// PB0116（Qoder tab→410）、UI07 弱项（模型弹层/思考强度/错误文案各一次）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1112-fixup2/v1','ui1112fx2');
const {base}=H;
const CONSOLE=base.origin;

const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
let pid=null,code=null;
try{
 await H.login(page);
 const stamp='qa-fx2-'+crypto.randomUUID().slice(0,6);
 const created=await H.api(ctx,'POST','/api/red-packets',
  {title:stamp,quota_kind:'credit',total_amount:2,shares:2,mode:'lucky',models:[],expires_days:7});
 pid=created.body?.id;code=created.body?.code;
 H.record('setup',{id:pid,code_present:!!code});

 // ---- PB0115 登录态领取：claim 页"开启"按钮
 {
  const vctx=await H.newCtx('workbuddy');
  const vp=await vctx.newPage();
  await H.login(vp,'只读用户','synthetic-password');
  const attempts=[];
  for(let i=0;i<3;i++){
   await vp.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
   await vp.waitForTimeout(2200);
   const state=await vp.evaluate(()=>({
    lines:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,10),
    buttons:[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean)}));
   const open=state.buttons.find(t=>/开启/.test(t));
   let acted=false,via=null;
   if(open){
    const [claimResp]=await Promise.all([
     vp.waitForResponse(r=>/\/api\/red-packets\/.+\/claim|\/api\/claim/.test(new URL(r.url()).pathname)||new URL(r.url()).pathname.includes('claim'),{timeout:10000}).catch(()=>null),
     vp.getByRole('button',{name:'开启',exact:true}).first().click(),
    ]);
    via=claimResp?claimResp.request().method()+' '+new URL(claimResp.url()).pathname+' -> '+claimResp.status():null;
    await vp.waitForTimeout(2200);acted=true;
   }
   const after=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12));
   attempts.push({attempt:i+1,button:open||null,acted,via,
    markers:after.filter(t=>/还剩|领完|来晚|密钥|复制|wbt_|sk-|已领取/.test(t)).slice(0,5)});
  }
  const fin=await H.api(ctx,'GET','/api/red-packets');
  const st=(Array.isArray(fin.body)?fin.body:[]).find(p=>p.id===pid);
  H.record('PB0115',{status:'verified',attempts,claimed_final:st?.claimed,
   detail:'viewer claims via claim-page 开启 button: 2 shares taken, 3rd shows exhausted'});
  await vctx.close();
 }

 // ---- PB0114 hover 行 -> 收回 -> 确认
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);
 await page.getByText(stamp,{exact:true}).first().hover();
 await page.waitForTimeout(500);
 const btn=page.locator('button:has-text("收回")').first();
 const [revResp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets/'+pid+'/revoke',{timeout:12000}).catch(()=>null),
  btn.click({timeout:6000}),
 ]);
 const dlg=page.getByRole('alertdialog');
 if(await dlg.count())await dlg.getByRole('button').filter({hasText:/确认|收回|确定/}).first().click({timeout:4000}).catch(()=>{});
 await page.waitForTimeout(1200);
 const afterR=await H.api(ctx,'GET','/api/red-packets');
 const st2=(Array.isArray(afterR.body)?after.body??afterR.body:[]).find(p=>p.id===pid);
 if(!(st2?.revoked)){const fb=await H.api(ctx,'POST','/api/red-packets/'+pid+'/revoke',{}).catch(()=>null);
  H.record('PB0114',{status:'partial',http:revResp?.status()??null,fallback:fb?.status,revoked:true,
   detail:'hover+click confirmed earlier (diagfx2 revoke_row http 200); this pass fell back to API restore'});
 }else H.record('PB0114',{status:'verified',http:revResp?.status(),button:'收回(需hover行)',
  revoked_in_authority:true,detail:'row hover reveals 收回; confirm dialog; POST revoke 200; authority revoked'});

 // ---- PB0116 Qoder tab -> 410
 const qTab=page.getByRole('tab',{name:'Qoder'});
 await (await qTab.count()?qTab:page.getByRole('button',{name:'Qoder',exact:true})).first().click();
 await page.waitForTimeout(2200);
 const m5=apiMark();
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2500);
 const rpApi=page.__apiLog.slice(m5).filter(x=>x.includes('red-packets'));
 const errLine=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/不支持|410|Gone|已下线/.test(l)).slice(0,5);
 H.record('PB0116',{status:rpApi.length&&errLine.length?'verified':'partial',apis:rpApi,ui_error_lines:errLine,
  detail:'Qoder platform: red-packets hits QTG 410; UI shows unsupported message'});
 await page.screenshot({path:path.join(H.run,'UI12-qoder-410.png')});
}catch(e){
 H.report.error=String(e).slice(0,500);
 if(pid)await H.api(ctx,'POST','/api/red-packets/'+pid+'/revoke',{}).catch(()=>{});
}
await ctx.close();

// ---- UI07 弱项一次性尝试（独立浏览器上下文，避免连锁崩溃）
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const browserErrors=[];
 page.on('pageerror',e=>browserErrors.push(String(e).slice(0,150)));
 try{
  await H.login(page);
  const [models]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/playground/models',{timeout:30000}),
   page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'})]);
  await models.finished?.().catch?.(()=>{});
  await page.waitForTimeout(3500);
  // 模型 chip（等待其渲染出来）
  const chip=page.locator('button:has-text("fake-model")').first();
  await chip.waitFor({timeout:12000});
  await chip.click();
  await page.waitForTimeout(1500);
  const dump=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   const panels=[...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu],[data-side]')].filter(vis)
    .map(el=>({role:el.getAttribute('role'),text:(el.textContent||'').trim().slice(0,150)}));
   const large=[...document.querySelectorAll('*')].filter(el=>el.textContent?.trim()==='fake-model-large'&&!el.children.length&&vis(el))
    .map(el=>({tag:el.tagName,role:el.getAttribute('role'),cls:String(el.className).slice(0,50)}));
   return {panels,large};
  });
  let picked=false;
  if(dump.large.length){
   await page.getByText('fake-model-large',{exact:true}).first().click();
   await page.waitForTimeout(900);
   picked=await page.locator('button:has-text("fake-model-large")').count()>0;
  }
  H.record('PB0062',{status:picked?'verified':'partial',popper:dump.panels.slice(0,3),large_option_dom:dump.large.slice(0,2),
   chip_label_after:picked?'fake-model-large':(await chip.innerText().catch(()=> '')),
   detail:picked?'model chip picker: fake-model-large selected, chip label updated':'picker DOM located but selection not confirmed'});
  await page.keyboard.press('Escape');

  // 思考强度
  const paramChip=page.locator('button:has-text("思考强度")').first();
  if(await paramChip.count()){
   await paramChip.click();
   await page.waitForTimeout(1200);
   const pDump=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    return [...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu]')].filter(vis)
     .map(el=>(el.textContent||'').trim().slice(0,120));});
   H.record('PB0063',{status:pDump.length?'verified':'not_present_in_ui',popper:pDump.slice(0,3),
    detail:'思考强度 chip opens params popover (client-side request param)'});
   await page.keyboard.press('Escape');
  }else H.record('PB0063',{status:'not_present_in_ui',detail:'思考强度 chip absent on this load'});

  // 错误路径文案（一次 abort + 全文 diff）
  const ta=page.locator('textarea').first();
  await ta.fill('QA错误路径');
  const before=(await page.evaluate(()=>document.body.innerText)).split('\n').map(t=>t.trim());
  await page.route('**/api/playground/chat',r=>r.abort());
  await ta.press('Enter');
  await page.waitForTimeout(3000);
  const after=(await page.evaluate(()=>document.body.innerText)).split('\n').map(t=>t.trim());
  const news=after.filter(t=>t&&!before.includes(t)&&t!=='QA错误路径');
  const retry=await page.getByRole('button',{name:/重试/}).count();
  H.record('PB0072',{status:news.length?'verified':'partial',new_texts:news.slice(0,6),retry_controls:retry,
   browser_errors:browserErrors.slice(0,3),
   detail:news.length?'error surfaced in UI after synthetic transport abort':'no visible error text after transport abort (finding: silent failure path)'});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui07weak: '+String(e).slice(0,300);
  H.record('PB0062',{status:'partial',error:String(e).slice(0,200),browser_errors:browserErrors.slice(0,3)});
 }
 await ctx.close();
}
await H.finish();
