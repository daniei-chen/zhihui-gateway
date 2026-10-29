// Rendering/control inventory; this does not claim all buttons executed.
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
  'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'../..');
if(!process.argv[2])throw new Error('Pass the captured LOCAL browser-ready.json');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false || base.hostname!=='127.0.0.1' || base.port!=='18443')
  throw new Error('Only the disposable local fixture is allowed');
const limit=Number(process.env.BROWSER_CASE_LIMIT || 10000);
const dir=path.join(root,'evidence/browser',new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(dir,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(dir,'verifier.mjs'));
function csv(text){
  const rows=[];let row=[],value='',quoted=false;
  for(let i=0;i<text.length;i++){
    const c=text[i];
    if(c==='"' && quoted && text[i+1]==='"'){value+='"';i++;}
    else if(c==='"')quoted=!quoted;
    else if(c===',' && !quoted){row.push(value);value='';}
    else if(c==='\n' && !quoted){row.push(value.replace(/\r$/,''));rows.push(row);row=[];value='';}
    else value+=c;
  }
  if(value || row.length){row.push(value);rows.push(row);}
  const header=rows.shift();
  return rows.filter(r=>r.length>1).map(r=>Object.fromEntries(header.map((k,i)=>[k,r[i]||''])));
}
const contracts=csv(fs.readFileSync(path.join(root,'contracts/page-button-capability.csv'),'utf8'));
const groups=[...new Map(contracts.map(row=>[row.ui_id,{id:row.ui_id,page:row.page,route:row.route}])).values()];
const eligiblePages=groups.filter(p=>/^\/[a-z0-9/\-]*$/.test(p.route));
eligiblePages.push({id:'UI24',page:'不存在页面',route:'/qa-definitely-absent/'});
const selected=(process.env.BROWSER_UI_IDS||'').split(',').map(x=>x.trim()).filter(Boolean);
if(selected.some(id=>!eligiblePages.some(p=>p.id===id)))throw new Error('Unknown browser page group selected');
const pages=selected.length?eligiblePages.filter(p=>selected.includes(p.id)):eligiblePages;
const report={schema:'zhihui-browser-inventory/v1',status:'running',source_sha256:fixture.source_sha256,
  verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
  scope:'Static frontend + real Nginx/Manager/QTG; fake provider; engine control behavior incomplete',
  verification_level:'page_render_and_control_inventory',production_connected:false,
  all_interactions_executed:false,contract_groups:groups.length,interaction_candidates:contracts.length,
  planned_page_cases:eligiblePages.length*6,selected_page_cases:pages.length*6,case_limit:limit,
  missing_scope:['UI01 marketing :8081 artifact','UI25 legacy routes','UI26 non-current candidates',
                 'populated real engine state and full 209 interaction executions'],
  cases:[],auth:[],external_requests_blocked:[]};
const persist=()=>fs.writeFileSync(path.join(dir,'report.json'),JSON.stringify(report,null,2));
persist();console.log('BROWSER_RUN '+dir);
const browser=await chromium.launch({headless:true});
let count=0;
try{
  for(const role of ['admin','viewer'])for(const platform of ['workbuddy','qoder','trae']){
    if(count>=limit)break;
    const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN',viewport:{width:1440,height:1000}});
    await context.route('**/*',async route=>{
      const u=new URL(route.request().url());
      if(['http:','https:'].includes(u.protocol) && u.origin!==base.origin){
        report.external_requests_blocked.push({host:u.hostname,path:u.pathname});await route.abort();
      }else await route.continue();
    });
    await context.addInitScript(({platform})=>{
      localStorage.setItem('qtg.platform',platform);
      localStorage.setItem('workbuddy-manager:locale','zh-CN');
    },{platform});
    const page=await context.newPage();
    const username=role==='admin'?'gateway-admin':'只读用户';
    const login=await context.request.post(base.origin+'/api/login',{data:{username,password:'synthetic-password'}});
    if(login.status()!==200)throw new Error('Synthetic login failed: '+role+'/'+platform+' '+login.status());
    if((await login.json()).role!==(role==='admin'?'admin':'viewer'))throw new Error('Wrong role from authority');
    report.auth.push({role,platform,http_status:login.status(),cookie_names:(await context.cookies()).map(c=>c.name)});
    for(const entry of pages){
      if(count++>=limit)break;
      const errors=[],api=[],failures=[];
      const onError=e=>errors.push(String(e.message).slice(0,600));
      const onResponse=r=>{
        const u=new URL(r.url());
        if(u.pathname.startsWith('/api/'))api.push({method:r.request().method(),path:u.pathname,status:r.status(),
          platform:r.request().headers()['x-qtg-platform']||null});
      };
      const onFailed=r=>failures.push({path:new URL(r.url()).pathname,error:r.failure()?.errorText});
      page.on('pageerror',onError);page.on('response',onResponse);page.on('requestfailed',onFailed);
      const item={group:entry.id,page:entry.page,route:entry.route,role,platform,errors,api,request_failures:failures};
      try{
        const response=await page.goto(base.origin+entry.route,{waitUntil:'domcontentloaded',timeout:45000});
        // The existing 404 layout uses fixed-position children, so body can have
        // zero height while its content is visible. Test the rendered content.
        await page.locator('body').waitFor({state:'attached'});
        await page.waitForTimeout(900);
        await page.waitForFunction(()=>!document.querySelector('[aria-busy="true"],[data-slot="skeleton"]'),
                                   null,{timeout:40000});
        item.status=response?.status();item.final_path=new URL(page.url()).pathname;
        if(entry.id==='UI24'){
          if(item.status!==404)throw new Error('Unknown route did not retain HTTP 404');
          await page.getByRole('link',{name:'返回首页',exact:true}).waitFor({state:'visible'});
        }else if(item.status!==200)throw new Error('Expected page HTTP 200');
        item.styles=await page.evaluate(()=>({
          links:document.querySelectorAll('link[rel="stylesheet"]').length,
          sheets:document.styleSheets.length,
          rules:[...document.styleSheets].reduce((n,s)=>{try{return n+s.cssRules.length;}catch{return n;}},0),
          body_font:getComputedStyle(document.body).fontFamily
        }));
        if(item.styles.links>0 && item.styles.rules===0)
          throw new Error('Stylesheets declared but no CSS rules applied');
        item.headings=await page.locator('h1,h2,[role="heading"]').allTextContents();
        item.controls=await page.locator('button,a,input,select,textarea,[role="button"]').evaluateAll(nodes=>nodes.map(n=>({
          tag:n.tagName.toLowerCase(),role:n.getAttribute('role'),
          text:(n.getAttribute('aria-label')||n.textContent||n.getAttribute('placeholder')||'').trim().slice(0,100),
          disabled:Boolean(n.disabled)||n.getAttribute('aria-disabled')==='true',
          type:n.getAttribute('type'),href:n.getAttribute('href')
        })));
        item.body_excerpt=(await page.locator('body').innerText()).slice(0,1200);
        const name=entry.id+'-'+role+'-'+platform+'.png';
        await page.screenshot({path:path.join(dir,name),fullPage:true,
          mask:[page.locator('input[type="password"],input[readonly],code,pre')]});
        item.screenshot=name;
        item.outcome=errors.length?'browser_error':'rendered_inventory';
      }catch(error){item.outcome='navigation_or_render_failure';item.failure=String(error.message).slice(0,800);}
      finally{
        page.off('pageerror',onError);page.off('response',onResponse);page.off('requestfailed',onFailed);
        report.cases.push(item);persist();
        console.log(JSON.stringify({group:entry.id,role,platform,outcome:item.outcome,status:item.status,
          errors:errors.length,api_errors:api.filter(r=>r.status>=400).length}));
      }
    }
    await context.close();
  }
  report.completed_cases=report.cases.length;
  report.render_failures=report.cases.filter(c=>c.outcome!=='rendered_inventory').length;
  report.scope=report.cases.length===report.planned_page_cases?'full_page_inventory':'focused_page_inventory';
  report.status=report.render_failures?'completed_with_render_failures':'completed';
  if(report.render_failures)process.exitCode=1;
}catch(error){report.status='failed';report.failure=String(error.message);process.exitCode=1;}
finally{await browser.close();persist();console.log('BROWSER_REPORT '+path.join(dir,'report.json'));}
