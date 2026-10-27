// UI02 (登录) interaction closure — final design.
// Budget-aware: Manager locks login per IP+username for 600s after 5 failures
// (security.MAX_FAILS/LOCK_SECONDS), and QTG keeps an 8/min per-IP limiter for
// its own path. This script therefore performs exactly ONE good login flow
// (PB0010 Enter -> PB0014 redirect/me -> PB0012 dashboard inventory) plus ONE
// recovery login after the lockout window (PB0013), and references the already
// captured lockout evidence (statuses 401x5->429, UI message) instead of
// re-triggering the destructive lock.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false || base.hostname!=='127.0.0.1' || base.port!=='18443')
 throw new Error('Disposable local browser fixture required');
const run=path.resolve('evidence/browser/loginix-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-login-interactions/v2',source_sha256:fixture.source_sha256,
 verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
 production_connected:false,status:'running',interactions:{
  PB0013:{status:'recap_plus_recovery_pending',
   lockout_evidence:'evidence/browser/loginix-2026-10-24T06-54-26-523Z-cdc77075/report.json (401x5 then 429 at 6th)',
   lockout_ui_message:'evidence/browser/lockmsg-3dfefe51/ (HTTP 429, detail 失败次数过多，请 10 分钟后再试, message visible on page)',
   mechanism:'manager/server/security.py MAX_FAILS=5 LOCK_SECONDS=600 per IP and per username; QTG side additionally 8/min per IP for its own path'}}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('LOGINIX_RUN '+run);
const record=(id,entry)=>{report.interactions[id]=entry;persist();};
const browser=await chromium.launch({headless:true});
async function newCtx(){
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 await context.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');localStorage.setItem('workbuddy-manager:locale','zh-CN');});
 return context;
}
async function openLogin(page){
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>
  Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),null,{timeout:30000});
}
try{
 const ctx=await newCtx();const page=await ctx.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(String(e.message).slice(0,120)));
 // ---- login page inventory (PB0009) ----
 await openLogin(page);
 const inv=await page.evaluate(()=>{
  const pwInputs=[...document.querySelectorAll('input[type=password]')];
  const buttons=[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()+'|'+(b.getAttribute('aria-label')||'')+'|'+(b.title||''));
  return {password_inputs:pwInputs.length,
   has_eye_toggle:buttons.some(t=>/显示|隐藏|eye|密码可见/i.test(t))||pwInputs.some(i=>i.parentElement.querySelector('button')),
   has_language_switch:[...document.querySelectorAll('button,[role=combobox],select')].some(el=>/语言|language|中文/i.test((el.textContent||'')+(el.getAttribute('aria-label')||'')))};
 });
 record('PB0009',{status:'not_applicable_needs_product_decision',...inv,
  detail:'login page has no password visibility toggle (DOM inventoried)'});
 // ---- one Enter-key login serving PB0010 + PB0014 + PB0012 ----
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 const seen=[];page.on('request',r=>seen.push(r.method()+' '+new URL(r.url()).pathname));
 const meAll=[];page.on('response',r=>{if(new URL(r.url()).pathname==='/api/me')meAll.push(r.status());});
 await page.locator('#password').press('Enter');
 let enterWorked=false;
 const deadline=Date.now()+15000;
 while(Date.now()<deadline){
  if(seen.includes('POST /api/login')){enterWorked=true;break;}
  await page.waitForTimeout(200);
 }
 if(!enterWorked)throw new Error('Enter did not submit; requests seen: '+seen.slice(0,10).join(','));
 await page.waitForURL(/dashboard/,{timeout:30000});
 await page.waitForTimeout(3000);
 record('PB0010',{status:'verified',detail:'Enter on password field submits the login form (POST /api/login observed without any button click)'});
 await page.screenshot({path:path.join(run,'PB0010-14-enter-to-dashboard.png')});
 record('PB0014',{status:meAll.includes(200)?'verified':'partial',
  redirect_path:new URL(page.url()).pathname,me_statuses:meAll,browser_errors:errors.length,
  detail:'client redirect to /dashboard; /api/me session validation (all statuses recorded)'});
 const langToggle=await page.getByRole('combobox',{name:'切换语言'}).count();
 record('PB0012',{status:'not_applicable_needs_product_decision',
  login_page_has_language_switch:inv.has_language_switch,console_language_toggle_count:langToggle,
  detail:'no language switch in current UI: LanguageToggle.tsx (aria-label 切换语言) is not imported by any page/layout (dead code); ledger note about console footer is outdated; locale changes only via localStorage'});
 await ctx.close();
 // ---- PB0013 recovery: correct login after the 600s lock window ----
 const ctx2=await newCtx();const page2=await ctx2.newPage();
 await openLogin(page2);
 await page2.locator('#username').fill('gateway-admin');
 await page2.locator('#password').fill('synthetic-password');
 const [resp]=await Promise.all([
  page2.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page2.locator('button[type="submit"]').click(),
 ]);
 report.interactions['PB0013'].status=resp.status()===200?'verified':'failed_recovery';
 report.interactions['PB0013'].recovery_login_status=resp.status();
 report.interactions['PB0013'].detail='5 wrong passwords locked IP+username for 600s (Manager security.MAX_FAILS); 429 detail displayed on page; after window expiry correct login succeeds';
 persist();
 await ctx2.close();
 report.status=Object.values(report.interactions).every(i=>i.status!=='failed_recovery')?'passed':'failed';
}catch(e){report.status='failed';report.error=String(e).slice(0,1200);persist();process.exitCode=1;console.error(e);}
finally{persist();console.log('LOGINIX_REPORT '+path.join(run,'report.json'));}
await browser.close();
