// Sweep all browser-ready groups (UI01/07/11/12/15/16/17/18/19/20): per page load,
// control inventory + safe interactions (tabs/filters), honest per-row statuses.
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
const run=path.resolve('evidence/browser/sweep-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-sweep/v1',source_sha256:fixture.source_sha256,production_connected:false,status:'running',pages:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('SWEEP_RUN '+run);
const browser=await chromium.launch({headless:true});
const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
await ctx.route('**/*',async route=>{
 const url=new URL(route.request().url());
 if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
});
await ctx.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');localStorage.setItem('workbuddy-manager:locale','zh-CN');});
const page=await ctx.newPage();
const apis=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push(r.method()+' '+u.pathname+u.search);});
await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
await page.waitForFunction(()=>[...document.forms].some(f=>Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),'',{timeout:30000}).catch(()=>{});
await page.locator('#username').fill('gateway-admin');
await page.locator('#password').fill('synthetic-password');
await Promise.all([
 page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
 page.locator('button[type="submit"]').click(),
]);
const PAGES=[
 ['UI07','/playground/',null],
 ['UI11','/security/',null],
 ['UI12','/red-packets/',null],
 ['UI15','/settings/upstream/',null],
 ['UI16','/settings/models/',null],
 ['UI17','/settings/users/',null],
 ['UI18','/settings/proxy/',null],
 ['UI19','/settings/tokens/',null],
 ['UI20','/settings/system/',null],
 ['UI01','/',null],
];
for(const [ui,route] of PAGES){
 const m=apis.length;const entry={route,loaded:false,apis:[],controls:{},interactions:{}};
 try{
  await page.goto(base.origin+route,{waitUntil:'domcontentloaded',timeout:45000});
  await page.waitForTimeout(3200);
  entry.loaded=await page.locator('h1').first().isVisible().catch(()=>false);
  entry.apis=apis.slice(m).filter(x=>!x.includes('login')).slice(0,12);
  entry.controls={
   buttons:(await page.locator('button:visible').allTextContents()).map(t=>t.trim()).filter(Boolean).slice(0,25),
   tabs:await page.getByRole('tab').count(),
   combobox:await page.getByRole('combobox').count(),
   search:await page.locator('input[type=search], input[placeholder*=搜索], input[placeholder*=筛选]').count(),
   dialogs:await page.getByRole('dialog').count(),
  };
  // safe interaction: click second tab if any
  const tabs=page.getByRole('tab');
  if(await tabs.count()>1){
   const mt=apis.length;
   await tabs.nth(1).click().catch(()=>{});
   await page.waitForTimeout(2000);
   entry.interactions.tab_switch={refetch:apis.slice(mt).slice(0,6),url_after:new URL(page.url()).pathname};
  }
  // safe interaction: search box if any
  const sb=page.locator('input[type=search], input[placeholder*=搜索], input[placeholder*=筛选]').first();
  if(await sb.count()){
   await sb.fill('qa');
   await page.waitForTimeout(1800);
   entry.interactions.search_probe={typed:'qa',rows_after:await page.getByRole('row').count()};
   await sb.fill('');
  }
  await page.screenshot({path:path.join(run,ui+'.png')});
 }catch(e){entry.error=String(e).slice(0,200);}
 report.pages[ui]=entry;persist();
 console.log(ui,entry.loaded?'loaded':'FAILED',entry.controls.buttons?.length||0,'btns');
}
report.status='completed';
persist();console.log('SWEEP_REPORT '+path.join(run,'report.json'));
await browser.close();
