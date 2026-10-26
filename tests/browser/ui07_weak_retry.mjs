// UI07 弱项重试（新 fixture 1d358c1e）：模型选择 / 思考强度 / 错误文案 / 流式采样。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui07-retry/v1','ui07rt');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const errs=[];
page.on('pageerror',e=>errs.push(String(e).slice(0,120)));
const ASST='div.flex.gap-2\\.5.justify-start';
try{
 await H.login(page);
 const [models]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/playground/models',{timeout:30000}),
  page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'})]);
 await models.finished?.().catch?.(()=>{});
 await page.waitForTimeout(3500);
 // 模型 chip
 const chip=page.locator('button:has-text("fake-model")').first();
 await chip.waitFor({timeout:15000});
 const before=await chip.innerText();
 await chip.click();
 await page.waitForTimeout(1500);
 const dump=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const panels=[...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu]')].filter(vis)
   .map(el=>(el.textContent||'').trim().slice(0,150));
  const large=[...document.querySelectorAll('*')].filter(el=>el.textContent?.trim()==='fake-model-large'&&!el.children.length&&vis(el))
   .map(el=>({tag:el.tagName,role:el.getAttribute('role')}));
  return {panels,large};});
 let picked=false;
 if(dump.large.length){
  await page.getByText('fake-model-large',{exact:true}).first().click();
  await page.waitForTimeout(900);
  picked=await page.locator('button:has-text("fake-model-large")').count()>0;
 }
 H.record('PB0062',{status:picked?'verified':'partial',before:before.trim(),picked,
  popper:dump.panels.slice(0,2),large_dom:dump.large.slice(0,2),
  detail:picked?'模型 chip -> fake-model-large 选中，chip 文案更新':'仍未能完成选择'+(errs.length?'; pageerror:'+errs[0]:'')});
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
  let chosen=null;
  const opts=pDump.flatMap(t=>t.split('\n')).map(t=>t.trim()).filter(Boolean);
  const choice=opts.find(t=>/^(高|中|低|off|on)$/i.test(t));
  if(choice){await page.getByText(choice,{exact:true}).first().click();await page.waitForTimeout(600);chosen=choice;}
  H.record('PB0063',{status:chosen?'verified':'partial',popper:opts.slice(0,6),chosen,
   detail:'思考强度 popper 选择'+(chosen?'成功':'未完成')});
  await page.keyboard.press('Escape');
 }else H.record('PB0063',{status:'not_present_in_ui',detail:'思考强度 chip 缺席'});
 // 流式采样（第二轮）
 const ta=page.locator('textarea').first();
 await ta.fill('第一轮');await ta.press('Enter');
 await page.waitForTimeout(2500);
 const lengths=[];
 const c0=await page.locator(ASST).count();
 const sampler=setInterval(async()=>{try{const c=await page.locator(ASST).count();
  if(c)lengths.push((await page.locator(ASST).last().innerText()).length);}catch{}},200);
 await ta.fill('第二轮：请多写几句');
 const [chat2]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/playground/chat',{timeout:60000}),
  ta.press('Enter')]);
 await chat2.finished().catch(()=>{});
 await new Promise(r=>setTimeout(r,600));
 clearInterval(sampler);
 H.record('PB0066',{status:lengths.length>=2&&new Set(lengths).size>1?'verified':'partial',
  growth_samples:lengths.slice(0,14),blocks_before_after:[c0,await page.locator(ASST).count()],
  detail:'增量渲染采样（新 fixture 重试）'});
 // 错误文案（body diff）
 await page.route('**/api/playground/chat',r=>r.abort());
 const bBefore=(await page.evaluate(()=>document.body.innerText)).split('\n').map(x=>x.trim());
 await ta.fill('err-probe-x9');await ta.press('Enter');
 await page.waitForTimeout(3000);
 const bAfter=(await page.evaluate(()=>document.body.innerText)).split('\n').map(x=>x.trim());
 const news=bAfter.filter(x=>x&&!bBefore.includes(x)&&x!=='err-probe-x9');
 H.record('PB0072',{status:news.length?'verified':'partial',new_texts:news.slice(0,6),
  detail:news.length?'错误文案可见':'仍无可见错误（N25 确认）'});
 await page.unroute('**/api/playground/chat');
 await page.screenshot({path:path.join(H.run,'ui07-retry.png')});
}catch(e){H.report.error=String(e).slice(0,400);H.note('pageerrors:'+JSON.stringify(errs.slice(0,3)));}
await ctx.close();
await H.finish();
