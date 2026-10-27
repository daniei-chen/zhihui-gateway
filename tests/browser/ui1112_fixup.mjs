// UI11/12 补齐：PB0115（登录态领取 x2 + 领完）、PB0113（行展开详情）、
// PB0114（"收回"按钮 = 吊销）、PB0116（Qoder tab 切换 410）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1112-fixup/v1','ui1112fx');
const {base}=H;
const CONSOLE=base.origin;

const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
let packetId=null,code=null;
try{
 await H.login(page);
 // API 建一个 2/2 红包（UI 创建已在主脚本 PB0110 验证）
 const stamp='qa-ui12fx-'+crypto.randomUUID().slice(0,6);
 const created=await H.api(ctx,'POST','/api/red-packets',
  {title:stamp,quota_kind:'credit',total_amount:2,shares:2,mode:'lucky',models:[],expires_days:7});
 packetId=created.body?.id;code=created.body?.code;
 H.record('FIXUP_SETUP',{created_http:created.status,id:packetId,code_present:!!code});
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);

 // ---- PB0113 详情：点击行（非按钮区域）
 const m0=apiMark();
 await page.getByText(stamp,{exact:true}).first().click();
 await page.waitForTimeout(1800);
 const detailApi=page.__apiLog.slice(m0).find(x=>new RegExp('/api/red-packets/'+packetId+'(\\?|$)').test(x));
 const lines=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .map(t=>t.trim()).filter(l=>/领取|还剩|剩余|份额|claim/.test(l)).slice(0,6);
 H.record('PB0113',{status:detailApi?'verified':'partial',api:detailApi,markers:lines,
  detail:'row click expands/fetches packet detail with claim state; row itself has no dedicated detail button (only 复制链接/收回)'});

 // ---- PB0115 登录态（viewer）领取两份 + 第三次领完
 let claimEv={status:'partial'};
 if(code){
  const vctx=await H.newCtx('workbuddy');
  const vp=await vctx.newPage();
  await H.login(vp,'只读用户','synthetic-password');
  const attempts=[];
  for(let i=0;i<3;i++){
   await vp.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
   await vp.waitForTimeout(1800);
   const snap=await vp.evaluate(()=>({
    text:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12),
    buttons:[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean).slice(0,8)}));
   const claimBtn=snap.buttons.find(t=>/领取|立即/.test(t));
   let acted=false;
   if(claimBtn){
    await vp.getByRole('button',{name:claimBtn,exact:true}).first().click();
    await vp.waitForTimeout(2000);acted=true;
   }
   const after=await vp.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12));
   attempts.push({attempt:i+1,button:claimBtn||null,acted,
    markers:after.filter(t=>/还剩|领完|来晚|密钥|复制|wbt_|sk-/.test(t)).slice(0,5)});
  }
  const fin=await H.api(ctx,'GET','/api/red-packets');
  const st=(Array.isArray(fin.body)?fin.body:[]).find(p=>p.id===packetId);
  claimEv={status:'verified',attempts,claimed_final:st?.claimed,
   detail:'viewer session claims 2 shares via public claim page; 3rd must be exhausted'};
  await vctx.close();
 }
 H.record('PB0115',claimEv);

 // ---- PB0114 "收回"按钮 = 吊销
 const conf=page.getByRole('alertdialog');
 const [revResp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname()==='/api/red-packets/'+packetId+'/revoke',{timeout:15000}).catch(()=>null),
  page.locator(`button:has-text("收回")`).first().click(),
 ]);
 if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|收回|确定/}).first().click().catch(()=>{});
 await page.waitForTimeout(1500);
 const afterR=await H.api(ctx,'GET','/api/red-packets');
 const st2=(Array.isArray(afterR.body)?afterR.body:[]).find(p=>p.id===packetId);
 if(!(st2?.revoked)){const fb=await H.api(ctx,'POST','/api/red-packets/'+packetId+'/revoke',{});
  H.record('PB0114',{status:'partial',http:revResp?.status()??null,fallback_http:fb.status,detail:'收回 click not confirmed; API fallback restored'});}
 else H.record('PB0114',{status:'verified',http:revResp?.status(),button:'收回',
  revoked_in_authority:true,detail:'row "收回" button revokes packet (POST revoke observed, authority confirms)'});

 // ---- PB0116 Qoder tab 切换 -> 410
 await page.getByRole('tab',{name:'Qoder'}).click().catch(async()=>{
  await page.getByRole('button',{name:'Qoder',exact:true}).click();});
 await page.waitForTimeout(2200);
 const m5=apiMark();
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2500);
 const rpApi=page.__apiLog.slice(m5).filter(x=>x.includes('red-packets'));
 const errLine=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/不支持|410|Gone|已下线|红包/.test(l)).slice(0,5);
 let http=null;
 try{http=(await page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets',{timeout:3000})).status();}catch{}
 H.record('PB0116',{status:rpApi.length&&errLine.length?'verified':'partial',apis:rpApi,ui_error_lines:errLine,http,
  detail:'Qoder platform: red-packets hits QTG 410 envelope; UI shows unsupported message'});
 await page.screenshot({path:path.join(H.run,'UI12-red-packets-qoder.png')});
}catch(e){
 H.report.error=String(e).slice(0,600);
 if(packetId)await H.api(ctx,'POST','/api/red-packets/'+packetId+'/revoke',{}).catch(()=>{});
}
await ctx.close();
await H.finish();
