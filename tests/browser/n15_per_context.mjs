// N15 可信测量：固定在**单个上下文**内连续导航（登录一次），避免每次重建上下文+重新登录
// 把"二次导航水合"的放大效应降到最低。这是评估任何修复前后差异的前提。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/n15-perctx-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-per-context/v1',source_sha256:fixture.source_sha256,
 production_connected:false,method:'login once per context, then N in-context navigations',
 cases:{},functional:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const PAGES=['/models/','/security/','/dashboard/'];
const PLATFORMS=['workbuddy','qoder','trae'];
const ROUNDS=30;   // 每个 (page,platform) 在同一上下文内导航 30 次

for(const platform of PLATFORMS){
 const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
  viewport:{width:1440,height:1000}});
 await ctx.addInitScript(({p})=>{localStorage.setItem('qtg.platform',p);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');},{p:platform});
 const page=await ctx.newPage();
 let hits=0; const samples=[];
 try{
  // 登录一次
  await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
  await page.locator('#username').fill('gateway-admin');
  await page.locator('#password').fill('synthetic-password');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(1500);

  // 功能检查：切换器 aria-selected 应指向持久化平台
  await page.goto(base.origin+'/models/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  report.functional[platform]=await page.evaluate(()=>{
   const tabs=[...document.querySelectorAll('[role=tab]')];
   return {all:tabs.map(t=>(t.textContent||'').trim()),
    selected:tabs.filter(t=>t.getAttribute('aria-selected')==='true')
      .map(t=>(t.textContent||'').trim())};});

  // 在同一上下文内连续导航 N 次（每次先回 dashboard 再进目标页，模拟真实点击）
  for(const target of PAGES){
   let pageHits=0;
   for(let i=0;i<ROUNDS;i++){
    const errs=[];
    const onErr=e=>errs.push(String(e.message||e).slice(0,200));
    page.on('pageerror',onErr);
    const onConsole=m=>{if(/418/.test(m.text()))errs.push(m.text().slice(0,200));};
    page.on('console',onConsole);
    try{
     await page.goto(base.origin+'/dashboard/',{waitUntil:'domcontentloaded',timeout:30000});
     await page.waitForTimeout(250);
     await page.goto(base.origin+target,{waitUntil:'domcontentloaded',timeout:30000});
     await page.waitForTimeout(900);
    }catch(e){}
    page.off('pageerror',onErr);page.off('console',onConsole);
    if(errs.some(e=>/418/.test(e))){pageHits++;hits++;
     if(samples.length<4)samples.push({page:target,attempt:i+1,err:errs[0]});}
   }
   report.cases[platform+'|'+target]={hits:pageHits,of:ROUNDS};
   persist();
   console.log('PERCTX',platform,target,pageHits+'/'+ROUNDS);
  }
 }catch(e){report.cases[platform]={error:String(e).slice(0,200)};}
 await ctx.close();
}
report.total_hits=Object.values(report.cases).reduce((n,c)=>n+(c.hits||0),0);
report.attempts=PLATFORMS.length*PAGES.length*ROUNDS;
report.samples=report.samples||[];
report.verdict=report.total_hits===0?'NO_418':`HITS_${report.total_hits}`;
persist();
console.log('PERCTX_TOTAL',report.total_hits+'/'+report.attempts);
console.log('FUNC',JSON.stringify(report.functional));
console.log('RUN '+run);
await browser.close();
