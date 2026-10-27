// N15 精确定位：在 #418 命中时，抓取「SSG 原始 HTML 结构」与「客户端首帧结构」
// 做逐节点比对，找出第一个不一致的元素。
//
// 做法：同一个页面加载两次——
//   A) JS 禁用 -> 得到浏览器解析后的 SSG DOM（就是 React 期待的结构）
//   B) 正常加载，在最早时刻（DOMContentLoaded 后立刻）抓 DOM
// 比较两者的「标签序列」（忽略文本内容，只看结构），定位第一处差异并打印上下文。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1')
 throw new Error('local fixture only');
const run=path.resolve('evidence/browser/n15-struct-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-struct/v1',source_sha256:fixture.source_sha256,
 production_connected:false,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});

/** 提取 DOM 的"带路径的标签序列"（用于结构比对） */
const TAG_WALK=`(() => {
  const out = [];
  const walk = (el, path) => {
    if (el.nodeType !== 1) return;
    const tag = el.tagName.toLowerCase();
    // 忽略 script/style/next 内部节点：它们由框架管理，不代表用户可见结构
    if (tag === 'script' || tag === 'style' || tag === 'link' || tag === 'meta') return;
    const role = el.getAttribute('role') || '';
    const key = path + '/' + tag + (role ? '[' + role + ']' : '');
    out.push(key);
    let i = 0;
    for (const c of el.children) { walk(c, key + ':' + (i++)); }
  };
  walk(document.body, 'body');
  return out;
})()`;

for(const [pagePath,platform] of [
 ['/models/','qoder'],['/models/','workbuddy'],['/security/','trae'],['/dashboard/','workbuddy'],
]){
 // A) SSG 结构（JS 关闭）
 let ssg=[];
 {
  const ctx=await browser.newContext({ignoreHTTPSErrors:true,javaScriptEnabled:false,
   locale:'zh-CN',viewport:{width:1440,height:1000}});
  const p=await ctx.newPage();
  try{
   await p.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'}).catch(()=>{});
   // 无 JS 无法登录；直接取 SSG 页面（未登录时中间件可能重定向，故用原始 HTML）
   const resp=await ctx.request.get(base.origin+pagePath,{maxRedirects:0});
   const html=await resp.text();
   // 用浏览器解析这段 HTML（JS 关闭，保证是纯 SSG 结构）
   const scope=await browser.newContext({javaScriptEnabled:false});
   const sp=await scope.newPage();
   await sp.setContent(html,{waitUntil:'domcontentloaded'});
   ssg=await sp.evaluate(TAG_WALK);
   await scope.close();
  }catch(e){report.cases.push({page:pagePath,platform,error:'ssg '+String(e).slice(0,150)});}
  await ctx.close();
 }
 // B) 客户端首帧结构（多次采样，抓最早时刻）
 let best=null;
 for(let i=0;i<12;i++){
  const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
   viewport:{width:1440,height:1000}});
  await ctx.addInitScript(({platform})=>{
   localStorage.setItem('qtg.platform',platform);
   localStorage.setItem('workbuddy-manager:locale','zh-CN');
  },{platform});
  const p=await ctx.newPage();
  const errs=[];
  p.on('pageerror',e=>errs.push(String(e.message)));
  p.on('console',m=>{if(m.type()==='error'&&/418/.test(m.text()))errs.push(m.text());});
  try{
   await p.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
   await p.locator('#username').fill('gateway-admin');
   await p.locator('#password').fill('synthetic-password');
   await p.locator('button[type="submit"]').click();
   await p.waitForTimeout(500);
   // 用原始 HTML 的客户端结构：先取 SSG，再在 JS 环境里读 earliest
   await p.goto(base.origin+pagePath,{waitUntil:'domcontentloaded',timeout:30000});
   const client=await p.evaluate(TAG_WALK);
   const hit=errs.some(e=>/418/.test(e));
   if(!best||hit)best={client,hit,errs:errs.slice(0,2)};
   if(hit)break;
  }catch(e){}
  await ctx.close();
 }
 // 比对
 if(best&&ssg.length){
  const a=ssg,b=best.client;
  let i=0;while(i<a.length&&i<b.length&&a[i]===b[i])i++;
  report.cases.push({page:pagePath,platform,
   ssg_len:a.length,client_len:b.length,
   first_diff_index:i,
   ssg_at_diff:a.slice(Math.max(0,i-3),i+4),
   client_at_diff:b.slice(Math.max(0,i-3),i+4),
   identical:i===a.length&&i===b.length,
   hydration_error_caught:best.hit,
   errors:best.errs});
 }else{
  report.cases.push({page:pagePath,platform,error:'no comparison',ssg_len:ssg.length});
 }
 persist();
 const c=report.cases.at(-1);
 console.log(pagePath,platform,'ssg',c.ssg_len,'client',c.client_len,'diff@',c.first_diff_index,
   c.identical?'IDENTICAL':'DIFF','418:',c.hydration_error_caught);
}
persist();
console.log('STRUCT_RUN '+run);
await browser.close();
