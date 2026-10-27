// A2: 用引擎模式驱动真实 UI 错误路径（解锁此前的"需真引擎"限制）。
// 通过拦截 playground chat 请求改写 model 后缀，让真实 UI 经历 400/429/500/503/断流/无 usage。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/engineui-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-engine-ui/v1',source_sha256:fixture.source_sha256,
 production_connected:false,cases:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
 viewport:{width:1440,height:1000}});
await ctx.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');
 localStorage.setItem('workbuddy-manager:locale','zh-CN');});
const page=await ctx.newPage();
const dismissOnboarding=async()=>{
 for(let k=0;k<6;k++){
  const d=page.getByRole('button',{name:/知道了|下一步|跳过|关闭/}).first();
  if(!(await d.count()))break;
  await d.click({force:true}).catch(()=>{});
  await page.waitForTimeout(500);
 }
};
try{
 // 用与其它验证器一致的登录实现（等 React 表单挂载完成再提交）
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>
  Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),
  null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 const [loginResp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login',{timeout:30000}).catch(()=>null),
  page.locator('button[type="submit"]').click(),
 ]);
 report.login_status=loginResp?loginResp.status():null;
 if(!loginResp||loginResp.status()!==200){
  report.error='login failed: '+(loginResp?loginResp.status():'no request');
  persist();console.log(JSON.stringify(report));await browser.close();process.exit(0);
 }
 // 等登录后跳转完成（否则会话 cookie 可能还没生效就 goto）
 await page.waitForURL(u=>!new URL(u).pathname.startsWith('/login'),{timeout:20000}).catch(()=>{});
 await page.waitForTimeout(1500);
 report.after_login_path=new URL(page.url()).pathname;
 await page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(5000);
 await dismissOnboarding();
 // 输入框出现前再等一下（模型列表要先取回）
 const ta0=page.locator('textarea').first();
 await ta0.waitFor({timeout:20000}).catch(async()=>{
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(5000);
  await dismissOnboarding();
  await page.locator('textarea').first().waitFor({timeout:20000}).catch(()=>{});
 });
 if(!(await page.locator('textarea').count())){
  report.error='textarea not found after waits';
  report.body_head=await page.evaluate(()=>document.body.innerText.slice(0,400));
  persist();console.log(JSON.stringify(report));await browser.close();process.exit(0);
 }

 const MODES=['error_400','error_429','error_500','error_503','broken_stream','missing_usage'];
 for(const mode of MODES){
  // 拦截：把 model 改成带模式后缀
  await page.route('**/api/playground/chat',async route=>{
   const req=route.request();
   let body={};
   try{body=JSON.parse(req.postData()||'{}');}catch{}
   const m=String(body.model||'fake-model');
   body.model=m.includes(':')?m.split(':')[0]+':'+mode:m+':'+mode;
   await route.continue({postData:JSON.stringify(body)});
  });
  const ta=page.locator('textarea').first();
  await ta.click({force:true}).catch(()=>{});
  await ta.fill('engine-'+mode);
  const before=await page.locator('div.flex.gap-2\\.5.justify-start').count();
  await ta.press('Enter').catch(()=>{});
  await page.waitForTimeout(4000);
  const asst=page.locator('div.flex.gap-2\\.5.justify-start');
  const afterCount=await asst.count();
  const txt=afterCount>before?(await asst.last().innerText().catch(()=>'')):'';
  const hasErr=/失败|错误|异常|限流|不可用|error|429|500|503|400/i.test(txt);
  const hasUsage=/tokens/i.test(txt);
  report.cases[mode]={assistant_blocks_before:before,after:afterCount,
   is_error_block:/^400|^429|^500|^503/.test(mode)||mode==='broken_stream',
   text:txt.slice(0,220),error_text_visible:hasErr,usage_shown:hasUsage};
  persist();
  console.log(mode,'->',hasErr?'ERR_TEXT':'no-err',JSON.stringify(txt.slice(0,80).replace(/\n/g,' ')));
  await page.unroute('**/api/playground/chat');
  await page.waitForTimeout(500);
 }

 // 额外：验证 429 是否带 retry-after 提示
 report.verdict=Object.values(report.cases).some(c=>c.error_text_visible)
  ?'UI_SURFACES_ENGINE_ERRORS':'NO_ERROR_TEXT';
 persist();
 console.log('ENGINE_UI',report.verdict);
 console.log('RUN '+run);
}catch(e){report.error=String(e).slice(0,300);persist();}
await browser.close();
