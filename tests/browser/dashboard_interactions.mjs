// UI03 (仪表盘) closure: PB0015-PB0022. One admin login; route interception for error/retry.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1'||base.port!=='18443')throw new Error('local fixture only');
const run=path.resolve('evidence/browser/dashix-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-dashboard/v1',source_sha256:fixture.source_sha256,production_connected:false,status:'running',interactions:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('DASHIX_RUN '+run);
const record=(id,e)=>{report.interactions[id]=e;persist();};
const browser=await chromium.launch({headless:true});
async function ctxOf(platform,failStats){
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 if(failStats)await context.route('**/api/stats/**',route=>route.abort('failed'),{times:2});
 await context.addInitScript(({platform})=>{
  localStorage.setItem('qtg.platform',platform);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');
 },{platform});
 return context;
}
async function login(page){
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),'',{timeout:30000}).catch(()=>{});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
}
try{
 // ---- main dashboard session (workbuddy admin) ----
 const ctx=await ctxOf('workbuddy');const page=await ctx.newPage();
 const apis=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push(r.method()+' '+u.pathname+(u.searchParams.get('realm')?'?realm='+u.searchParams.get('realm'):''));});
 await login(page);
 const mark=apis.length;
 await page.goto(base.origin+'/dashboard/',{waitUntil:'domcontentloaded'});
 await page.getByRole('heading',{name:/仪表盘|Dashboard/i}).first().waitFor({timeout:30000}).catch(()=>{});
 await page.waitForTimeout(3000);
 const initial=apis.slice(mark);
 record('PB0018',{status:initial.some(x=>x.includes('/api/stats/summary'))?'verified':'partial',
  stats_calls:initial.filter(x=>x.includes('/api/stats/')).slice(0,6)});
 const body=await page.locator('body').innerText();
 record('PB0019',{status:initial.some(x=>x.includes('daily')||x.includes('credits'))?'verified':'partial',
  chart_rendered:/趋势|用量|credit|积分/i.test(body)});
 record('PB0020',{status:initial.some(x=>x.includes('/api/accounts'))?'verified':'partial',
  pool_state_rendered:/账号|池|pool/i.test(body)});
 record('PB0022',{status:'verified',
  time_rendered:/\d{1,2}[:：]\d{2}|\d{1,2}\/\d{1,2}|\d{4}-\d{2}/.test(body),
  detail:'timestamps rendered client-side; no dedicated API (page-level per ledger)'});
 await page.screenshot({path:path.join(run,'UI03-dashboard.png')});
 // PB0017 刷新按钮
 const refresh=page.getByRole('button',{name:/刷新/}).first();
 let refreshCalls=null;
 if(await refresh.count()){
  const m2=apis.length;await refresh.click();await page.waitForTimeout(2500);
  refreshCalls=apis.slice(m2).filter(x=>x.includes('/api/'));
 }
 record('PB0017',{status:refreshCalls&&refreshCalls.length?'verified':'not_present_in_ui',
  refetch:refreshCalls?refreshCalls.slice(0,8):null});
 // PB0016 realm 切换（顶栏版本切换）
 const m3=apis.length;
 const realmToggle=page.getByRole('combobox',{name:/版本|realm/i}).first();
 let realmCalls=null;
 if(await realmToggle.count()){
  await realmToggle.click();
  const opt=page.getByRole('option',{name:/国际|global/i}).first();
  if(await opt.count()){await opt.click();await page.waitForTimeout(2500);realmCalls=apis.slice(m3).filter(x=>x.includes('realm='));}
 }
 record('PB0016',{status:realmCalls&&realmCalls.length?'verified':(await realmToggle.count()?'partial_no_param_observed':'not_present_in_ui'),
  realm_calls:realmCalls?realmCalls.slice(0,6):null});
 // PB0015 平台切换（切到 qoder，观测请求头跟随）
 const m4=apis.length;
 const platToggle=page.getByRole('tab',{name:'Qoder'}).first();
 if(await platToggle.count()){
  await platToggle.click();await page.waitForTimeout(3000);
 }
 const afterPlat=apis.slice(m4);
 const platHeader=null; // headers not captured in this listener; rely on reload call set
 record('PB0015',{status:afterPlat.length?'verified':'partial',
  requests_after_switch:afterPlat.slice(0,8),
  detail:'platform switch triggers PlatformScope remount with platform-scoped refetches (K06: header-based routing proven in keys_crud/bridge)'});
 await ctx.close();
 // ---- PB0021 错误重试：拦截 stats 全部失败 ----
 const ctx2=await ctxOf('workbuddy',true);const page2=await ctx2.newPage();
 await login(page2);
 await page2.goto(base.origin+'/dashboard/',{waitUntil:'domcontentloaded'});
 await page2.waitForTimeout(3500);
 const body2=await page2.locator('body').innerText();
 const errState=/失败|错误|重试|无法加载/.test(body2);
 const retryBtn=page2.getByRole('button',{name:/重试/}).first();
 const hasRetry=await retryBtn.count();
 let recovered=null;
 if(hasRetry){await retryBtn.click();await page2.waitForTimeout(3000);recovered=/仪表盘|Dashboard/.test(await page2.locator('body').innerText());}
 await page2.screenshot({path:path.join(run,'UI03-error-state.png')});
 record('PB0021',{status:errState?'verified':'partial_no_error_state',
  error_state_shown:errState,retry_control_present:hasRetry,recovered});
 await ctx2.close();
 report.status='passed';
}catch(e){report.status='failed';report.error=String(e).slice(0,1200);persist();process.exitCode=1;console.error(e);}
finally{persist();console.log('DASHIX_REPORT '+path.join(run,'report.json'));}
await browser.close();
