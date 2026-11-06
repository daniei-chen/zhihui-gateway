// Real browser QA; only a loopback fixture and public static files are used.
import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {createPreviewServer} from '../site/tools/preview-server.mjs';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH||'playwright-core');
const output=path.resolve(process.argv[2]||'home-browser-evidence');
const demo=process.env.PUBLIC_DEMO_ROOT;
if(!demo)throw new Error('A private copy of the public static demo is required; no proxy fallback');
const home=new URL('../site/home/index.html',import.meta.url);
const report={production_connected:false,home_sha256:crypto.createHash('sha256').update(await fs.readFile(home)).digest('hex'),
  viewports:[],errors:[],badResponses:[],blockedExternal:[]};
await fs.mkdir(output,{recursive:true});
const server=createPreviewServer({publicDemoRoot:demo});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin='http://127.0.0.1:'+server.address().port;
const browser=await chromium.launch({headless:true,args:['--enable-unsafe-swiftshader']});
async function context(options={}) {
  const ctx=await browser.newContext({locale:'zh-CN',...options});
  await ctx.route('**/*',route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin){report.blockedExternal.push(url.href);return route.abort();}
    if(url.pathname==='/portal/')return route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><h1>synthetic portal navigation</h1>'});
    return route.continue();
  });
  const page=await ctx.newPage();
  page.on('pageerror',error=>report.errors.push(String(error)));
  page.on('response',response=>{if(response.status()>=400)report.badResponses.push({url:response.url(),status:response.status()});});
  return {ctx,page};
}
async function hero(page) {
  await page.goto(origin+'/',{waitUntil:'networkidle'});
  const admin=page.locator('header.hero .hero-actions a').filter({hasText:'管理员登录'});
  const user=page.locator('header.hero .hero-actions a').filter({hasText:'用户登录'});
  assert.equal(await admin.getAttribute('href'),'/login/');
  assert.equal(await user.getAttribute('href'),'/portal/');
  for(const link of [admin,user]) {
    await link.waitFor({state:'visible'});
    const visible=await link.evaluate(node=>{
      const b=node.getBoundingClientRect();let opacity=1;
      for(let p=node;p;p=p.parentElement)opacity*=Number(getComputedStyle(p).opacity);
      return {opacity,inside:b.x>=0&&b.y>=0&&b.right<=innerWidth&&b.bottom<=innerHeight,
        hit:document.elementFromPoint(b.x+b.width/2,b.y+b.height/2)?.closest('a')===node};
    });
    assert(visible.opacity>.95&&visible.inside&&visible.hit,JSON.stringify(visible));
  }
  return {admin,user};
}
try {
  for(const width of [1440,430,390,320]) {
    const {ctx,page}=await context({viewport:{width,height:1000},isMobile:width<500,hasTouch:width<500});
    const started=Date.now();const links=await hero(page);const firstPageMs=Date.now()-started;
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
    assert.equal(overflow,false,'body overflow at '+width);
    await page.screenshot({path:path.join(output,'home-'+width+'.png')});
    await links.user.click();await page.waitForURL(origin+'/portal/');
    const back=await hero(page);await back.admin.click();await page.waitForURL(/\/login\/?$/);
    report.viewports.push({width,first_page_ms:firstPageMs,user_login_click:true,admin_login_click:true,body_overflow:false});
    await ctx.close();
  }
  const {ctx,page}=await context({viewport:{width:1440,height:1000},reducedMotion:'reduce'});
  await hero(page);
  for(const id of ['taskWorkspace','pelicanRide']) {
    const element=page.locator('#'+id);await element.scrollIntoViewIfNeeded();
    const url=new URL(await element.getAttribute('src'),origin);
    assert(url.pathname.startsWith('/home-assets/r-bb0b5ad4c7a0582efcf4/animations/'));
    const response=await ctx.request.get(url.href);
    assert.equal(response.status(),200);assert.equal(response.headers()['x-frame-options'],'SAMEORIGIN');
    const frame=await element.elementHandle().then(handle=>handle.contentFrame());
    await frame.waitForLoadState('domcontentloaded');
    assert((await frame.locator('body').innerHTML()).length>500);
  }
  const demoFrame=page.locator('.public-console-frame');await demoFrame.scrollIntoViewIfNeeded();
  const frame=await demoFrame.elementHandle().then(handle=>handle.contentFrame());
  await frame.waitForLoadState('networkidle');assert((await frame.locator('body').innerText()).length>50);
  await ctx.close();
  const without=await context({viewport:{width:390,height:1000},javaScriptEnabled:false});
  await hero(without.page);await without.ctx.close();
  // Explicitly exercise a failed optional 3D module with the poster retained.
  const degraded=createPreviewServer({degrade3d:true,publicDemoRoot:demo});
  await new Promise(resolve=>degraded.listen(0,'127.0.0.1',resolve));
  const degradedOrigin='http://127.0.0.1:'+degraded.address().port;
  const degradeCtx=await browser.newContext({viewport:{width:1440,height:1000}});
  await degradeCtx.route('**/*',r=>new URL(r.request().url()).origin===degradedOrigin?r.continue():r.abort());
  const degradePage=await degradeCtx.newPage();await degradePage.goto(degradedOrigin+'/',{waitUntil:'networkidle'});
  await degradePage.locator('#thinkerGallery').scrollIntoViewIfNeeded();
  await degradePage.locator('#thinkerGallery[data-state="error"]').waitFor({timeout:15000});
  assert(await degradePage.locator('.sculpture-poster').isVisible());
  assert.match(await degradePage.locator('#thinkerStatus').innerText(),/静态渲染/);
  await degradeCtx.close();await new Promise(resolve=>degraded.close(resolve));
  assert.deepEqual(report.badResponses,[]);assert.deepEqual(report.errors,[]);assert.deepEqual(report.blockedExternal,[]);
  Object.assign(report,{passed:true,no_javascript_hero:true,reduced_motion_hero:true,retained_scenes_render:true,
    public_demo_render:true,optional_3d_failure_retains_poster:true});
}finally {
  await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
  await browser.close();await new Promise(resolve=>server.close(resolve));
}
console.log(JSON.stringify(report));
