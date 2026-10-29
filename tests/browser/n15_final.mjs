// N15 最终定位：不用 addInitScript（它会改变脚本时序、放大竞态）。
// 只在命中 #418 后立刻读 DOM 状态，并与"同一次加载的正常 SSG 快照"对比。
// 关键：区分「body 为空」与「body 有内容但结构不同」两种 #418 成因。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/n15-final-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-final/v1',source_sha256:fixture.source_sha256,
 production_connected:false,cases:[],hits:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});

// 先取该页的 SSG 基准（JS 关闭，纯服务端产物）
async function ssgBaseline(pagePath){
 const ctx=await browser.newContext({ignoreHTTPSErrors:true,javaScriptEnabled:false});
 const p=await ctx.newPage();
 // 未登录也能拿到 SSG 文件（中间件只保护 API）；若被重定向则跟随
 await p.goto(base.origin+pagePath,{waitUntil:'domcontentloaded'}).catch(()=>{});
 const r=await p.evaluate(()=>{
  const tags=[];const walk=(el,d)=>{if(d>8||tags.length>200)return;
   if(['SCRIPT','STYLE','LINK','META'].includes(el.tagName))return;
   tags.push(d+':'+el.tagName.toLowerCase());for(const c of el.children)walk(c,d+1);};
  if(document.body)walk(document.body,0);
  return {tags,bodyChildren:document.body?document.body.children.length:0,
   bodyLen:document.body?document.body.innerHTML.length:0};});
 await ctx.close();
 return r;
}

for(const [pagePath,platform] of [['/models/','qoder'],['/models/','trae'],['/security/','trae']]){
 const ssg=await ssgBaseline(pagePath);
 report.cases.push({page:pagePath,platform,ssg_tags:ssg.tags.slice(0,40),
  ssg_body_children:ssg.bodyChildren,ssg_body_len:ssg.bodyLen});
 let found=false;
 for(let i=0;i<25&&!found;i++){
  const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
   viewport:{width:1440,height:1000}});
  await ctx.addInitScript(({platform})=>{
   localStorage.setItem('qtg.platform',platform);
   localStorage.setItem('workbuddy-manager:locale','zh-CN');
  },{platform});
  const p=await ctx.newPage();
  const errs=[];
  p.on('pageerror',e=>errs.push(String(e.message).slice(0,300)));
  p.on('console',m=>{if(/418/.test(m.text()))errs.push('C:'+m.text().slice(0,300));});
  try{
   await p.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
   await p.locator('#username').fill('gateway-admin');
   await p.locator('#password').fill('synthetic-password');
   await p.locator('button[type="submit"]').click();
   await p.waitForTimeout(400);
   await p.goto(base.origin+pagePath,{waitUntil:'domcontentloaded',timeout:30000});
   const hit=errs.some(e=>/418/.test(e));
   if(hit){
    // 命中瞬间的 DOM 状态 + 稍后稳定态
    const snap=await p.evaluate(()=>{
     const tags=[];const walk=(el,d)=>{if(d>8||tags.length>200)return;
      if(['SCRIPT','STYLE','LINK','META'].includes(el.tagName))return;
      tags.push(d+':'+el.tagName.toLowerCase());for(const c of el.children)walk(c,d+1);};
     if(document.body)walk(document.body,0);
     return {readyState:document.readyState,
      bodyChildren:document.body?document.body.children.length:0,
      bodyLen:document.body?document.body.innerHTML.length:0,tags};});
    report.hits.push({page:pagePath,platform,attempt:i+1,errors:errs.slice(0,2),
     at_hit:{readyState:snap.readyState,bodyChildren:snap.bodyChildren,
      bodyLen:snap.bodyLen,tags:snap.tags.slice(0,40)},
     ssg_bodyChildren:ssg.bodyChildren,ssg_bodyLen:ssg.bodyLen});
    found=true;
   }
  }catch(e){}
  await ctx.close();
 }
 persist();
 console.log(pagePath,platform,found?'HIT':'no-hit');
 if(found){
  const h=report.hits.at(-1);
  console.log('  at_hit:',JSON.stringify({rs:h.at_hit.readyState,bc:h.at_hit.bodyChildren,bl:h.at_hit.bodyLen}),
   ' ssg:',JSON.stringify({bc:h.ssg_bodyChildren,bl:h.ssg_bodyLen}));
 }
}
persist();console.log('FINAL_RUN '+run);
await browser.close();
