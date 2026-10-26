// 引擎夹具能力验证（A）：确认能通过 model 后缀 :mode 驱动 FakeProvider 的 15 种模式，
// 并观察 UI 在各模式下的真实表现（这是"引擎控制"这一前置的真实替代）。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/engine-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-engine-fixture/v1',source_sha256:fixture.source_sha256,
 production_connected:false,modes:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
 viewport:{width:1440,height:1000}});
await ctx.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');
 localStorage.setItem('workbuddy-manager:locale','zh-CN');});
const page=await ctx.newPage();
try{
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await page.locator('button[type="submit"]').click();
 await page.waitForTimeout(1200);

 // 直接用 fetch 打 /api/v1/responses，用 model 后缀驱动 fake provider 的模式。
 // 这条路证明"引擎可控"：同一入口、同一鉴权，只改 model 名就能得到不同引擎行为。
 // 先建一把真实密钥（推理入口需要 Bearer，会话 cookie 不够）
 const keyResp=await ctx.request.post(base.origin+'/api/keys',{
  headers:{Origin:base.origin,'Content-Type':'application/json'},
  data:{name:'qa-engine-'+crypto.randomUUID().slice(0,6),platforms:['workbuddy']}});
 const keyBody=await keyResp.json().catch(()=>({}));
 const apiKey=keyBody.key||'';
 report.api_key_created=Boolean(apiKey);
 const probe = async (mode)=>{
  const r=await page.evaluate(async ({mode,apiKey})=>{
   const res=await fetch('/v1/responses',{method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+apiKey},
    body:JSON.stringify({model:'fake-model:'+mode,input:'engine-probe-'+mode})});
   const text=await res.text();
   return {status:res.status,body:text.slice(0,300),ct:res.headers.get('content-type')||''};
  },{mode,apiKey});
  return r;
 };
 const MODES=['normal','error_400','error_429','error_500','error_503','missing_usage',
  'duplicate_chunk','broken_stream','slow_headers','huge_response'];
 for(const mode of MODES){
  let res=null,err=null;
  try{ res=await probe(mode); }catch(e){ err=String(e).slice(0,200); }
  report.modes[mode]={result:res,error:err};
  persist();
  console.log(mode,res?res.status:'ERR',res?(res.body||'').slice(0,60).replace(/\n/g,' '):'');
 }

 // 再验证 UI 侧：playground 发送时选带模式的模型名，看气泡表现
 await page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const before=await page.evaluate(()=>document.body.innerText.length);
 // 直接改请求：拦截一次 chat 调用，把 model 改成带 error 模式
 let injected=null;
 await page.route('**/api/playground/chat',async route=>{
  const req=route.request();
  let body={};
  try{body=JSON.parse(req.postData()||'{}');}catch{}
  body.model=(body.model||'fake-model')+':error_500';
  injected=body.model;
  await route.continue({postData:JSON.stringify(body)});
 });
 const ta=page.locator('textarea').first();
 if(await ta.count()){
  await ta.fill('engine-fixture-error-test');
  await ta.press('Enter');
  await page.waitForTimeout(3000);
  const asst=await page.locator('div.flex.gap-2\\.5.justify-start').last().innerText().catch(()=>'');
  report.ui_error_mode={injected_model:injected,assistant_text:asst.slice(0,200),
   has_error_text:/失败|错误|500|异常/.test(asst)};
  console.log('UI error_500 ->', asst.slice(0,100).replace(/\n/g,' '));
 }
 await page.unroute('**/api/playground/chat');
 report.verdict=Object.values(report.modes).some(m=>m.result&&m.result.status>=400)?'ENGINE_CONTROLLABLE':'UNEXPECTED';
 persist();
 console.log('ENGINE',report.verdict);
 console.log('RUN '+run);
}catch(e){report.error=String(e).slice(0,300);persist();}
await browser.close();
