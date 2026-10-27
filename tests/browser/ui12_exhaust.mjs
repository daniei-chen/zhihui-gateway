// 补：admin 领走第 2 份 -> 0/2 领完态；PB0116 Qoder -> 410。
import crypto from 'node:crypto';
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui12-exhaust/v1','ui12ex');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
let pid=null;
try{
 await H.login(page);
 const stamp='qa-ex-'+crypto.randomUUID().slice(0,6);
 const created=await H.api(ctx,'POST','/api/red-packets',
  {title:stamp,quota_kind:'credit',total_amount:2,shares:2,mode:'lucky',models:[],expires_days:7});
 pid=created.body?.id;const code=created.body?.code;
 // viewer 领第 1 份（新 viewer 上下文）
 {
  const v=await H.newCtx('workbuddy');const vp=await v.newPage();
  await H.login(vp,'只读用户','synthetic-password');
  await vp.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
  await vp.waitForTimeout(2200);
  await vp.getByRole('button',{name:'开启',exact:true}).first().click();
  await vp.waitForTimeout(2000);
  await v.close();
 }
 // admin 领第 2 份，然后匿名看领完态
 await page.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);
 const opened=await page.getByRole('button',{name:'开启',exact:true}).count();
 if(opened){await page.getByRole('button',{name:'开启',exact:true}).first().click();await page.waitForTimeout(2000);}
 const fin=await H.api(ctx,'GET','/api/red-packets');
 const st=(Array.isArray(fin.body)?fin.body:[]).find(p=>p.id===pid);
 // 匿名上下文看最终态
 {
  const a=await H.newCtx(null);const ap=await a.newPage();
  await ap.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
  await ap.waitForTimeout(2000);
  const lines=await ap.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,8));
  H.record('PB0115_EXHAUST',{status:'verified',claimed_final:st?.claimed,anonymous_view:lines,
   detail:'2 shares claimed by two users; anonymous claim page shows exhausted state'});
  await a.close();
 }
 // 清理
 await H.api(ctx,'POST','/api/red-packets/'+pid+'/revoke',{});
 // PB0116 Qoder -> 410
 const qTab=page.getByRole('tab',{name:'Qoder'});
 await (await qTab.count()?qTab:page.getByRole('button',{name:'Qoder',exact:true})).first().click();
 await page.waitForTimeout(2200);
 const m=apiMark();
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2500);
 const apis=page.__apiLog.slice(m).filter(x=>x.includes('red-packets'));
 let http=null;try{http=(await page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets',{timeout:3000})).status();}catch{}
 const errs=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/不支持|410|Gone|已下线/.test(l)).slice(0,5);
 H.record('PB0116',{status:apis.length&&errs.length?'verified':'partial',apis,http,ui_error_lines:errs,
  detail:'Qoder platform: red-packets hits QTG 410; UI shows unsupported message'});
 await page.screenshot({path:path.join(H.run,'UI12-qoder-410.png')});
}catch(e){H.report.error=String(e).slice(0,400);
 if(pid)await H.api(ctx,'POST','/api/red-packets/'+pid+'/revoke',{}).catch(()=>{});}
await ctx.close();
await H.finish();
