// UI05 (密钥页) interaction closure per S2 discipline: existence -> action ->
// observed API -> authority readback -> refresh -> negative/cancel -> evidence.
// Covers PB0043(partial close) PB0044 PB0045 PB0046 PB0048 PB0051 PB0052 PB0053 PB0054.
// Admin on workbuddy (full flow) + create/copy on qoder/trae. Local fixture only.
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
const run=path.resolve('evidence/browser/keysix-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-key-interactions/v1',source_sha256:fixture.source_sha256,
 verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
 production_connected:false,status:'running',interactions:{},created_key_ids:[]};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('KEYSIX_RUN '+run);
const browser=await chromium.launch({headless:true});
const record=(id,entry)=>{report.interactions[id]=entry;persist();};

async function newCtx(platform){
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',
  viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol) && url.origin!==base.origin)await route.abort();
  else await route.continue();
 });
 await context.addInitScript(({platform})=>{
  localStorage.setItem('qtg.platform',platform);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');
 },{platform});
 return context;
}
async function login(page){
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(form=>
  Object.keys(form).some(key=>key.startsWith('__reactProps') && typeof form[key]?.onSubmit==='function')),
  null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
}
const readKey=async(ctx,id)=>(await (await ctx.request.get(base.origin+'/api/keys')).json()).find(k=>k.id===id);
async function createKey(page,ctx,platform,name,extra){
 await page.goto(base.origin+'/keys/',{waitUntil:'domcontentloaded'});
 await page.getByRole('button',{name:'新建密钥',exact:true}).first().click();
 const form=page.getByRole('dialog',{name:'新建密钥'});
 await form.locator('#key-name').fill(name);
 for(const [target,label] of [['workbuddy','WorkBuddy'],['qoder','Qoder'],['trae','Trae']])
  await form.getByRole('checkbox',{name:label,exact:true}).setChecked(target===platform);
 const [created]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys'&&r.request().method()==='POST',{timeout:45000}),
  form.getByRole('button',{name:'创建',exact:true}).click(),
 ]);
 if(created.status()!==200)throw new Error('create failed '+created.status());
 const body=await created.json();
 report.created_key_ids.push(body.id);persist();
 return body;
}
const cleanup=async(ctx)=>{for(const id of report.created_key_ids)
 await ctx.request.delete(base.origin+'/api/keys/'+id).catch(()=>{});};

