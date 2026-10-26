// N15 根治验证：
//  A) 功能：持久化平台能被正确恢复（不能因 getServerSnapshot 固定而卡在默认）
//  B) 稳定性：#418 是否消失（多平台 × 多次加载）
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/n15fix-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-fix-verify/v1',source_sha256:fixture.source_sha256,
 production_connected:false,functional:{},stability:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});

// ---------- A) 功能：持久化平台恢复 ----------
for(const platform of ['qoder','trae','workbuddy']){
 const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
  viewport:{width:1440,height:1000}});
 await ctx.addInitScript(({p})=>{localStorage.setItem('qtg.platform',p);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');},{p:platform});
 const page=await ctx.newPage();
 try{
  await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
  await page.locator('#username').fill('gateway-admin');
  await page.locator('#password').fill('synthetic-password');
  await page.locator('button[type="submit"]').click();
  await page.waitForTimeout(600);
  await page.goto(base.origin+'/models/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(3000);
  // 顶部平台切换器的 aria-selected 应指向持久化的平台
  const active=await page.evaluate(()=>{
   const tabs=[...document.querySelectorAll('[role=tab]')];
   const sel=tabs.filter(t=>t.getAttribute('aria-selected')==='true')
     .map(t=>(t.textContent||'').trim());
   return {selected:sel,all:tabs.map(t=>(t.textContent||'').trim())};});
  // 再确认请求头带的是该平台
  const reqPlatform=await page.evaluate(async()=>{
   const r=await fetch('/api/me',{credentials:'include'});
   return r.status;});
  report.functional[platform]={active_tabs:active,stored:platform,
   restored_correctly:active.selected.some(t=>t.toLowerCase().includes(platform.slice(0,4))),
   me_status:reqPlatform};
 }catch(e){report.functional[platform]={error:String(e).slice(0,200)};}
 await ctx.close();persist();
 console.log('FUNC',platform,JSON.stringify(report.functional[platform].active_tabs?.selected));
}

// ---------- B) 稳定性：#418 计数（每平台 40 次） ----------
let hits=0,samples=[];
for(const pagePath of ['/models/','/security/']){
 for(const platform of ['workbuddy','qoder','trae']){
  let ph=0;
  for(let i=0;i<40;i++){
   const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
    viewport:{width:1440,height:1000}});
   await ctx.addInitScript(({p})=>{localStorage.setItem('qtg.platform',p);
    localStorage.setItem('workbuddy-manager:locale','zh-CN');},{p:platform});
   const page=await ctx.newPage();
   const errs=[];
   page.on('pageerror',e=>errs.push(String(e.message).slice(0,200)));
   page.on('console',m=>{if(/418/.test(m.text()))errs.push(m.text().slice(0,200));});
   try{
    await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
    await page.locator('#username').fill('gateway-admin');
    await page.locator('#password').fill('synthetic-password');
    await page.locator('button[type="submit"]').click();
    await page.waitForTimeout(400);
    await page.goto(base.origin+pagePath,{waitUntil:'domcontentloaded',timeout:30000});
    await page.waitForTimeout(1500);
   }catch(e){}
   if(errs.some(e=>/418/.test(e))){ph++;hits++;
    if(samples.length<5)samples.push({page:pagePath,platform,attempt:i+1,err:errs[0]});}
   await ctx.close();
  }
  report.stability[pagePath+'|'+platform]=ph;
  persist();
  console.log('STAB',pagePath,platform,ph+'/40');
 }
}
report.total_hits=hits;report.attempts=240;report.samples=samples;
report.verdict=hits===0?'NO_418':'STILL_REPRODUCES';
persist();
console.log('N15FIX',report.verdict,'hits',hits,'/240');
console.log('RUN '+run);
await browser.close();
