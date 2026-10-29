// N15 hunt: load pages from the local next dev server (full React error text),
// proxying /api/* to the disposable QA fixture. Repeats each page N times with
// fresh contexts to catch the racy hydration mismatch.
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
const devOrigin='http://127.0.0.1:33001';
const run=path.resolve('evidence/browser/diag-hydration-dev-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-diag-hydration-dev/v1',source_sha256:fixture.source_sha256,production_connected:false,dev_origin:devOrigin,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const targets=[
 {page:'/security/',platform:'trae',role:'admin'},
 {page:'/security/',platform:'trae',role:'viewer'},
 {page:'/models/',platform:'trae',role:'admin'},
];
try{
 for(const t of targets){
  for(let i=0;i<4;i++){
   const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
   // proxy every /api call to the disposable fixture, forwarding cookies/headers
   await ctx.route('**/api/**',async route=>{
    const req=route.request();
    const url=new URL(req.url());
    const headers={...req.headers()};
    delete headers.host;
    try{
     const resp=await ctx.request.fetch(base.origin+url.pathname+url.search,{method:req.method(),headers,data:req.postDataBuffer()??undefined,maxRedirects:0});
     await route.fulfill({status:resp.status(),headers:resp.headers(),body:await resp.body()});
    }catch(e){await route.abort();}
   });
   await ctx.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin===devOrigin)await route.continue();
    else if(['http:','https:'].includes(url.protocol))await route.abort();
    else await route.continue();
   });
   await ctx.addInitScript(({platform,role})=>{
    localStorage.setItem('qtg.platform',platform);
    localStorage.setItem('workbuddy-manager:locale','zh-CN');
    if(role==='viewer')sessionStorage.setItem('zh-role','viewer');
   },{platform:t.platform,role:t.role});
   const p=await ctx.newPage();
   const consoleErrors=[];
   p.on('console',m=>{if(m.type()==='error')consoleErrors.push(m.text().slice(0,2500));});
   p.on('pageerror',e=>consoleErrors.push('PAGEERROR '+String(e.message).slice(0,2500)));
   const user=t.role==='viewer'?'只读用户':'gateway-admin';
   // login on the fixture origin first so the proxy carries the session cookie
   const login=await ctx.request.post(base.origin+'/api/login',{data:{username:user,password:'synthetic-password'}});
   const item={page:t.page,platform:t.platform,role:t.role,attempt:i+1,login_status:login.status(),console_errors:[]};
   try{
    await p.goto(devOrigin+t.page,{waitUntil:'load',timeout:45000});
    await p.waitForTimeout(3500);
   }catch(e){item.nav_error=String(e.message).slice(0,200);}
   item.console_errors=consoleErrors.slice(0,4);
   report.cases.push(item);persist();
   await ctx.close();
   console.log(JSON.stringify({page:t.page,platform:t.platform,role:t.role,attempt:i+1,errors:consoleErrors.length,first:(consoleErrors[0]||'').slice(0,160)}));
  }
 }
 console.log('DIAG_RUN '+run);
}finally{await browser.close();persist();}