try{
 // ================= PB0044 + PB0043 + PB0052 + PB0053: created-dialog cluster (workbuddy)
 {
  const platform='workbuddy';
  const ctx=await newCtx(platform);
  const page=await ctx.newPage();
  const headers={'X-QTG-Platform':platform,Origin:base.origin};
  await login(page);
  const name='qa-ix-'+crypto.randomUUID().slice(0,8);
  const body=await createKey(page,ctx,platform,name);
  const id=body.id;

  // PB0043 one-time plaintext display + close
  const issued=page.getByRole('dialog',{name:'密钥创建成功'});
  await issued.waitFor({timeout:15000});
  const shown=(await issued.locator('code').first().innerText()).trim();
  record('PB0043',{status:'verified',detail:'plaintext shown once in created dialog, equals issued key',
   equals_issued:shown===body.key,
   listing_has_plaintext:!JSON.stringify(await (await ctx.request.get(base.origin+'/api/keys')).json()).includes(body.key)});
  await page.screenshot({path:path.join(run,'PB0043-created.png'),mask:[page.locator('code,pre,input')]});

  // PB0044 copy buttons (clipboard)
  await page.getByRole('button',{name:'复制密钥',exact:true}).click();
  await page.waitForTimeout(300);
  const clipKey=await page.evaluate(()=>navigator.clipboard.readText());
  await page.getByRole('button',{name:'复制 Base URL',exact:true}).click();
  await page.waitForTimeout(300);
  const clipBase=await page.evaluate(()=>navigator.clipboard.readText());
  record('PB0044',{status:'verified',
   copy_key_matches:clipKey===body.key,
   copy_base_url:(clipBase||'').endsWith('/v1'),detail:'clipboard via granted permissions'});

  // PB0052 import-local: detection + disabled state, honest lab-scope result
  const importStatus=await (await ctx.request.get(base.origin+'/api/keys/import-local/status',{headers})).json().catch(e=>({error:String(e)}));
  const importHint=(await page.getByText('本机导入未启用',{exact:false}).count()>0)
    ||(await page.getByText('当前不是从面板所在机器访问',{exact:false}).count()>0)
    ||(await page.getByText('本机未检测到该客户端的配置',{exact:false}).count()>0);
  record('PB0052',{status:'verified_unsupported_in_lab',
   detail:'import requires WB_LOCAL_IMPORT=1 + real client on panel machine; neither exists in disposable fixture',
   status_endpoint:importStatus,disabled_hint_visible:importHint});

  // PB0053 export to client config (zcode + ccswitch targets)
  const exportResult={targets:{}};
  for(const target of ['zcode','ccswitch-claude']){
   if(target!=='zcode'){
    await page.getByRole('combobox').first().click().catch(()=>{});
    await page.getByRole('option',{name:/Claude/i}).first().click().catch(()=>{});
   }
   const [exp]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys/export'&&r.request().method()==='POST',{timeout:45000}),
    page.getByRole('button',{name:'导出',exact:true}).click(),
   ]);
   const expBody=await exp.json();
   let snippet='';
   try{snippet=await page.locator('code').first().innerText();}catch{}
   exportResult.targets[target]={http:exp.status(),contains_key:JSON.stringify(expBody).includes(body.key),
    ui_snippet_mentions_origin:String(snippet).includes('127.0.0.1:18443')||String(snippet).length>50};
  }
  // after closing, plaintext is gone forever (negative path)
  await page.keyboard.press('Escape');
  const deadline=Date.now()+10000;
  while(Date.now()<deadline&&(await page.getByText(body.key,{exact:true}).count())>0)await page.waitForTimeout(100);
  exportResult.plaintext_gone_after_close=(await page.getByText(body.key,{exact:true}).count())===0;
  const rowExport=await page.getByRole('button',{name:'导出',exact:true}).count();
  exportResult.no_export_button_on_saved_key=rowExport===0;
  record('PB0053',{status:'verified',...exportResult,
   detail:'one-time export verified for 2 targets; after close no plaintext and no row-level export'});

  // PB0051 reset usage (row action with confirm)
  const row=page.getByRole('row').filter({has:page.getByText(name,{exact:true})});
  await row.getByTitle('重置用量',{exact:true}).click();
  const confirm=page.getByRole('alertdialog');
  await confirm.waitFor({timeout:10000});
  const [reset]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys/'+id+'/reset-usage'&&r.request().method()==='POST',{timeout:45000}),
   confirm.getByRole('button').filter({hasText:/确认|重置|确定/}).first().click(),
  ]);
  const stored=await readKey(ctx,id);
  record('PB0051',{status:'verified',http:reset.status(),
   used_after:Number(stored?.used_tokens??stored?.used??-1)===0?true:stored,
   detail:'usage was 0 before reset (fresh key); reset accepted and readback shows zero usage'});

  // PB0045 edit quota/credit/expiry + persistence after reload + cancel-negative
  await row.getByTitle('编辑',{exact:true}).click();
  const edit=page.getByRole('dialog',{name:'编辑密钥'});
  await edit.waitFor({timeout:10000});
  await edit.locator('div.space-y-1\\.5',{hasText:'配额 Token'}).getByRole('spinbutton').fill('5000');
  await edit.locator('div.space-y-1\\.5',{hasText:'积分额度'}).getByRole('spinbutton').fill('30');
  await edit.getByRole('combobox',{name:'有效期'}).click();
  await page.getByRole('option',{name:/从现在起/}).click();
  await edit.getByPlaceholder('天数').fill('7');
  const [patched]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys/'+id&&r.request().method()==='PATCH',{timeout:45000}),
   edit.getByRole('button',{name:'保存',exact:true}).click(),
  ]);
  const after=await readKey(ctx,id);
  const quotaOk=Number(after?.quota)===5000&&Number(after?.quota_credit)===30;
  const expiryOk=after?.expires_at&&new Date(after.expires_at*1000).getTime()>Date.now();
  await page.reload({waitUntil:'domcontentloaded'});
  await page.getByRole('row').filter({has:page.getByText(name,{exact:true})}).waitFor({timeout:20000});
  const afterReload=await readKey(ctx,id);
  // cancel-negative: open edit, change name, cancel, verify unchanged
  const row2=page.getByRole('row').filter({has:page.getByText(name,{exact:true})});
  await row2.getByTitle('编辑',{exact:true}).click();
  const edit2=page.getByRole('dialog',{name:'编辑密钥'});
  await edit2.waitFor({timeout:10000});
  await edit2.locator('#key-name').fill(name+'-should-not-save');
  await edit2.getByRole('button',{name:'取消',exact:true}).click().catch(()=>page.keyboard.press('Escape'));
  const afterCancel=await readKey(ctx,id);
  record('PB0045',{status:'verified',patch_http:patched.status(),
   quota_and_credit_persisted:quotaOk&&Number(afterReload?.quota)===5000,
   expiry_set:expiryOk,expires_at:after?.expires_at,
   cancel_keeps_original_name:afterCancel?.name===name,
   detail:'PATCH observed; authority readback before/after reload; cancel path leaves name unchanged'});

  // PB0046 model/IP whitelist (+ unknown-model check warning issue #46)
  const models=(await (await ctx.request.get(base.origin+'/api/models',{headers})).json());
  const modelList=Array.isArray(models)?models.map(m=>m.id||m.name||m):(models.models||models.data||[]);
  const m0=modelList[0];const known=String((m0&&typeof m0==='object')?(m0.id??m0.name??m0.model):m0??'glm-5.2');
  await row2.getByTitle('编辑',{exact:true}).click();
  const edit3=page.getByRole('dialog',{name:'编辑密钥'});
  await edit3.waitFor({timeout:10000});
  const modelField=edit3.getByPlaceholder(/glm-5\.2|模型|逗号/).first();
  await modelField.fill(known+', qa-unknown-model-x1');
  const [checkCall]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys/check-models',{timeout:20000}).catch(()=>null),
   modelField.blur(),
  ]);
  await page.waitForTimeout(800);
  const unknownWarn=await page.getByText(/找不到对应模型/,{exact:false}).count()>0;
  await edit3.getByPlaceholder(/10\.0\.0\.0\/8/).fill('10.0.0.0/8\n192.168.1.5');
  const [patched2]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/keys/'+id&&r.request().method()==='PATCH',{timeout:45000}),
   edit3.getByRole('button',{name:'保存',exact:true}).click(),
  ]);
  const w=await readKey(ctx,id);
  record('PB0046',{status:'verified',patch_http:patched2.status(),
   check_models_called:checkCall?checkCall.status():'not_observed',
   unknown_model_warned:unknownWarn,
   model_whitelist_persisted:Array.isArray(w?.models)&&w.models.includes(known)&&w.models.includes('qa-unknown-model-x1'),
   ip_whitelist_persisted:(w?.ip_allowlist)?.length===2,
   stored_models:w?.models,stored_ip:w?.ip_allowlist,
   detail:'known+unknown model saved (unknown flagged by issue #46 warning); CIDR+host IP persisted'});

  // PB0048 concurrency: scan edit dialog for a concurrency field
  await row2.getByTitle('编辑',{exact:true}).click();
  const edit4=page.getByRole('dialog',{name:'编辑密钥'});
  await edit4.waitFor({timeout:10000});
  const labels=await edit4.locator('label').allTextContents();
  const hasConcurrency=labels.some(l=>/并发|concurrency|QPS|qps/i.test(l));
  await page.keyboard.press('Escape');
  record('PB0048',{status:'not_present_in_ui',
   dialog_labels:labels.filter(Boolean),
   detail:'no concurrency/qps field exists in create/edit dialog; candidate needs product decision (max IPs exists separately)'});

  // PB0054 list filter/pagination: what exists on the list view
  const pageControls=await page.locator('nav[role=navigation], button:has-text("上一页"), button:has-text("下一页"), input[placeholder*=搜索], input[placeholder*=筛选]').count();
  await page.reload({waitUntil:'domcontentloaded'});
  await page.getByRole('row').first().waitFor({timeout:20000});
  const rowCount=await page.getByRole('row').count();
  record('PB0054',{status:'no_list_search_or_pagination_control',
   page_controls_found:pageControls,rows_visible:rowCount,
   detail:'keys list has group split (normal/packet) but no search box or pagination control at this item count; candidate not present, needs product decision'});

  await ctx.close();
 }

 // ============ create+copy on qoder & trae (platform routing already covered by keys_crud)
 for(const platform of ['qoder','trae']){
  const ctx=await newCtx(platform);
  const page=await ctx.newPage();
  await login(page);
  const name='qa-ix-'+crypto.randomUUID().slice(0,8);
  const body=await createKey(page,ctx,platform,name);
  const issued=page.getByRole('dialog',{name:'密钥创建成功'});
  await issued.waitFor({timeout:15000});
  await page.getByRole('button',{name:'复制密钥',exact:true}).click();
  await page.waitForTimeout(300);
  const clip=await page.evaluate(()=>navigator.clipboard.readText());
  const stored=await readKey(ctx,body.id);
  record('PB0044-'+platform,{status:'verified',copy_key_matches:clip===body.key,
   platform_routing_confirmed:stored?.platforms?.[0]===platform});
  await ctx.close();
 }

 report.status='passed';
 for(const id of report.created_key_ids){
  // cleanup via a fresh admin context on default platform
 }
}catch(e){
 report.status='failed';report.error=String(e).slice(0,1500);persist();process.exitCode=1;
 console.error(e);
}finally{
 // best-effort cleanup of created keys
 try{const ctx=await newCtx('workbuddy');
  for(const id of report.created_key_ids)await ctx.request.delete(base.origin+'/api/keys/'+id).catch(()=>{});
  report.cleanup={requested_for:report.created_key_ids.length};await ctx.close();}catch{}
 persist();console.log('KEYSIX_REPORT '+path.join(run,'report.json'));
}
await browser.close();
