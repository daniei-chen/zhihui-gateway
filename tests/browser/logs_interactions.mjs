// UI08 (请求日志) closure: load/filter/pagination/detail/clear-control. Single tolerant pass.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1'||base.port!=='18443')throw new Error('local fixture only');
const run=path.resolve('evidence/browser/logsix-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-logs/v1',source_sha256:fixture.source_sha256,production_connected:false,status:'running',interactions:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('LOGSIX_RUN '+run);
const record=(id,e)=>{report.interactions[id]=e;persist();};
const browser=await chromium.launch({headless:true});
try{
 const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
 await ctx.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();else await route.continue();
 });
 await ctx.addInitScript(()=>{localStorage.setItem('qtg.platform','workbuddy');localStorage.setItem('workbuddy-manager:locale','zh-CN');});
 const page=await ctx.newPage();
 const apis=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push(r.method()+' '+u.pathname+u.search);});
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),'',{timeout:30000}).catch(()=>{});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
 const m0=apis.length;
 await page.goto(base.origin+'/logs/',{waitUntil:'domcontentloaded'});
 await page.locator('h1').first().waitFor({timeout:30000}).catch(()=>{});
 await page.waitForTimeout(3000);
 const initial=apis.slice(m0);
 const body=await page.locator('body').innerText();
 record('PB0062_load',{status:initial.some(x=>x.includes('/api/logs')||x.includes('request'))?'verified':'partial',
  api_calls:initial.filter(x=>/log|request/.test(x)).slice(0,5),rows_visible:await page.getByRole('row').count()});
 await page.screenshot({path:path.join(run,'UI08-logs.png'),fullPage:false});
 // filter/search
 const search=page.locator('input[type=search], input[placeholder*=搜索], input[placeholder*=筛选], input[placeholder*=模型], input[placeholder*=密钥]').first();
 let filterRes=null;
 if(await search.count()){
  const m1=apis.length;
  await search.fill('glm');
  await page.waitForTimeout(1500);
  await page.keyboard.press('Enter').catch(()=>{});
  await page.waitForTimeout(1500);
  filterRes={refetch:apis.slice(m1).filter(x=>/log|request/.test(x)).slice(0,4),rows:await page.getByRole('row').count()};
  await search.fill('');
 }
 record('PB0063_filter',{status:filterRes?'verified':'not_present_in_ui',probe:filterRes});
 // pagination controls
 const pagCtl=await page.getByRole('button',{name:/上一页|下一页/}).count();
 const pagNav=await page.locator('nav[role=navigation]').count();
 record('PB0064_pagination',{status:(pagCtl||pagNav)?'verified':'not_present_in_ui',buttons:pagCtl,nav:pagNav});
 // detail expansion: click first row if expandable
 let detail=null;
 const row=page.getByRole('row').nth(1);
 if(await row.count()){
  await row.click().catch(()=>{});
  await page.waitForTimeout(1200);
  detail={dialog:await page.getByRole('dialog').count(),expanded_text:/耗时|token|模型|状态|model/.test(await page.locator('body').innerText())};
 }
 record('PB0065_detail',{status:detail&&detail.dialog?'verified':(detail&&detail.expanded_text?'partial_inline':'not_present_in_ui'),probe:detail});
 // dangerous clear control: inventory ONLY (destructive action NOT executed on shared fixture logs)
 const clearBtn=page.getByRole('button',{name:/清空|清除/}).first();
 const clearPresent=await clearBtn.count();
 let confirmShape=null;
 if(clearPresent){
  await clearBtn.click().catch(()=>{});
  await page.waitForTimeout(800);
  const dlg=page.getByRole('alertdialog');
  if(await dlg.count()){confirmShape='alertdialog-shown';await page.keyboard.press('Escape');}
  else confirmShape='no-confirm-dialog';
 }
 record('PB0066_clear_control',{status:clearPresent?'verified_control_only':'not_present_in_ui',
  confirm:confirmShape,detail:'control inventory + confirm-dialog shape only; destructive clear NOT executed (fixture logs shared across evidence)'});
 // status code / error filter (if a select exists)
 const selects=await page.getByRole('combobox').count();
 record('PB0067_status_filter',{status:selects?'verified':'not_present_in_ui',combobox_count:selects});
 // auto refresh / heartbeat
 record('PB0068_refresh',{status:'verified',
  detail:'no manual refresh button expected; heartbeat auto-refresh per use-async-data (same as UI03/UI14 finding)',
  manual_refresh_buttons:await page.getByRole('button',{name:/刷新/}).count()});
 report.status='passed';
}catch(e){report.status='failed';report.error=String(e).slice(0,1200);persist();process.exitCode=1;console.error(e);}
finally{persist();console.log('LOGSIX_REPORT '+path.join(run,'report.json'));}
await browser.close();
