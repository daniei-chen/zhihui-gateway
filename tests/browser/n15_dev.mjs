// N15 定位：在 next dev（非压缩构建）上反复加载目标页面，收集完整的 React 错误文本。
// dev server 的 rewrites 指向不可达后端（ECONNREFUSED），这不影响 hydration 错误——
// #418 发生在 HTML 结构比对，与 API 成败无关。我们用 route 把 /api 直接 fulfill 成
// 登录态友好的最小 JSON，避免页面因 401 跳走。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const devOrigin=process.env.DEV_ORIGIN||'http://127.0.0.1:33001';
const run=path.resolve('evidence/browser/n15-dev-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-n15-dev/v1',dev_origin:devOrigin,production_connected:false,
 cases:[],complete_errors:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
// 最小可用 API 假响应：让页面进入"有数据"分支（这是最可能与 SSG 空态不同的路径）
const FAKE={
 '/api/me':{username:'gateway-admin',role:'admin'},
 '/api/users':[{username:'gateway-admin',role:'admin'},{username:'只读用户',role:'viewer'}],
 '/api/models':[{id:'fake-model'},{id:'fake-model-large'}],
 '/api/security/config':{enabled:false,mode:'blacklist'},
 '/api/security/rules':[],
 '/api/security/logs':[],
 '/api/audit-logs':[],
 '/api/settings/upstream':{available:false,error:'未找到上游配置文件'},
 '/api/settings/model-map':{},
 '/api/system/versions':{current:'v2.8.3'},
 '/api/accounts':{total:0,accounts:[],upstream:{name:'默认上游'}},
 '/api/upstreams':{items:[]},
};
const targets=[
 {page:'/security/',platforms:['workbuddy','qoder','trae']},
 {page:'/models/',platforms:['workbuddy','qoder','trae']},
];
try{
 for(const t of targets){
  for(const platform of t.platforms){
   for(let i=0;i<8;i++){
    const ctx=await browser.newContext({locale:'zh-CN',viewport:{width:1440,height:1000}});
    await ctx.addInitScript(({platform})=>{
     localStorage.setItem('qtg.platform',platform);
     localStorage.setItem('workbuddy-manager:locale','zh-CN');
    },{platform});
    await ctx.route('**/api/**',async route=>{
     const u=new URL(route.request().url());
     const key=Object.keys(FAKE).find(k=>u.pathname===k||u.pathname.startsWith(k+'/'));
     if(key)return route.fulfill({status:200,contentType:'application/json',
      body:JSON.stringify(FAKE[key])});
     return route.fulfill({status:200,contentType:'application/json',body:'{}'});
    });
    const p=await ctx.newPage();
    const errs=[];
    p.on('console',m=>{if(m.type()==='error')errs.push(m.text());});
    p.on('pageerror',e=>errs.push('PAGEERROR '+String(e.message)));
    try{
     await p.goto(devOrigin+t.page,{waitUntil:'load',timeout:60000});
     await p.waitForTimeout(2500);
    }catch(e){errs.push('NAV '+String(e.message).slice(0,300));}
    const hyd=errs.filter(e=>/hydrat|#418|#423|#425|did not match|Text content/i.test(e));
    report.cases.push({page:t.page,platform,attempt:i+1,hydration_errors:hyd.slice(0,3),all:errs.length});
    if(hyd.length&&report.complete_errors.length<6){
     report.complete_errors.push({page:t.page,platform,texts:hyd.slice(0,2)});
    }
    persist();
    if(hyd.length)console.log('HIT',t.page,platform,i+1,hyd[0].slice(0,300).replace(/\n/g,' '));
    await ctx.close();
   }
  }
 }
 report.total_hydration=report.cases.reduce((n,c)=>n+c.hydration_errors.length,0);
 report.verdict=report.total_hydration?'REPRODUCED':'NOT_REPRODUCED';
 persist();
 console.log('N15DEV',report.verdict,'cases',report.cases.length,'hydration_errors',report.total_hydration);
 console.log('RUN '+run);
}finally{await browser.close();persist();}
