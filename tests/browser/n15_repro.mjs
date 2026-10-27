// N15 复现尝试：对 /models/ /security/ /playground/ 在多平台反复加载，
// 捕获 React #418 与具体不匹配文本。用 40 次/页面提高竞态命中率。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1'||base.port!=='18443')
 throw new Error('local fixture only');
const run=path.resolve('evidence/browser/n15-repro-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-repro/v1',source_sha256:fixture.source_sha256,
 production_connected:false,attempts:40,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const pages=['/models/','/security/','/playground/','/settings/upstream/'];
const platforms=['workbuddy','qoder','trae'];
let hits=0,samples=[];
try{
 for(const page of pages){
  for(const platform of platforms){
   let pageHits=0;
   for(let i=0;i<40;i++){
    const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
     viewport:{width:1440,height:1000}});
    await ctx.addInitScript(({platform})=>{
     localStorage.setItem('qtg.platform',platform);
     localStorage.setItem('workbuddy-manager:locale','zh-CN');
    },{platform});
    const p=await ctx.newPage();
    const errs=[];
    p.on('console',m=>{if(m.type()==='error')errs.push(m.text().slice(0,1200));});
    p.on('pageerror',e=>errs.push('PAGEERROR '+String(e.message).slice(0,1200)));
    try{
     await base; // noop
     await p.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
     await p.locator('#username').fill('gateway-admin');
     await p.locator('#password').fill('synthetic-password');
     await p.locator('button[type="submit"]').click();
     await p.waitForTimeout(600);
     await p.goto(base.origin+page,{waitUntil:'domcontentloaded',timeout:30000});
     await p.waitForTimeout(1800);
    }catch(e){errs.push('NAV '+String(e.message).slice(0,200));}
    const r418=errs.filter(e=>/#418|418/.test(e));
    if(r418.length){
     pageHits++;hits++;
     if(samples.length<8){
      const bodyText=await p.evaluate(()=>document.body.innerText.slice(0,600)).catch(()=> '');
      samples.push({page,platform,attempt:i+1,errors:r418.slice(0,2),body_head:bodyText.replace(/\n+/g,' | ').slice(0,400)});
     }
    }
    await ctx.close();
   }
   report.cases.push({page,platform,hits:pageHits,of:40,rate:(pageHits/40).toFixed(3)});
   persist();
   console.log(page,platform,pageHits+'/40');
  }
 }
 report.total_hits=hits;report.samples=samples;
 report.verdict=hits>0?'REPRODUCED':'NOT_REPRODUCED';
 persist();
 console.log('N15_VERDICT',report.verdict,'hits',hits,'of',pages.length*platforms.length*40);
 console.log('RUN '+run);
}finally{await browser.close();persist();}
