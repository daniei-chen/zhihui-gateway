// Shared harness for S2 focused interaction verifiers (local disposable fixture only).
// Pattern (from keys_interactions.mjs): real interaction -> observed API ->
// authority readback -> persistence -> negative path -> honest per-row record.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

export function makeHarness(fixturePath,schema,tag){
 const require=createRequire(import.meta.url);
 const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH||
  'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
 const fixture=JSON.parse(fs.readFileSync(fixturePath,'utf8'));
 const base=new URL(fixture.host_url);
 if(fixture.production_connected!==false||base.hostname!=='127.0.0.1'||base.port!=='18443')
  throw new Error('Disposable local browser fixture required');
 const run=path.resolve('evidence/browser/'+tag+'-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
 fs.mkdirSync(run,{recursive:true});
 const report={schema,source_sha256:fixture.source_sha256,production_connected:false,
  status:'running',interactions:{},notes:[]};
 const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
 persist();console.log(tag.toUpperCase()+'_RUN '+run);
 process.on('unhandledRejection',e=>{report.notes.push('unhandledRejection: '+String(e).slice(0,200));persist();});
 const browserP=chromium.launch({headless:true});
 const out={base,run,report,persist,
  record(id,entry){report.interactions[id]=entry;persist();},
  note(s){report.notes.push(String(s).slice(0,300));persist();}};
 out.browser=()=>browserP;
 out.newCtx=async platform=>{
  const browser=await browserP;
  const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
   viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']});
  await context.route('**/*',async route=>{
   const url=new URL(route.request().url());
   if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();
   else await route.continue();
  });
  if(platform)await context.addInitScript(({platform})=>{
   localStorage.setItem('qtg.platform',platform);
   localStorage.setItem('workbuddy-manager:locale','zh-CN');
  },{platform});
  else await context.addInitScript(()=>{localStorage.setItem('workbuddy-manager:locale','zh-CN');});
  return context;
 };
 out.login=async(page,username='gateway-admin',password='synthetic-password')=>{
  await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
  await page.waitForFunction(()=>[...document.forms].some(f=>
   Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),
   null,{timeout:30000});
  await page.locator('#username').fill(username);
  await page.locator('#password').fill(password);
  const [resp]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
   page.locator('button[type="submit"]').click(),
  ]);
  if(resp.status()!==200)throw new Error('login failed '+resp.status());
  return resp;
 };
 // authority readback via authenticated API (same context session)
 out.api=async(ctx,method,p,data,platform='workbuddy')=>{
  const r=await ctx.request.fetch(base.origin+p,{method,data:data===undefined?undefined:JSON.stringify(data),
   headers:{'X-QTG-Platform':platform,Origin:base.origin,'Content-Type':'application/json'}});
  let body=null;try{body=await r.json();}catch{}
  return {status:r.status(),body};
 };
 out.finish=async()=>{
  const counts={};
  for(const e of Object.values(report.interactions))counts[e.status]=(counts[e.status]||0)+1;
  report.counts=counts;
  report.status=report.error?'failed_with_records':'completed';
  persist();
  await (await browserP).close();
  console.log(tag.toUpperCase()+'_REPORT '+path.join(run,'report.json')+' '+JSON.stringify(counts));
  return report;
 };
 return out;
}

// wait for an API call fired by a UI action; returns [response, promise resolved]
export function watch(page,pathname,method='GET'){
 return page.waitForResponse(r=>new URL(r.url()).pathname===pathname&&r.request().method()===method,{timeout:45000});
}
export const snap=()=>Date.now();
export function apisSince(page,mark,limit=8){
 return page.__apiLog.slice(mark).slice(0,limit);
}
export function trackApis(page){
 page.__apiLog=[];
 page.on('request',r=>{const u=new URL(r.url());
  if(u.pathname.startsWith('/api/'))page.__apiLog.push(r.method()+' '+u.pathname+u.search);});
 return ()=>page.__apiLog.length;
}
