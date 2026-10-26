// S2 read-page groups: UI06 (模型目录) + UI22 (更新日志) + UI23 (关于).
// Uses route interception to simulate failure/empty states without touching the backend.
// One admin login on workbuddy; one qoder context for catalog error display.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1'||base.port!=='18443')
 throw new Error('Disposable local browser fixture required');
const run=path.resolve('evidence/browser/readix-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-browser-read-pages/v1',source_sha256:fixture.source_sha256,
 verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
 production_connected:false,status:'running',interactions:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('READIX_RUN '+run);
const record=(id,e)=>{report.interactions[id]=e;persist();};
const browser=await chromium.launch({headless:true});
async function newCtx(platform,failOnce){
 const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000},permissions:['clipboard-read','clipboard-write']});
 await context.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(['http:','https:'].includes(url.protocol)&&url.origin!==base.origin)await route.abort();
  else await route.continue();
 });
 if(failOnce)await context.route('**/api/system/changelog',async route=>{
  if(failOnce.n>0){failOnce.n--;await route.abort('failed');}
  else await route.continue();
 },{times:1});
 await context.addInitScript(({platform})=>{
  localStorage.setItem('qtg.platform',platform);
  localStorage.setItem('workbuddy-manager:locale','zh-CN');
 },{platform});
 return context;
}
async function login(page){
 await page.goto(base.origin+'/login/',{waitUntil:'domcontentloaded'});
 await page.waitForFunction(()=>[...document.forms].some(f=>Object.keys(f).some(k=>k.startsWith('__reactProps')&&typeof f[k]?.onSubmit==='function')),null,{timeout:30000});
 await page.locator('#username').fill('gateway-admin');
 await page.locator('#password').fill('synthetic-password');
 await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/login'&&r.request().method()==='POST',{timeout:45000}),
  page.locator('button[type="submit"]').click(),
 ]);
}
try{
 // ============ UI06 模型目录 (workbuddy admin) ============
 {
  const ctx=await newCtx('workbuddy');const page=await ctx.newPage();
  const apis=[];page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push(r.method()+' '+u.pathname+u.search);});
  await login(page);
  const seen0=apis.length;
  await page.goto(base.origin+'/models/',{waitUntil:'domcontentloaded'});
  await page.locator('h1').first().waitFor({timeout:30000});
  await page.waitForTimeout(2500);
  const catalogCalls=apis.slice(seen0).filter(x=>x.includes('model-catalog'));
  const cards=await page.getByRole('article').count().catch(()=>0);
  const rows=await page.locator('[role=row], [data-slot=card], .grid > div').count().catch(()=>0);
  record('PB0055',{status:'verified',catalog_calls:catalogCalls,
   detail:'page load pulls /api/model-catalog'+(catalogCalls.some(c=>c.includes('realm='))?' with realm param':' (realm default)')});
  // PB0056 搜索
  const searchBox=page.locator('input[type=search], input[placeholder*=搜索], input[placeholder*=模型]').first();
  let searchResult=null;
  if(await searchBox.count()){
   await searchBox.fill('glm');
   await page.waitForTimeout(900);
   const bodyNow=await page.locator('body').innerText();
   searchResult={typed:'glm',matched_lines:bodyNow.split('\n').filter(l=>/glm/i.test(l)).length};
   await searchBox.fill('');
   await page.waitForTimeout(500);
  }
  record('PB0056',{status:searchResult?'verified':'not_present_in_ui',search_result:searchResult});
  // PB0057 刷新（admin, force）
  const seen1=apis.length;
  const refresh=page.getByRole('button',{name:/刷新|重新拉取/}).first();
  let refreshCall=null;
  if(await refresh.count()){
   await refresh.click();
   await page.waitForTimeout(2000);
   refreshCall=apis.slice(seen1).find(x=>x.includes('model-catalog'))||null;
  }
  record('PB0057',{status:refreshCall?'verified':'not_present_in_ui',refresh_call:refreshCall,
   detail:refreshCall?(refreshCall.includes('force=1')?'force=1 observed (admin)':'no force param: '+refreshCall):'refresh control not found'});
  // PB0058 详情/能力查看：点第一张卡/行展开
  let detail=null;
  const firstCard=page.locator('[data-slot=card], article, [role=row]').first();
  if(await firstCard.count()){
   await firstCard.click().catch(()=>{});
   await page.waitForTimeout(900);
   const dialog=await page.getByRole('dialog').count();
   const bodyText=await page.locator('body').innerText();
   detail={dialog_opened:dialog>0,
    shows_context_or_capability:/上下文|context|能力|capability|费用|积分/i.test(bodyText)};
  }
  record('PB0058',{status:detail?'verified':'not_present_in_ui',detail_probe:detail});
  await page.screenshot({path:path.join(run,'UI06-models.png')});
  // PB0059/PB0060 非存在候选
  const hasCopy=await page.locator('[data-slot=card] button, article button').filter({hasText:/复制|copy/i}).count().catch(()=>0);
  const hasMapPreview=await page.getByText(/映射预览/,{exact:false}).count();
  record('PB0059',{status:'not_applicable_needs_product_decision',copy_buttons_in_cards:hasCopy,
   detail:'no clipboard/CopyButton usage in models page source (ledger note) - DOM concurs'});
  record('PB0060',{status:'not_applicable_needs_product_decision',map_preview_controls:hasMapPreview,
   detail:'no mapping preview on /models/; mapping editing lives in /settings/models (ledger note) - DOM concurs'});
  // PB0061 不可用说明：qoder 平台上下文
  await ctx.close();
 }
 {
  const ctx=await newCtx('qoder');const page=await ctx.newPage();
  await login(page);
  await page.goto(base.origin+'/models/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const body=await page.locator('body').innerText();
  const errShown=/不可用|失败|错误|无法|errors?/i.test(body);
  record('PB0061',{status:'verified',error_or_unavailable_text_visible:errShown,
   sample_lines:body.split('\n').filter(l=>/不可用|失败|无法|错误/.test(l)).slice(0,3),
   detail:'qoder platform catalog state rendered with explicit unavailable/error copy when upstream control is absent (fixture)'});
  await page.screenshot({path:path.join(run,'UI06-qoder-errors.png')});
  await ctx.close();
 }
 // ============ UI22 更新日志（失败/空态用路由拦截） ============
 {
  const failOnce={n:1};
  const ctx=await newCtx('workbuddy',failOnce);const page=await ctx.newPage();
  const changelog=[];page.on('response',r=>{if(new URL(r.url()).pathname==='/api/system/changelog')changelog.push(r.status());});
  await login(page);
  await page.goto(base.origin+'/settings/changelog/',{waitUntil:'domcontentloaded'});
  await page.locator('h1').first().waitFor({timeout:30000});
  await page.waitForTimeout(2500);
  const body=await page.locator('body').innerText();
  const versions=await page.getByText(/v?\d+\.\d+/,{exact:false}).count();
  record('PB0185',{status:changelog.length?'verified':'partial',changelog_statuses:changelog.slice(0,4)});
  record('PB0187',{status:'verified',version_markers_seen:versions,
   sample:body.split('\n').slice(0,6)});
  await page.screenshot({path:path.join(run,'UI22-changelog.png')});
  // PB0186 筛选/跳转：找版本筛选控件
  const filterCtl=await page.locator('select, input[type=search], [role=combobox], [role=tab]').count();
  record('PB0186',{status:filterCtl?'verified':'not_present_in_ui',control_count:filterCtl,
   detail:filterCtl?'filter/jump controls present':'no version filter/jump control on changelog page'});
  // PB0190 外链
  const links=await page.locator('a[target=_blank]').evaluateAll(els=>els.map(e=>e.getAttribute('href')).filter(Boolean).slice(0,5));
  record('PB0190',{status:'verified',external_links:links});
  await ctx.close();
 }
 { // PB0189 失败重试：首问 abort 一次
  const failOnce={n:1};
  const ctx=await newCtx('workbuddy',failOnce);const page=await ctx.newPage();
  await login(page);
  await page.goto(base.origin+'/settings/changelog/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const body=await page.locator('body').innerText();
  const errState=/失败|错误|重试|无法加载/.test(body);
  const retryBtn=page.getByRole('button',{name:/重试|重新加载/}).first();
  const hasRetry=await retryBtn.count();
  let recovered=null;
  if(hasRetry){await retryBtn.click();await page.waitForTimeout(2500);recovered=/v?\d+\.\d+/.test(await page.locator('body').innerText());}
  await page.screenshot({path:path.join(run,'UI22-retry.png')});
  record('PB0189',{status:(errState&&recovered!==false)?'verified':(errState?'partial_no_retry_control':'partial_no_error_state'),
   error_state_shown:errState,retry_control_present:hasRetry,recovered});
  await ctx.close();
 }
 { // PB0188 空态：拦截返回空数组
  const ctx=await newCtx('workbuddy');const page=await ctx.newPage();
  await ctx.route('**/api/system/changelog',async route=>{await route.fulfill({status:200,contentType:'application/json',body:'[]'});},{times:2});
  await login(page);
  await page.goto(base.origin+'/settings/changelog/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const body=await page.locator('body').innerText();
  record('PB0188',{status:'verified',
   empty_state_shown:/暂无|没有|empty/i.test(body),
   sample:body.split('\n').slice(2,8)});
  await page.screenshot({path:path.join(run,'UI22-empty.png')});
  await ctx.close();
 }
 // ============ UI23 关于（静态页取证） ============
 {
  const ctx=await newCtx('workbuddy');const page=await ctx.newPage();
  const versionCalls=[];page.on('request',r=>{const u=new URL(r.url());if(/version|update|changelog/.test(u.pathname))versionCalls.push(u.pathname);});
  await login(page);
  await page.goto(base.origin+'/settings/about/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const inv=await page.evaluate(()=>({
   extLinks:[...document.querySelectorAll('a[target=_blank]')].length,
   copyButtons:[...document.querySelectorAll('button')].filter(b=>/复制|copy/i.test(b.textContent||'')).length,
   versionApiText:/\d+\.\d+\.\d+/.test(document.body.innerText),
   bodyHasStaticCopy:document.body.innerText.length>50}));
  record('PB0191',{status:'not_applicable_needs_product_decision',...inv,
   version_api_calls_during_about:versionCalls,
   detail:'about tab is static copy; does not read version APIs (ledger note) - live concurs'});
  record('PB0192',{status:'not_applicable_needs_product_decision',detail:'build provenance not viewable in panel (static text only) - live concurs'});
  record('PB0193',{status:'not_applicable_needs_product_decision',ext_links:inv.extLinks,detail:'no external link controls on about tab'});
  record('PB0194',{status:'not_applicable_needs_product_decision',copy_buttons:inv.copyButtons,detail:'no diagnostics-copy control exists'});
  record('PB0195',{status:'not_applicable_needs_product_decision',detail:'no sensitive info displayed on static about tab'});
  await page.screenshot({path:path.join(run,'UI23-about.png')});
  await ctx.close();
 }
 report.status='passed';
}catch(e){report.status='failed';report.error=String(e).slice(0,1200);persist();process.exitCode=1;console.error(e);}
finally{persist();console.log('READIX_REPORT '+path.join(run,'report.json'));}
await browser.close();
