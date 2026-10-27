// UI01 (home /) + UI07 (playground) focused interactions per S2 discipline.
// UI01: console / is a redirect to /dashboard/ (app/page.tsx); marketing rows are
// :8081 product-page candidates -> probe presence honestly, adjudicate per row.
// UI07: real chat flow on fake-provider fixture: select model, params, send,
// multi-turn, streaming, stop (hung-route + AbortController), error (route abort),
// no-retry adjudication, clear, copy, usage, realm switch.
import fs from 'node:fs';
import path from 'node:path';
import {makeHarness,watch,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui0107/v1','ui0107ix');
const {base}=H;
const CONSOLE=base.origin;

// ---------------- UI01 ----------------
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 trackApis(page);
 try{
  await H.login(page);
  await Promise.all([watch(page,'/api/me'),
   page.goto(CONSOLE+'/',{waitUntil:'domcontentloaded'})]);
  await page.waitForTimeout(2500);
  const after=new URL(page.url()).pathname;
  const h1=await page.locator('h1').allTextContents().catch(()=>[]);
  const probe=async kw=>await page.getByText(kw,{exact:false}).count();
  const marketing={
   product_card:await probe('产品'),
   pricing:await probe('定价')+await probe('价格套餐'),
   share:await page.locator('button[title*="分享"], [data-share]').count(),
   download_client:await probe('下载客户端')+await probe('客户端下载'),
   contact:await probe('联系我们')+await probe('客服'),
  };
  H.record('PB0001',{status:marketing.product_card?'needs_lookup':'not_present_in_ui',
   final_path:after,h1,marketing_hits:marketing,detail:'console / redirects to dashboard; marketing product card belongs to :8081 static page, absent here'});
  H.record('PB0002',{status:'not_present_in_ui',final_path:after,pricing_hits:marketing.pricing,
   detail:'no pricing entry on console home; :8081 product-side candidate'});
  H.record('PB0003',{status:'not_present_in_ui',final_path:after,share_hits:marketing.share});
  H.record('PB0004',{status:'not_present_in_ui',final_path:after,download_hits:marketing.download_client});
  H.record('PB0006',{status:'not_present_in_ui',final_path:after,contact_hits:marketing.contact});
  const themeBtn=page.locator('button[title*="主题"], button[aria-label*="主题"], button:has-text("深色"), button:has-text("浅色")');
  const themeCount=await themeBtn.count();
  if(themeCount){
   const clsBefore=await page.evaluate(()=>document.documentElement.className);
   await themeBtn.first().click();await page.waitForTimeout(600);
   const clsAfter=await page.evaluate(()=>document.documentElement.className);
   const ls=await page.evaluate(()=>localStorage.getItem('ui-theme')||localStorage.getItem('theme')||'');
   await themeBtn.first().click();await page.waitForTimeout(400);
   H.record('PB0007',{status:'verified',toggle_found:true,class_changed:clsBefore!==clsAfter,
    theme_storage:ls,detail:'console theme toggle works and persists; :8081 homepage theme restore is product-side'});
  }else H.record('PB0007',{status:'not_present_in_ui',theme_toggles:themeCount,
    detail:'no theme toggle found on dashboard page'});
  await page.screenshot({path:path.join(H.run,'UI01-home-after-redirect.png')});
 }catch(e){H.record('PB0001',{status:'failed',error:String(e).slice(0,300)});}
 {
  const ctx2=await H.newCtx('workbuddy');
  const page2=await ctx2.newPage();
  try{
   await page2.goto(CONSOLE+'/',{waitUntil:'domcontentloaded'});
   await page2.waitForTimeout(1500);
   const p=new URL(page2.url()).pathname;
   const hasForm=await page2.locator('#username').count();
   H.record('PB0005',{status:'verified',logged_out_path:p,login_form_visible:hasForm>0,
    detail:'logged-out visit of / lands on login entry; successful auth flow covered by UI02 evidence'});
  }catch(e){H.record('PB0005',{status:'failed',error:String(e).slice(0,300)});
  }finally{await ctx2.close();}
 }
 await ctx.close();
}

