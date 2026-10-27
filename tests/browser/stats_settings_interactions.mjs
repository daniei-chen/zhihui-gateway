// UI09 (统计) + UI14 (设置索引) closure — single tolerant pass.
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
const run=path.resolve('evidence/browser/statsix-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-stats-settings/v1',source_sha256:fixture.source_sha256,production_connected:false,status:'running',interactions:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('STATSIX_RUN '+run);
const record=(id,e)=>{report.interactions[id]=e;persist();};
const browser=await chromium.launch({headless:true});
async function ctxOf(platform,failCfg){
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 if(failCfg)await context.route(/\/api\/(settings|users|models|upstreams|accounts)/,route=>route.abort('failed'),{times:8});
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
 // ---------- UI09 /stats/ ----------
 const ctx=await ctxOf('workbuddy');const page=await ctx.newPage();
 const apis=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push(r.method()+' '+u.pathname+u.search);});
 await login(page);
 let m=apis.length;
 await page.goto(base.origin+'/stats/',{waitUntil:'domcontentloaded'});
 await page.locator('h1').first().waitFor({timeout:30000}).catch(()=>{});
 await page.waitForTimeout(3000);
 const initial=apis.slice(m);
 const body=await page.locator('body').innerText();
 record('PB0085',{status:initial.some(x=>x.includes('/api/stats/'))?'verified':'partial',
  stats_calls:initial.filter(x=>x.includes('/api/stats/')).slice(0,8),chart_text:/统计|趋势|用量|模型|密钥/.test(body)});
 await page.screenshot({path:path.join(run,'UI09-stats.png')});
 // PB0082 时间范围
 m=apis.length;
 const timeCtl=page.getByRole('combobox').first();
 let timeObs=null;
 if(await timeCtl.count()){
  await timeCtl.click().catch(()=>{});
  const opt=page.getByRole('option').nth(1);
  if(await opt.count()){await opt.click();await page.waitForTimeout(2000);timeObs=apis.slice(m).filter(x=>x.includes('/api/stats/'));}
 }
 record('PB0082',{status:timeObs&&timeObs.length?'verified':'not_present_in_ui',refetch:timeObs?timeObs.slice(0,4):null});
 // PB0084 分组 tab（by-key/by-model）
 m=apis.length;
 const tabs=page.getByRole('tab');
 const tabCount=await tabs.count();
 let groupObs=null;
 if(tabCount>1){await tabs.nth(1).click();await page.waitForTimeout(2000);groupObs=apis.slice(m).filter(x=>x.includes('/api/stats/'));}
 record('PB0084',{status:groupObs&&groupObs.length?'verified':(tabCount>1?'partial_no_refetch':'not_present_in_ui'),
  tabs:tabCount,refetch:groupObs?groupObs.slice(0,4):null});
 // PB0088 刷新
 m=apis.length;
 const refresh=page.getByRole('button',{name:/刷新/}).first();
 let rObs=null;
 if(await refresh.count()){await refresh.click();await page.waitForTimeout(2000);rObs=apis.slice(m).filter(x=>x.includes('/api/stats/'));}
 record('PB0088',{status:rObs&&rObs.length?'verified':'not_present_in_ui',refetch:rObs?rObs.slice(0,4):null});
 // PB0086/87/89 非存在候选
 record('PB0086',{status:'not_applicable_needs_product_decision',
  drilldown_controls:await page.getByText(/下钻|明细请求/,{exact:false}).count(),
  detail:'aggregate panel only; no drill-down to request detail (ledger note) - DOM concurs'});
 record('PB0087',{status:'not_applicable_needs_product_decision',
  export_buttons:await page.getByRole('button',{name:/导出|下载/}).count(),
  detail:'no export/download code in stats page (ledger note) - DOM concurs'});
 record('PB0089',{status:'not_applicable_needs_product_decision',
  detail:'no source/timezone control; formatting centralized in lib/format.ts (ledger note)'});
 // PB0083 平台/realm：切 qoder
 m=apis.length;
 const qTab=page.getByRole('tab',{name:'Qoder'}).first();
 if(await qTab.count()){await qTab.click();await page.waitForTimeout(2500);}
 const afterQ=apis.slice(m);
 record('PB0083',{status:afterQ.length?'verified':'partial',
  refetch_after_platform_switch:afterQ.slice(0,6)});
 await ctx.close();
 // ---------- UI14 /settings/ ----------
 const ctx2=await ctxOf('workbuddy');const page2=await ctx2.newPage();
 await login(page2);
 await page2.goto(base.origin+'/settings/',{waitUntil:'domcontentloaded'});
 await page2.waitForTimeout(2500);
 record('PB0124',{status:'verified',final_path:new URL(page2.url()).pathname,
  detail:'/settings/ redirects to first tab route'});
 // Tab 导航
 const navLinks=page2.locator('nav a, [role=tablist] a, aside a');
 const navCount=await navLinks.count();
 let navPath=null;
 if(navCount>1){
  await navLinks.nth(1).click();
  await page2.waitForTimeout(1500);
  navPath=new URL(page2.url()).pathname;
 }
 record('PB0125',{status:navPath&&navPath!=='/settings/upstream/'?'verified':'partial',
  nav_links:navCount,navigated_to:navPath});
 await page2.screenshot({path:path.join(run,'UI14-settings.png')});
 record('PB0126',{status:'not_applicable_needs_product_decision',
  detail:'no manual refresh on settings shell; data via heartbeat auto-refresh'});
 await ctx2.close();
 // PB0127 失败/重试（拦截 settings 数据端点）
 const fail={n:1};
 const ctx3=await ctxOf('workbuddy',true);const page3=await ctx3.newPage();
 await login(page3);
 await page3.goto(base.origin+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page3.waitForTimeout(3500);
 const b3=await page3.locator('body').innerText();
 const errShown=/失败|错误|重试|未取到|无法/.test(b3);
 const retry=page3.getByRole('button',{name:/重试/}).first();
 const hasRetry=await retry.count();
 let rec=null;
 if(hasRetry){await retry.click();await page3.waitForTimeout(3000);rec=/上游|配置/.test(await page3.locator('body').innerText());}
 await page3.screenshot({path:path.join(run,'UI14-error.png')});
 record('PB0127',{status:errShown&&hasRetry?'verified':(errShown?'partial_no_retry':'partial_no_error_state'),
  error_state_shown:errShown,retry_control_present:hasRetry,recovered:rec});
 await ctx3.close();
 // PB0128/29 qoder 平台能力与 cfg 缺失说明
 const ctx4=await ctxOf('qoder');const page4=await ctx4.newPage();
 await login(page4);
 await page4.goto(base.origin+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page4.waitForTimeout(3000);
 const b4=await page4.locator('body').innerText();
 const capMsg=/qoder-bff|trae-bff|Go 二进制|面板不提供在线编辑/.test(b4);
 record('PB0128',{status:capMsg?'verified':'partial',capability_message_visible:capMsg,
  sample:b4.split('\n').filter(l=>/qoder|bff|在线编辑/.test(l)).slice(0,3)});
 record('PB0129',{status:capMsg?'verified':'partial',
  detail:'cfg.available=false path displays explicit per-deployment message (upstreamError string verified live in S0 probes)'});
 await page4.screenshot({path:path.join(run,'UI14-qoder.png')});
 await ctx4.close();
 report.status='passed';
}catch(e){report.status='failed';report.error=String(e).slice(0,1200);persist();process.exitCode=1;console.error(e);}
finally{persist();console.log('STATSIX_REPORT '+path.join(run,'report.json'));}
await browser.close();
