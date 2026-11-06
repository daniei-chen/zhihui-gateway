// Real React/Chromium verification against synthetic loopback HTTP only.
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const path=require('node:path');
const http=require('node:http');
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH||'playwright-core');
const root=path.resolve(__dirname,'../manager/web/out');
const output=path.resolve(process.argv[2]||'portal-browser-evidence');
const created=new Set(),calls=[],errors=[];
const overview=owner=>({owner_id:owner,scale:1000000,balance_units:100000000,
  account_count:0,active_account_count:0,expiring_7d_units:0,today_checkin_units:0,
  held_units:0,debt_units:0,callable_units:100000000,pending_pool_units:0,snapshot_at:1793865600,
  dispatch_enabled:true,pool_enabled:true,dispatch_note:'synthetic fixture',allowed_models:['deepseek-fixture'],
  accounts:[],grants:[],events:[]});
const types={'.html':'text/html','.css':'text/css','.js':'text/javascript','.svg':'image/svg+xml','.txt':'text/plain','.json':'application/json','.woff2':'font/woff2'};
const server=http.createServer(async(req,res)=>{
  const u=new URL(req.url,'http://127.0.0.1');
  const owner=/fixture_owner=B/.test(req.headers.cookie||'')?'B':'A';
  const send=(status,value)=>{res.writeHead(status,{'content-type':'application/json','cache-control':'no-store'});res.end(JSON.stringify(value));};
  if(u.pathname.startsWith('/api/')){
    calls.push({path:u.pathname,method:req.method,owner});
    if(u.pathname==='/api/portal/workspace')return send(200,{me:{id:owner,username:'synthetic-'+owner},data:overview(owner)});
    if(u.pathname==='/api/portal/keys'){
      if(req.method==='POST'){created.add(owner);return send(200,{id:owner==='A'?1:2,key:'synthetic-key-'+owner,existing:false});}
      return send(200,{items:created.has(owner)?[{id:owner==='A'?1:2,label:'synthetic-'+owner,prefix:'fixture',enabled:true,created_at:1}]:[],
        can_initialize:!created.has(owner),allowed_models:['deepseek-fixture'],dispatch_enabled:true,pool_enabled:true,model_discovery_enabled:true,dispatch_note:'synthetic'});
    }
    if(u.pathname==='/api/portal/auth/start')return send(200,{session:'a'.repeat(48),auth_url:'https://example.invalid/fixture',expires_in:300});
    if(u.pathname==='/api/portal/auth/poll')return send(200,{state:'waiting'});
    if(u.pathname==='/api/portal/auth/cancel')return send(200,{state:'cancelled'});
    return send(401,{detail:'synthetic anonymous admin'});
  }
  try{
    let file=path.resolve(root,'.'+decodeURIComponent(u.pathname));
    if(file!==root&&!file.startsWith(root+path.sep))throw Error('unsafe fixture path');
    if((await fs.stat(file)).isDirectory())file=path.join(file,'index.html');
    res.writeHead(200,{'content-type':types[path.extname(file)]||'application/octet-stream'});
    res.end(await fs.readFile(file));
  }catch{res.writeHead(404);res.end('fixture not found');}
});
(async()=>{
  await fs.mkdir(output,{recursive:true});
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin='http://127.0.0.1:'+server.address().port;
  let browser;
  try{
    browser=await chromium.launch({headless:true});
    const ctx=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN'});
    await ctx.route('**/*',route=>new URL(route.request().url()).origin===origin?route.continue():route.abort());
    const page=await ctx.newPage();page.on('pageerror',error=>errors.push(String(error)));
    const start=Date.now();await page.goto(origin+'/portal/',{waitUntil:'networkidle'});
    await page.getByRole('heading',{name:'你好，synthetic-A。'}).waitFor();
    const firstPageMs=Date.now()-start;
    await page.getByRole('tab',{name:'调用密钥'}).click();
    await page.getByRole('button',{name:'启用默认密钥'}).click();
    await page.getByLabel('新密钥',{exact:true}).waitFor();
    assert.equal(await page.getByLabel('新密钥',{exact:true}).inputValue(),'synthetic-key-A');
    await ctx.addCookies([{name:'fixture_owner',value:'B',url:origin}]);
    await page.getByRole('button',{name:'刷新',exact:true}).click();
    await page.getByRole('heading',{name:'你好，synthetic-B。'}).waitFor();
    assert.equal(await page.getByLabel('新密钥',{exact:true}).count(),0);
    assert.equal(await page.getByRole('tab',{name:'总览'}).getAttribute('data-state'),'active');
    await page.getByRole('button',{name:'扫码添加账号',exact:true}).click();
    await page.getByRole('dialog').waitFor();
    await ctx.addCookies([{name:'fixture_owner',value:'A',url:origin}]);
    // A focus refresh changes the authenticated snapshot while a dialog is open.
    await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await page.getByRole('heading',{name:'你好，synthetic-A。'}).waitFor({timeout:20000});
    assert.equal(await page.getByRole('dialog').count(),0);
    await page.screenshot({path:path.join(output,'portal-desktop.png'),fullPage:true});
    await page.setViewportSize({width:390,height:844});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>window.innerWidth+1);
    assert.equal(overflow,false,'the page must not overflow the mobile viewport');
    await page.screenshot({path:path.join(output,'portal-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    const report={passed:true,production_connected:false,real_chromium:true,first_page_ms:firstPageMs,
      synthetic_http:true,identity_switch_clears_plaintext_key:true,identity_switch_closes_qr:true,
      mobile_width:390,body_overflow:false,workspace_calls:calls.filter(c=>c.path.endsWith('/workspace')).length,
      page_errors:errors,requests:calls};
    await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  }finally{if(browser)await browser.close();await new Promise(resolve=>server.close(resolve));}
})().catch(error=>{console.error(error);process.exitCode=1;});
