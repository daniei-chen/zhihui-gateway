// Diagnose: does Enter submit the login form? Structure + network trace.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
const browser=await chromium.launch({headless:true});
try{
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 await context.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');localStorage.setItem('workbuddy-manager:locale','zh-CN');});
 const page=await context.newPage();
 const requests=[];
 page.on('request',r=>requests.push(r.method()+' '+new URL(r.url()).pathname));
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>
  Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),null,{timeout:30000});
 const structure=await page.evaluate(()=>{
  const form=document.querySelector('form');
  const inForm=[...(form?.querySelectorAll('button')||[])].map(b=>({type:b.type,text:(b.textContent||'').trim()}));
  const all=[...document.querySelectorAll('button')].map(b=>({type:b.type,text:(b.textContent||'').trim(),inside:!!b.closest('form')}));
  return {form_exists:!!form,form_buttons:inForm,all_buttons:all,
   inputs:[...document.querySelectorAll('input')].map(i=>({type:i.type,id:i.id}))};
 });
 console.log(JSON.stringify(structure,null,1));
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 requests.length=0;
 await page.locator('#password').press('Enter');
 await page.waitForTimeout(4000);
 console.log('after enter, requests:',JSON.stringify(requests));
 console.log('url now:',page.url());
 await browser.close();
}catch(e){console.error(String(e));process.exitCode=1;await browser.close();}
