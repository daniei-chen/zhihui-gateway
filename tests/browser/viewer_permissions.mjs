import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8')),base=new URL(fixture.host_url);
if(fixture.production_connected!==false || base.hostname!=='127.0.0.1' || base.port!=='18443')throw new Error('Local QA fixture required');
const run=path.resolve('evidence/browser/viewer-'+new Date().toISOString().replaceAll(/[:.]/g,'-')+'-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
fs.copyFileSync(new URL(import.meta.url),path.join(run,'verifier.mjs'));
const report={schema:'zhihui-viewer-browser/v1',source_sha256:fixture.source_sha256,production_connected:false,
 verifier_sha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),
 status:'running',cases:[],scope:'Viewer controls, automatic side effects and direct write denial; engine behavior not accepted'};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
persist();console.log('VIEWER_RUN '+run);
const browser=await chromium.launch({headless:true});
try{
 for(const platform of ['workbuddy','qoder','trae']){
  const context=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN'});
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());
   if(['http:','https:'].includes(u.protocol) && u.origin!==base.origin)await route.abort();else await route.continue();
  });
  await context.addInitScript(({platform})=>{
   localStorage.setItem('qtg.platform',platform);localStorage.setItem('workbuddy-manager:locale','zh-CN');
  },{platform});
  const item={platform,status:'running',automatic_writes:[],pages:[],steps:[]};report.cases.push(item);persist();
  const page=await context.newPage();
  page.on('request',request=>{
   const url=new URL(request.url());
   if(url.pathname.startsWith('/api/') && !['GET','HEAD','OPTIONS'].includes(request.method()))
    item.automatic_writes.push({path:url.pathname,method:request.method()});
  });
  const check=(ok,why)=>{if(!ok)throw new Error(why);};
  const headers={'X-QTG-Platform':platform,Origin:base.origin};
  try{
   const login=await context.request.post(base.origin+'/api/login',{data:{username:'只读用户',password:'synthetic-password'}});
   check(login.status()===200 && (await login.json()).role==='viewer','Synthetic viewer authority');
   for(const pathname of ['/dashboard/','/accounts/','/keys/']){
    const [identity]=await Promise.all([
     page.waitForResponse(r=>new URL(r.url()).pathname==='/api/me',{timeout:45000}),
     page.goto(base.origin+pathname,{waitUntil:'domcontentloaded',timeout:45000}),
    ]);
    check(identity.status()===200 && (await identity.json()).role==='viewer','Actual page identity mismatch');
    await page.locator('h1').waitFor({timeout:45000});
    await page.waitForTimeout(1200);
    await page.waitForFunction(()=>!document.querySelector('[data-slot="skeleton"],[aria-busy="true"]'),null,{timeout:45000});
    const info={path:pathname,headings:await page.locator('h1').allTextContents()};
    check(new URL(page.url()).pathname!== '/login/','Viewer lost authenticated page');
    if(pathname==='/keys/'){
     check(await page.getByRole('button',{name:'新建密钥',exact:true}).count()===0,'Viewer sees create key control');
     check(await page.locator('button[title="编辑"],button[title="停用"],button[title="删除"]').count()===0,
           'Viewer sees key mutation controls');
    }
    item.pages.push(info);persist();
   }
   check(item.automatic_writes.length===0,'Read-only navigation triggered a write request');
   const before=await context.request.get(base.origin+'/api/keys',{headers});
   check(before.status()===200,'Viewer list status');
   const ids=(await before.json()).map(k=>k.id).sort();
   const denied=await context.request.post(base.origin+'/api/keys',{headers,data:{name:'qa-viewer-must-never-create',platforms:[platform]}});
   check(denied.status()===403,'Direct viewer write not denied');
   const after=await context.request.get(base.origin+'/api/keys',{headers});
   check(after.status()===200 && JSON.stringify((await after.json()).map(k=>k.id).sort())===JSON.stringify(ids),
         'Denied mutation changed persistent key list');
   item.steps=['real_viewer_authority','three_pages_without_automatic_writes','no_key_mutation_controls',
               'direct_create_403','persistent_list_unchanged'];
   item.status='passed';
  }catch(error){item.status='failed';item.failure=String(error.message).slice(0,1000);process.exitCode=1;}
  finally{await context.close();persist();console.log(JSON.stringify({platform,status:item.status,failure:item.failure}));}
 }
 report.status=report.cases.every(c=>c.status==='passed')?'passed':'failed';
}finally{await browser.close();persist();console.log('VIEWER_REPORT '+path.join(run,'report.json'));}
