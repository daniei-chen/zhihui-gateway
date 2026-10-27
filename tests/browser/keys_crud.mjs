// Real browser writes and independent authoritative API readback. Local QA only.
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
const run=path.resolve('evidence/browser/keys-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-key-crud/v1',source_sha256:fixture.source_sha256,
 verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
 production_connected:false,status:'running',cases:[],verification_level:'UI_API_authoritative_persistence_readback',
 limitations:['No production execution','No populated engine account or inference acceptance in this suite']};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('KEYS_RUN '+run);
const browser=await chromium.launch({headless:true});
try{
 for(const platform of ['workbuddy','qoder','trae']){
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
  const item={platform,steps:[],status:'running',browser_errors:[]};report.cases.push(item);persist();
  const step=value=>{item.steps.push(value);persist();};
  page.on('pageerror',e=>item.browser_errors.push(String(e.message).slice(0,400)));
  const headers={'X-QTG-Platform':platform,Origin:base.origin};
  let id=null;
  const check=(condition,message)=>{if(!condition)throw new Error(message);};
  const readAuthority=async()=>{
   const response=await context.request.get(base.origin+'/api/keys');
   check(response.status()===200,'Authority list status '+response.status());
   const list=await response.json();check(Array.isArray(list),'Authority list contract');return list;
  };
  const action=async(method,pathname,click)=>{
   const [result]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname===pathname && r.request().method()===method,{timeout:45000}),
    click(),
   ]);
   check(result.status()===200,'UI operation '+method+' '+pathname+' status '+result.status());return result;
  };
  try{
   await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
   // Wait for hydration before typing into controlled inputs; otherwise React
   // can overwrite early DOM input while attaching the submit handler.
   await page.waitForFunction(()=>[...document.forms].some(form=>
    Object.keys(form).some(key=>key.startsWith('__reactProps') && typeof form[key]?.onSubmit==='function')),
    null,{timeout:30000});
   await page.locator('#username').fill('gateway-admin');
   await page.locator('#password').fill('synthetic-password');
   await action('POST','/api/login',()=>page.locator('button[type="submit"]').click());
   step('real_UI_login');
   if(platform!=='workbuddy'){
    const diagnostic=await context.request.get(base.origin+'/api/v1/system/configuration',{headers});
    check(diagnostic.status()===200,'Fixture configuration unavailable');
    check((await diagnostic.json()).configuration.settings.QTG_CHECKIN_SCHEDULER_ENABLED?.value===false,
          'Fixture scheduler is not explicitly disabled');
    item.fixture_scheduler_disabled=true;
   }
   await page.goto(base.origin+'/keys/',{waitUntil:'domcontentloaded'});
   await page.getByRole('button',{name:'新建密钥',exact:true}).first().click();
   const form=page.getByRole('dialog',{name:'新建密钥'});
   const name='qa-ui-'+platform+'-'+crypto.randomUUID().slice(0,8);
   await form.locator('#key-name').fill(name);
   for(const [target,label] of [['workbuddy','WorkBuddy'],['qoder','Qoder'],['trae','Trae']])
    await form.getByRole('checkbox',{name:label,exact:true}).setChecked(target===platform);
   const created=await action('POST','/api/keys',()=>form.getByRole('button',{name:'创建',exact:true}).click());
   const body=await created.json();id=body.id;check(Number.isInteger(id),'Created key ID missing');
   const issued=page.getByRole('dialog',{name:'密钥创建成功'});
   await issued.waitFor({timeout:15000});
   check(Boolean(body.key),'One-time credential missing');
   check((await issued.locator('code').first().innerText()).trim()===body.key,
         'Displayed one-time credential differs from the issued credential');
   await page.screenshot({path:path.join(run,platform+'-created.png'),
    mask:[page.locator('code,pre,input[type="password"],input[readonly]')]});
   await page.keyboard.press('Escape');
   // The dialog unmounts after its close animation (observed 0.4-1s on the QA
   // VM); poll instead of sampling once so a lingering credential still fails.
   const credentialGone=async()=>{
    const deadline=Date.now()+10000;
    while(Date.now()<deadline){
     if(await page.getByText(body.key,{exact:true}).count()===0)return true;
     await page.waitForTimeout(100);
    }
    return false;
   };
   check(await credentialGone(),'Credential remains in the DOM 10s after closing');
   let stored=(await readAuthority()).find(k=>k.id===id);
   check(stored?.name===name,'Created name differs in authority');
   check(JSON.stringify(stored.platforms)==JSON.stringify([platform]),'Platform authority differs');
   check(!stored.key,'Plain key unexpectedly persisted in listing');
   await page.reload({waitUntil:'domcontentloaded'});
   await page.getByRole('row').filter({has:page.getByText(name,{exact:true})}).waitFor({timeout:20000});
   check(await page.getByText(body.key,{exact:true}).count()===0,'Credential reappeared after reload');
   item.key_id=id;step('UI_create_one_time_credential_and_authority_platform_readback');
   let row=page.getByRole('row').filter({has:page.getByText(name,{exact:true})});
   await row.getByTitle('编辑',{exact:true}).click();
   const edit=page.getByRole('dialog',{name:'编辑密钥'});
   await edit.locator('#key-name').fill(name+'-edited');
   await action('PATCH','/api/keys/'+id,()=>edit.getByRole('button',{name:'保存',exact:true}).click());
   stored=(await readAuthority()).find(k=>k.id===id);
   check(stored?.name===name+'-edited','Edited name not persisted');step('UI_edit_and_authority_readback');
   row=page.getByRole('row').filter({has:page.getByText(name+'-edited',{exact:true})});
   await action('PATCH','/api/keys/'+id,()=>row.getByTitle('停用',{exact:true}).click());
   check(!(await readAuthority()).find(k=>k.id===id)?.enabled,'Disable not persisted');
   await action('PATCH','/api/keys/'+id,()=>row.getByTitle('启用',{exact:true}).click());
   check(Boolean((await readAuthority()).find(k=>k.id===id)?.enabled),'Enable not persisted');
   step('UI_disable_enable_and_authority_readback');
   await row.getByTitle('删除',{exact:true}).click();
   const confirm=page.getByRole('alertdialog');
   await action('DELETE','/api/keys/'+id,()=>confirm.getByRole('button',{name:'删除',exact:true}).click());
   check(!(await readAuthority()).some(k=>k.id===id),'Deleted key remains');id=null;
   step('UI_delete_confirmation_and_authority_readback');
   check(item.browser_errors.length===0,'Browser exception');item.status='passed';
  }catch(error){
   item.status='failed';item.failure=String(error.message).slice(0,1200);process.exitCode=1;
   item.failure_path=new URL(page.url()).pathname;
   item.failure_headings=await page.locator('h1,h2').allTextContents().catch(()=>[]);
   await page.screenshot({path:path.join(run,platform+'-failure.png'),
    mask:[page.locator('code,pre,input,textarea')]}).catch(()=>{});
  }
  finally{
   if(id!==null){
    const cleanup=await context.request.delete(base.origin+'/api/keys/'+id,{headers});
    item.failure_cleanup_status=cleanup.status();
   }
   await context.close();persist();console.log(JSON.stringify({platform,status:item.status,steps:item.steps.length,failure:item.failure}));
  }
 }
 report.status=report.cases.every(c=>c.status==='passed')?'passed':'failed';
}finally{await browser.close();persist();console.log('KEYS_REPORT '+path.join(run,'report.json'));}
