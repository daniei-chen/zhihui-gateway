// Capture the 429 lockout message as displayed by the UI (lock already active;
// blocked attempts raise before record_fail, so this does not extend the lock).
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const run=path.resolve('evidence/browser/lockmsg-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const browser=await chromium.launch({headless:true});
try{
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 await context.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');localStorage.setItem('workbuddy-manager:locale','zh-CN');});
 const page=await context.newPage();
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>
  Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('any-password-while-locked');
 const [resp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
 await page.waitForTimeout(800);
 const bodyText=await page.locator('body').innerText();
 const alert=(await page.locator('[role=alert],.text-destructive,[data-slot=alert]').allTextContents()).join(' | ');
 await page.screenshot({path:path.join(run,'lock-429-ui.png')});
 const out={http:resp.status(),body:await resp.json().catch(()=>null),
  ui_alert_text:alert.slice(0,300),body_excerpt:bodyText.split('\n').filter(l=>l.includes('分钟')||l.includes('失败')||l.includes('频繁')).slice(0,5)};
 fs.writeFileSync(path.join(run,'result.json'),JSON.stringify(out,null,2));
 console.log(JSON.stringify(out,null,1));console.log('LOCKMSG_RUN '+run);
}catch(e){console.error(String(e));process.exitCode=1;}finally{await browser.close();}
