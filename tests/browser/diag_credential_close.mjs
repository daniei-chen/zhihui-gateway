// Diagnostic: does the one-time credential leave the DOM after Escape, and when?
// Read-only reproduction of the keys_crud failure path against the local fixture.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false || base.hostname!=='127.0.0.1' || base.port!=='18443')
 throw new Error('Disposable local browser fixture required');
const run=path.resolve('evidence/browser/diag-close-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-diag-credential-close/v1',source_sha256:fixture.source_sha256,production_connected:false,cases:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const browser=await chromium.launch({headless:true});
try{
 const platform='workbuddy';
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol) && url.origin!==base.origin)await route.abort();
  else await route.continue();
 });
 await context.addInitScript(({platform})=>{
  localStorage.setItem('qtg.platform',platform);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');
 },{platform});
 const page=await context.newPage();
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(form=>
  Object.keys(form).some(key=>key.startsWith('__reactProps') && typeof form[key]?.onSubmit==='function')),
  null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login' && r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
 const headers={'X-QTG-Platform':platform,Origin:base.origin};
 await page.goto(base.origin+'/keys/',{waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'新建密钥',exact:true}).first().click();
 const form=page.getByRole('dialog',{name:'新建密钥'});
 const name='qa-diag-'+crypto.randomUUID().slice(0,8);
 await form.locator('#key-name').fill(name);
 await form.getByRole('checkbox',{name:'WorkBuddy',exact:true}).setChecked(true);
 const [created]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys' && r.request().method()==='POST',{timeout:45000}),
  form.getByRole('button',{name:'创建',exact:true}).click(),
 ]);
 const body=await created.json();
 const item={platform,key_id:body.id,samples:[]};
 report.cases.push(item);
 const issued=page.getByRole('dialog',{name:'密钥创建成功'});
 await issued.waitFor({timeout:15000});
 item.dialog_role_count_before=await page.getByRole('dialog').count();
 await page.keyboard.press('Escape');
 const t0=Date.now();
 for(const delayMs of [0,150,400,1000]){
  if(delayMs)await page.waitForTimeout(delayMs-(Date.now()-t0>delayMs?delayMs:Date.now()-t0));
  item.samples.push({after_ms:Date.now()-t0,
   credential_text_count:await page.getByText(body.key,{exact:true}).count(),
   success_dialog_count:await page.getByRole('dialog',{name:'密钥创建成功'}).count(),
   any_dialog_count:await page.getByRole('dialog').count()});
 }
 await page.screenshot({path:path.join(run,'after-escape.png'),fullPage:true});
 // cleanup the created key through the authority API
 const del=await context.request.delete(base.origin+'/api/keys/'+body.id,{headers});
 item.cleanup_delete_status=del.status();
 persist();
 console.log(JSON.stringify(item,null,2));
 console.log('DIAG_RUN '+run);
}catch(e){report.error=String(e);persist();console.error(String(e));process.exitCode=1;}
finally{await browser.close();}