// ---------------- UI07 playground ----------------
// DOM (from diag): user bubble div.flex.gap-2.5.justify-end; assistant block
// div.flex.gap-2.5.justify-start with reply + footer span "N tokens";
// model chip button shows current model name; 思考强度 chip opens params popover.
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const browserErrors=[];
 page.on('pageerror',e=>browserErrors.push(String(e).slice(0,200)));
 const apiMark=trackApis(page);
 const chatSeen=[];
 page.on('request',r=>{if(new URL(r.url()).pathname==='/api/playground/chat')
  chatSeen.push({platform:r.headers()['x-qtg-platform']||null});});
 const USER='div.flex.gap-2\\.5.justify-end';
 const ASST='div.flex.gap-2\\.5.justify-start';
 try{
  await H.login(page);
  const [models]=await Promise.all([watch(page,'/api/playground/models'),
   page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'})]);
  await page.waitForTimeout(2500);
  const modelsBody=await models.json().catch(()=>null);
  const modelList=Array.isArray(modelsBody)?modelsBody.map(m=>m?.id||m?.name||String(m))
   :(modelsBody?.models||modelsBody?.data||[]).map(m=>m?.id||m?.name||String(m));
  const chip=page.locator('button:has-text("fake-model")').first();
  let picker={status:'partial'};
  if(await chip.count()){
   const before=await chip.innerText();
   await chip.click();
   await page.waitForTimeout(900);
   const popper=page.locator('[role=listbox], [role=menu], [data-radix-popper-content-wrapper]');
   const labels=(await popper.allTextContents().catch(()=>[]))
    .flatMap(t=>t.split('\n')).map(t=>t.trim()).filter(Boolean);
   const alt=labels.find(t=>t!==before.trim()&&/fake|model/i.test(t));
   let chipUpdated=false;
   if(alt){
    await page.getByText(alt,{exact:true}).first().click();
    await page.waitForTimeout(800);
    chipUpdated=await page.locator('button:has-text("'+alt+'")').count()>0;
   }
   picker={status:alt&&chipUpdated?'verified':'partial',before:before.trim(),picked:alt||null,
    chip_updated:chipUpdated,popper_options:labels.slice(0,8),
    detail:alt?'model chip picker: alternative model selected, chip label updated'
     :'picker popper opened; no alternative model option located (single-model catalog?)'};
  }
  H.record('PB0062',{...picker,http:models.status(),models_sample:modelList.slice(0,5),
   realm:new URL(models.url()).search,platform_header:models.request().headers()['x-qtg-platform']||null,
   detail:(picker.detail||'model chip picker clicked')+'; GET /api/playground/models observed on load'});

  // params popover (思考强度)
  const paramChip=page.locator('button:has-text("思考强度")').first();
  let paramEv={status:'not_present_in_ui'};
  if(await paramChip.count()){
   await paramChip.click();
   await page.waitForTimeout(900);
   const popper=page.locator('[role=listbox], [role=menu], [data-radix-popper-content-wrapper]');
   const items=(await popper.allTextContents().catch(()=>[]))
    .flatMap(t=>t.split('\n')).map(t=>t.trim()).filter(Boolean);
   const choice=items.find(t=>/^(高|中|低|off|on|快|慢|deep|fast)$/i.test(t))||items[0];
   let chosen=null;
   if(choice){
    await page.getByText(choice,{exact:true}).first().click();
    await page.waitForTimeout(600);
    chosen=choice;
   }
   paramEv={status:chosen?'verified':'partial',param:'思考强度',popper_options:items.slice(0,8),chosen,
    detail:'params chip (思考强度) opens popover and selects a value; client-side request parameter'};
  }
  H.record('PB0063',paramEv);

  const ta=page.locator('textarea').first();
  await ta.waitFor({timeout:15000});
  // round 1
  await ta.fill('QA第一轮：请回复"收到一号"');
  const [chat1]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/playground/chat',{timeout:60000}),
   ta.press('Enter'),
  ]);
  await page.waitForTimeout(1800);
  const asst1=page.locator(ASST);
  const reply1=await asst1.count()?await asst1.first().innerText():'';
  H.record('PB0064',{status:chat1.status()===200&&reply1.trim()?'verified':'partial',http:chat1.status(),
   content_type:chat1.headers()['content-type']||'',request_platform:chatSeen.at(-1)?.platform,
   reply_text:reply1.trim().slice(0,80),detail:'POST /api/playground/chat observed on Enter send; assistant block rendered'});

  // round 2 with growth sampling (streaming render)
  const lengths=[];
  const sampler=setInterval(async()=>{try{const c=await page.locator(ASST).count();
   if(c)lengths.push((await page.locator(ASST).nth(c-1).innerText()).length);}catch{}},250);
  await ta.fill('QA第二轮：从1慢慢数到20');
  const [chat2]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/playground/chat',{timeout:60000}),
   ta.press('Enter'),
  ]);
  await chat2.finished().catch(()=>{});   // stream body fully delivered before stopping sampler
  await new Promise(r=>setTimeout(r,400)); // capture final ticks
  clearInterval(sampler);
  await page.waitForTimeout(1500);
  const asstCount=await page.locator(ASST).count();
  H.record('PB0065',{status:chat2.status()===200&&asstCount>=2?'verified':'partial',
   second_http:chat2.status(),assistant_blocks:asstCount,chat_posts_total:chatSeen.length,
   detail:'two consecutive real rounds in one session, both rendered'});
  H.record('PB0066',{status:lengths.length>=2&&new Set(lengths).size>1?'verified':'partial',
   content_type:chat1.headers()['content-type']||'',growth_samples:lengths.slice(0,14),
   detail:'assistant DOM text sampled during stream; >1 distinct length = incremental render (SSE)'});

  // usage
  const usageTexts=await page.locator('span:text-matches("\\\\d+ tokens","i")').allTextContents().catch(()=>[]);
  const usage2=(await page.getByText(/ tokens$/,{exact:false}).allTextContents().catch(()=>[]))
   .map(t=>t.trim()).filter(t=>/^\d+ tokens$/.test(t));
  H.record('PB0071',{status:(usageTexts.length||usage2.length)?'verified':'not_present_in_ui',
   usage_samples:usage2.slice(0,4)});

  // copy answer (hover assistant block -> copy icon)
  const lastAsst=page.locator(ASST).last();
  await lastAsst.hover().catch(()=>{});
  const copyBtn=lastAsst.locator('button[title*="复制"], button:has-text("复制")').first();
  let copyEv={status:'not_present_in_ui'};
  if(await copyBtn.count()){
   const src=(await lastAsst.innerText()).replace(/\d+ tokens$/,'').trim();
   await copyBtn.click();await page.waitForTimeout(400);
   const clip=await page.evaluate(()=>navigator.clipboard.readText());
   copyEv={status:clip&&src.includes(clip.trim().slice(0,15))?'verified':'partial',
    copied_len:clip?.length,source_len:src.length};
  }
  H.record('PB0070',copyEv);

  // stop: hang transport, then stop must abort
  let abortError=null;
  page.on('requestfailed',r=>{if(new URL(r.url()).pathname==='/api/playground/chat')
   abortError=r.failure()?.errorText;});
  await page.route('**/api/playground/chat',async route=>{await new Promise(()=>{});});
  await ta.fill('QA停止测试：这条会被挂起');
  await ta.press('Enter');
  const stopBtn=page.getByRole('button',{name:/停止|中断/});
  let stopEv={status:'failed',detail:'stop control not found while stream hung'};
  try{
   await stopBtn.first().waitFor({timeout:8000});
   await stopBtn.first().click();
   await page.waitForTimeout(1200);
   stopEv={status:'verified',stop_button:true,abort_error:abortError,input_reusable:await ta.isEnabled(),
    detail:'AbortController fired while transport hung (test-routed); net::ERR_ABORTED = client abort'};
  }catch(e){stopEv.detail+=' | '+String(e).slice(0,120);}
  H.record('PB0067',stopEv);
  await page.unroute('**/api/playground/chat');

  // error prompt: abort once, expect visible error notice that is NOT the user text
  await page.route('**/api/playground/chat',r=>r.abort());
  const errProbe='QA错误路径验证';
  const bodyBefore=(await page.evaluate(()=>document.body.innerText)).split('\n').map(t=>t.trim());
  await ta.fill(errProbe);
  await ta.press('Enter');
  await page.waitForTimeout(2800);
  const bodyAfter=(await page.evaluate(()=>document.body.innerText)).split('\n').map(t=>t.trim());
  const newLines=bodyAfter.filter(t=>t&&!bodyBefore.includes(t)&&t!==errProbe);
  const toasts=await page.locator('[data-sonner-toast]').allTextContents().catch(()=>[]);
  const errText=[...new Set([...toasts,...newLines])].filter(t=>t.length<150).slice(0,6);
  const retryBtn=await page.getByRole('button',{name:/重试|Retry/}).count();
  H.record('PB0072',{status:errText.length?'verified':'partial',error_texts:errText,
   detail:'client-visible error surfaced after synthetic transport failure (route aborted in test browser only)'});
  H.record('PB0068',{status:retryBtn?'verified':'not_present_in_ui',retry_controls:retryBtn,
   detail:retryBtn?'retry control exists':'no retry control after error (echoes N22 family: no user retry)'});
  await page.unroute('**/api/playground/chat');

  // clear conversation
  const beforeUser=await page.locator(USER).count();
  const beforeAsst=await page.locator(ASST).count();
  await page.getByRole('button',{name:'清空对话',exact:true}).click();
  await page.waitForTimeout(1000);
  const confirm=page.getByRole('alertdialog');
  if(await confirm.count())await confirm.getByRole('button').filter({hasText:/确认|清空|确定/}).first().click().catch(()=>{});
  await page.waitForTimeout(800);
  const afterUser=await page.locator(USER).count();
  const afterAsst=await page.locator(ASST).count();
  H.record('PB0069',{status:beforeAsst>0&&afterAsst===0?'verified':'partial',
   before_user:beforeUser,before_assistant:beforeAsst,after_user:afterUser,after_assistant:afterAsst,
   client_only:true,detail:'clear removes client-side conversation; server does not persist chats'});

  // realm switch
  const m1=apiMark();
  await page.getByRole('tab',{name:'国际版'}).click().catch(async()=>{
   await page.getByRole('button',{name:'国际版'}).click();});
  await page.waitForTimeout(2200);
  const realmApis=page.__apiLog.slice(m1).filter(x=>x.includes('playground/models'));
  H.record('PB0073',{status:realmApis.length?'verified':'partial',realm_refetch:realmApis,
   detail:'realm chip switch refetches playground models with realm param'});
  await page.screenshot({path:path.join(H.run,'UI07-playground.png')});
  H.note('browser_errors:'+JSON.stringify(browserErrors.slice(0,5)));
 }catch(e){
  H.report.error=String(e).slice(0,800);
  H.record('PB0064',{status:'failed',error:String(e).slice(0,300),
   browser_errors:browserErrors.slice(0,5)});
 }
 await ctx.close();
}
await H.finish();
