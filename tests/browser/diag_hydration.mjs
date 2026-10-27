// Diagnostic for N15 (React #418 hydration mismatch on /models/ and /security/):
// capture body text at the earliest post-DOMContentLoaded tick (before API data lands)
// for each platform, and diff against the raw SSG HTML text.
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
const run=path.resolve('evidence/browser/diag-hydration-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-diag-hydration/v1',source_sha256:fixture.source_sha256,production_connected:false,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const targets=[
 {page:'/models/',platforms:['workbuddy','qoder']},
 {page:'/security/',platforms:['workbuddy']},
];
try{
 // 1) raw SSG text per page (JS disabled => exactly what React hydrates against)
 const ssgText={};
 for(const t of targets){
  const ctx=await browser.newContext({ignoreHTTPSErrors:true,javaScriptEnabled:false,viewport:{width:1440,height:1000}});
  const p=await ctx.newPage();
  await p.goto(base.origin+t.page,{waitUntil:'load',timeout:30000});
  ssgText[t.page]=await p.evaluate(()=>document.body.innerText);
  await ctx.close();
 }
 // 2) earliest client text per (page, platform): snapshot at first rAF after DOMContentLoaded
 for(const t of targets){
  for(const platform of t.platforms){
   const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
   await ctx.addInitScript(({platform})=>{
    localStorage.setItem('qtg.platform',platform);
    localStorage.setItem('workbuddy-manager:locale','zh-CN');
    // earliest possible snapshot: right after DOMContentLoaded fires, before React
    // hydration even starts. Second snapshot ~250ms later (post-hydration, pre-API).
    window.__zhSnap={};
    document.addEventListener('DOMContentLoaded',()=>{
     requestAnimationFrame(()=>{window.__zhSnap.t0=document.body?document.body.innerText:'<no body>';});
     setTimeout(()=>{window.__zhSnap.t250=document.body?document.body.innerText:'<no body>';},250);
    });
   },{platform});
   const p=await ctx.newPage();
   const pageErrors=[];
   p.on('pageerror',e=>pageErrors.push(String(e.message).slice(0,200)));
   await p.goto(base.origin+t.page,{waitUntil:'commit',timeout:30000});
   await p.waitForFunction(()=>window.__zhSnap&&window.__zhSnap.t250!==undefined,null,{timeout:15000});
   const snaps=await p.evaluate(()=>({t0:window.__zhSnap.t0,t250:window.__zhSnap.t250}));
   const ssgLines=ssgText[t.page].split('\n').map(s=>s.trim()).filter(Boolean);
   const diff250=snaps.t250.split('\n').map(s=>s.trim()).filter(Boolean).filter(l=>!ssgLines.includes(l));
   report.cases.push({page:t.page,platform,
    page_errors:pageErrors,
    ssg_line_count:ssgLines.length,t0_like_ssg:snaps.t0.split('\n').map(s=>s.trim()).filter(Boolean).length,
    client_only_lines_at_250ms:diff250.slice(0,25)});
   persist();
   await ctx.close();
  }
 }
 console.log(JSON.stringify(report.cases,null,1));
 console.log('DIAG_RUN '+run);
}finally{await browser.close();persist();}
