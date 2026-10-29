// One-shot DOM dump of the keys edit dialog to fix selector targeting.
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
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
 await page.goto(base.origin+'/keys/',{waitUntil:'domcontentloaded'});
 // create a throwaway key to have a row
 await page.getByRole('button',{name:'新建密钥',exact:true}).first().click();
 const form=page.getByRole('dialog',{name:'新建密钥'});
 await form.locator('#key-name').fill('qa-diag-'+crypto.randomUUID().slice(0,6));
 await form.getByRole('checkbox',{name:'WorkBuddy',exact:true}).setChecked(true);
 const [created]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys'&&r.request().method()==='POST',{timeout:45000}),
  form.getByRole('button',{name:'创建',exact:true}).click(),
 ]);
 const {id}=await created.json();
 await page.keyboard.press('Escape');
 await page.waitForTimeout(800);
 const name=(await created.json()).name||'';
 const row=page.getByRole('row').filter({has:page.getByText('qa-diag-',{exact:false})});
 await row.getByTitle('编辑',{exact:true}).click();
 const edit=page.getByRole('dialog',{name:'编辑密钥'});
 await edit.waitFor({timeout:10000});
 const dump=await edit.evaluate(dlg=>{
  const out={buttons:[],combos:[],inputs:[],textareas:[],labels:[]};
  dlg.querySelectorAll('button').forEach(b=>out.buttons.push({text:b.textContent.trim().slice(0,30),role:b.getAttribute('role'),aria:b.getAttribute('aria-expanded')!==null?'expandable':null,title:b.getAttribute('title')||''}));
  dlg.querySelectorAll('[role=combobox]').forEach(c=>out.combos.push(c.textContent.trim().slice(0,40)));
  dlg.querySelectorAll('input').forEach(i=>out.inputs.push({type:i.type,id:i.id,placeholder:i.placeholder||'',value:String(i.value).slice(0,12)}));
  dlg.querySelectorAll('textarea').forEach(t=>out.textareas.push({id:t.id,placeholder:(t.placeholder||'').slice(0,30)}));
  dlg.querySelectorAll('label').forEach(l=>out.labels.push(l.textContent.trim().slice(0,40)));
  return out;
 });
 console.log(JSON.stringify(dump,null,1));
 await context.request.delete(base.origin+'/api/keys/'+id);
 await browser.close();
}catch(e){console.error(String(e));process.exitCode=1;await browser.close();}
