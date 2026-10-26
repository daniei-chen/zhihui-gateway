// N15 精确定位 v2：在页面脚本执行**之前**注入 MutationObserver，
// 记录 React 水合期间对 DOM 结构做的第一处修改——那正是 #418 的不匹配点。
//
// 原理：#418 是"服务端 HTML 与客户端首帧不一致"。React 在 DEV 下会尝试 patch
// 并打印差异；在生产压缩构建下只报错码。所以我们自己观察：从 document 初始 HTML
// （服务端产物）到 React 第一次改动之间，哪个节点被替换/移动了。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/n15-mut-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-mutations/v1',source_sha256:fixture.source_sha256,
 production_connected:false,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});

const OBSERVE=`(() => {
  window.__zhMutations = [];
  const pathOf = (n) => {
    const parts = [];
    let el = n.nodeType === 1 ? n : n.parentElement;
    while (el && el !== document.body) {
      const parent = el.parentElement;
      if (!parent) break;
      parts.unshift(el.tagName.toLowerCase() + ':' + Array.prototype.indexOf.call(parent.children, el));
      el = parent;
    }
    return parts.join('/');
  };
  const obs = new MutationObserver((list) => {
    for (const m of list) {
      if (window.__zhMutations.length > 60) return;
      const rec = {type: m.type, target: pathOf(m.target)};
      if (m.type === 'childList') {
        rec.added = [...m.addedNodes].filter(n => n.nodeType === 1).map(n => n.tagName.toLowerCase()).slice(0,6);
        rec.removed = [...m.removedNodes].filter(n => n.nodeType === 1).map(n => n.tagName.toLowerCase()).slice(0,6);
      } else if (m.type === 'attributes') {
        rec.attr = m.attributeName;
        rec.oldValue = m.oldValue;
        rec.newValue = m.target.getAttribute(m.attributeName);
      }
      if (rec.added?.length || rec.removed?.length || rec.type === 'attributes')
        window.__zhMutations.push(rec);
    }
  });
  // 同时观察 documentElement 的属性（lang/class 也是常见不匹配源）
  obs.observe(document, {childList:true, subtree:true, attributes:true, attributeOldValue:true});
  // 兜底：记录 React 首次 hydrate 前就存在的结构（若 observer 装得太晚，这里为空）
  window.__zhHadBodyAtInit = Boolean(document.body);
})()`;

for(const [pagePath,platform] of [['/models/','qoder'],['/models/','trae'],['/security/','trae']]){
 let hitCase=null;
 for(let i=0;i<20&&!hitCase;i++){
  const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
   viewport:{width:1440,height:1000}});
  await ctx.addInitScript(({platform})=>{
   localStorage.setItem('qtg.platform',platform);
   localStorage.setItem('workbuddy-manager:locale','zh-CN');
  },{platform});
  const p=await ctx.newPage();
  const errs=[];
  p.on('pageerror',e=>errs.push(String(e.message).slice(0,600)));
  p.on('console',m=>{const x=m.text();if(/418|423|425|hydrat/i.test(x))errs.push('CONSOLE:'+x.slice(0,600));});
  p.on('console',m=>{if(m.type()==='error'&&/418/.test(m.text()))errs.push(m.text().slice(0,300));});
  try{
   await p.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
   await p.locator('#username').fill('gateway-admin');
   await p.locator('#password').fill('synthetic-password');
   await p.locator('button[type="submit"]').click();
   await p.waitForTimeout(400);
   // 在导航前把 observer 注入到新文档
   await p.addInitScript(OBSERVE);
   await p.goto(base.origin+pagePath,{waitUntil:'domcontentloaded',timeout:30000});
   await p.waitForTimeout(1500);
   const muts=await p.evaluate(()=>window.__zhMutations||[]);
   const has418=errs.some(e=>/418/.test(e));
   if(has418){
    // 只关心水合阶段的改动：前 25 条
    const tags=await p.evaluate(()=>{
      const out=[];
      const walk=(el,d)=>{ if(d>6||out.length>80)return;
        if(['SCRIPT','STYLE','LINK','META'].includes(el.tagName))return;
        out.push(d+':'+el.tagName.toLowerCase()+(el.getAttribute('role')?'[role='+el.getAttribute('role')+']':''));
        for(const c of el.children)walk(c,d+1); };
      walk(document.body,0);return out;});
    hitCase={attempt:i+1,errors:errs.slice(0,3),mutations:muts.slice(0,25),
      body_tags:tags, had_body_at_init:await p.evaluate(()=>window.__zhHadBodyAtInit)};
   }
  }catch(e){}
  await ctx.close();
 }
 report.cases.push({page:pagePath,platform,hit:Boolean(hitCase),...hitCase});
 persist();
 console.log(pagePath,platform,hitCase?`HIT at ${hitCase.attempt}`:'no hit',
   hitCase?JSON.stringify(hitCase.mutations.slice(0,3)):'');
}
persist();console.log('MUT_RUN '+run);
await browser.close();
