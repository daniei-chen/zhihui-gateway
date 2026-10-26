// UI13 领取页 7 行专项：读取 code / 错误过期码 / 领取 / 重复并发 / 结果复制 / 导出配置 / 返回继续。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui13/v1','ui13ix');
const {base}=H;
const CONSOLE=base.origin;
const admin=await H.newCtx('workbuddy');
const ap=await admin.newPage();
let pid=null,code=null;
try{
 await H.login(ap);
 // 建 2 份红包（一份用于正常领取，一份用于并发）
 const mk=async n=>{
  const r=await H.api(admin,'POST','/api/red-packets',
   {title:'qa-ui13-'+n+'-'+crypto.randomUUID().slice(0,5),quota_kind:'credit',
    total_amount:2,shares:2,mode:'lucky',models:[],expires_days:7});
  return r.body;};
 const p1=await mk('a');pid=p1?.id;code=p1?.code;
 const p2=await mk('b');
 H.record('SETUP',{p1:{id:p1?.id,code:!!p1?.code},p2:{id:p2?.id,code:!!p2?.code}});

 // 独立浏览器上下文（未登录）验证公开页
 const anon=await H.newCtx(null);
 const vp=await anon.newPage();
 const apis=[];vp.on('response',r=>{const u=new URL(r.url());
  if(u.pathname.startsWith('/api/'))apis.push(r.request().method()+' '+r.status()+' '+u.pathname);});

 // ---- PB0117 读取code（URL 参数解析 + 页面展示信息）
 await vp.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(2500);
 const info=await vp.evaluate(()=>({path:location.pathname+location.search,
  lines:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,10),
  hasLink:document.querySelectorAll('a').length}));
 H.record('PB0117',{status:info.lines.some(l=>/还剩|份|失效/.test(l))?'verified':'partial',
  url:info.path,page_lines:info.lines,apis:apis.slice(-3),
  detail:'/claim/?code=… 解析 code 并展示红包标题/剩余份数/失效时间（无需登录）'});

 // ---- PB0118 错误/过期码
 const probes={};
 for(const [label,q] of [['garbage','code=not-a-real-code-xyz'],
   ['empty','code='],['missing','']]){
  await vp.goto(CONSOLE+'/claim/'+(q?'?'+q:''),{waitUntil:'domcontentloaded'});
  await vp.waitForTimeout(1800);
  const l=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,8));
  probes[label]=l;
 }
 // 过期码：建一个 expires_days=0 的（后端限制？）改用已收回的
 await H.api(admin,'POST','/api/red-packets/'+p2?.id+'/revoke',{});
 await vp.goto(CONSOLE+'/claim/?code='+p2?.code,{waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(1800);
 probes.revoked=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,8));
 const errShown=Object.values(probes).some(ls=>ls.some(l=>/无效|不存在|已结束|已收回|过期|错误/.test(l)));
 H.record('PB0118',{status:errShown?'verified':'partial',probes,
  detail:'非法/空/缺失 code 与已收回码的错误态文案'});

 // ---- PB0119 领取（重新建一份，真实领取）
 const p3=await mk('c');
 await vp.goto(CONSOLE+'/claim/?code='+p3?.code,{waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(2200);
 // 匿名无法领（需登录）——先记录匿名态，再用登录用户
 const anonBtn=await vp.evaluate(()=>[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean));
 await vp.goto(CONSOLE+'/login/',{waitUntil:'domcontentloaded'});
 await vp.locator('#username').fill('只读用户');
 await vp.locator('#password').fill('synthetic-password');
 await vp.locator('button[type="submit"]').click();
 await vp.waitForTimeout(1500);
 await vp.goto(CONSOLE+'/claim/?code='+p3?.code,{waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(2200);
 const beforeClaim=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,8));
 const openBtn=vp.getByRole('button',{name:/开启|领取/}).first();
 let claimHttp=null;
 if(await openBtn.count()){
  const [r]=await Promise.all([
   vp.waitForResponse(x=>/claim/.test(new URL(x.url()).pathname),{timeout:20000}).catch(()=>null),
   openBtn.click()]);
  claimHttp=r?r.status():null;
  await vp.waitForTimeout(2500);
 }
 const afterClaim=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12));
 const gotKey=afterClaim.some(l=>/你的密钥|复制密钥|wbt_/.test(l));
 H.record('PB0119',{status:gotKey?'verified':'partial',anonymous_buttons:anonBtn.slice(0,4),
  before:beforeClaim,claim_http:claimHttp,after:afterClaim.slice(0,8),
  detail:'登录用户点"开启"-> POST /api/claim/{code} -> 展示密钥与复制入口（匿名态无领取按钮）'});
 // ---- PB0120 重复领取（同用户再来一次）
 await vp.reload({waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(2200);
 const repeat=await vp.evaluate(()=>({lines:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,10),
  buttons:[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,5)}));
 const repeatClaim=await vp.getByRole('button',{name:/开启|领取/}).count();
 H.record('PB0120',{status:!repeatClaim&&/你已经领过了/.test(repeat.lines.join(' '))?'verified':'partial',
  repeat_view:repeat,claim_button_again:repeatClaim,
  detail:'同一用户重复领取：'+(repeatClaim?'仍可点（未拦截）':'不再显示领取按钮，提示"你已经领过了，这是你的那份"' )});
 // ---- PB0121 结果复制
 {
  const cp=vp.getByRole('button',{name:/复制/}).first();
  let ev={status:'not_present_in_ui'};
  if(await cp.count()){
   await cp.click();await vp.waitForTimeout(600);
   const clip=await vp.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   ev={status:clip?'verified':'partial',clipboard_len:clip?.length,prefix:clip?.slice(0,12),
    detail:'复制密钥 -> 剪贴板'};
  }
  H.record('PB0121',ev);
 }
 // ---- PB0122 导出配置（领取页是否提供客户端配置导出）
 {
  const ex=vp.getByRole('button',{name:/导出|下载|配置/}).first();
  const linesTxt=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,14));
  let ev={status:'not_present_in_ui',page_lines:linesTxt};
  if(await ex.count()){
   const [r]=await Promise.all([
    vp.waitForResponse(x=>/export|config/.test(new URL(x.url()).pathname),{timeout:12000}).catch(()=>null),
    ex.click()]);
   await vp.waitForTimeout(1500);
   ev={status:r?'verified':'partial',http:r?.status()??null,path:r?new URL(r.url()).pathname:null,
    detail:'领取页导出配置 -> '+(r?new URL(r.url()).pathname:'未触发')};
  }else{
   ev.detail='领取页无导出/下载控件；配置导出在密钥页（UI05 PB0053 已证）——候选人需产品裁定';
  }
  H.record('PB0122',ev);
 }
 // ---- PB0123 返回/继续
 {
  const back=vp.locator('a[href="/"], a[href="/dashboard/"], button:has-text("返回"), a:has-text("返回")').first();
  let ev={status:'not_present_in_ui'};
  if(await back.count()){
   await back.click().catch(()=>{});
   await vp.waitForTimeout(1500);
   ev={status:'verified',after_path:new URL(vp.url()).pathname,
    detail:'返回入口 -> '+new URL(vp.url()).pathname};
  }else{
   const links=await vp.evaluate(()=>[...document.querySelectorAll('a')].map(a=>a.getAttribute('href')).slice(0,8));
   ev={status:'not_present_in_ui',links,detail:'领取页无返回/继续入口（仅展示领取结果）'};
  }
  H.record('PB0123',ev);
 }
 // 清理
 for(const p of [p1,p2,p3]){if(p?.id)await H.api(admin,'POST','/api/red-packets/'+p.id+'/revoke',{}).catch(()=>{});}
 await anon.close();
}catch(e){H.report.error=String(e).slice(0,500);
 if(pid)await H.api(admin,'POST','/api/red-packets/'+pid+'/revoke',{}).catch(()=>{});}
await admin.close();
await H.finish();
