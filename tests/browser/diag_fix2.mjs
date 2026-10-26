// 诊断：claim 页领取控件 / 收回按钮可点击性 / playground 模型弹层。
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-fix2/v1','diagfx2');
const {base}=H;
const CONSOLE=base.origin;

// 1) admin 建一个 active 红包
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
const stamp='qa-diag-'+crypto.randomUUID().slice(0,6);
const created=await H.api(ctx,'POST','/api/red-packets',
 {title:stamp,quota_kind:'credit',total_amount:2,shares:2,mode:'lucky',models:[],expires_days:7});
 const pid=created.body?.id,code=created.body?.code;
H.record('setup',{id:pid,code});

// 2) viewer 登录看 claim 页全部可交互元素
{
 const vctx=await H.newCtx('workbuddy');
 const vp=await vctx.newPage();
 await H.login(vp,'只读用户','synthetic-password');
 await vp.goto(CONSOLE+'/claim/?code='+code,{waitUntil:'domcontentloaded'});
 await vp.waitForTimeout(2500);
 const dump=await vp.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {lines:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,20),
   clickables:[...document.querySelectorAll('button,a,[role=button],[onclick],input,select')].filter(vis)
    .map(el=>({tag:el.tagName,text:(el.textContent||'').trim().slice(0,20)||el.value||el.placeholder||'',cls:String(el.className).slice(0,50)})).slice(0,15)};
 });
 H.record('claim_logged_in',dump);
 await vp.screenshot({path:H.run+'/claim-viewer.png'});
 await vctx.close();
}

// 3) 收回按钮：hover 行后点击，观察 alertdialog
{
 await page.goto(CONSOLE+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);
 const rowText=page.getByText(stamp,{exact:true}).first();
 await rowText.hover();
 await page.waitForTimeout(600);
 const btn=page.locator('button:has-text("收回")').first();
 const visible=await btn.isVisible().catch(()=>false);
 let dialogSeen=false,http=null;
 try{
  const [resp]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets/'+pid+'/revoke',{timeout:10000}).catch(()=>null),
   (async()=>{
    await btn.click({timeout:5000});
    const dlg=page.getByRole('alertdialog');
    await dlg.waitFor({timeout:4000}).catch(()=>{});
    if(await dlg.count())await dlg.getByRole('button').filter({hasText:/确认|收回|确定/}).first().click({timeout:3000}).catch(()=>{});
   })(),
  ]);
  http=resp?.status();dialogSeen=true;
 }catch(e){H.note('revoke-diag-error:'+String(e).slice(0,200));}
 const after=await H.api(ctx,'GET','/api/red-packets');
 const st=(Array.isArray(after.body)?after.body:[]).find(p=>p.id===pid);
 H.record('revoke_row',{hover_then_click:true,btn_visible_before_hover:null,visible_now:visible,
  dialog_clicked:dialogSeen,revoke_http:http,revoked_in_authority:st?.revoked});
 if(!st?.revoked){const fb=await H.api(ctx,'POST','/api/red-packets/'+pid+'/revoke',{});H.note('fallback revoke '+fb.status);}
}

// 4) playground 模型弹层：点击 chip 后 dump
{
 await page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2500);
 const chip=page.locator('button:has-text("fake-model")').first();
 await chip.click();
 await page.waitForTimeout(1500);
 const dump=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const popper=document.querySelector('[data-radix-popper-content-wrapper],[role=listbox],[role=menu],.popover,[data-side]');
  return {popper_found:!!popper,popper_text:popper?(popper.textContent||'').trim().slice(0,200):null,
   fakeLarge:[...document.querySelectorAll('*')].filter(el=>el.textContent?.trim()==='fake-model-large'&&el.children.length===0&&vis(el)).slice(0,3)
    .map(el=>({tag:el.tagName,cls:String(el.className).slice(0,60),role:el.getAttribute('role')})),
   newPanels:[...document.querySelectorAll('[data-state=open], [role=listbox], [data-radix-popper-content-wrapper]')]
    .filter(vis).map(el=>({tag:el.tagName,role:el.getAttribute('role'),text:(el.textContent||'').trim().slice(0,120)}))};
 });
 H.record('model_popper',dump);
 await page.screenshot({path:H.run+'/model-popper.png'});
}
await ctx.close();
console.log(JSON.stringify(H.report.interactions,null,1).slice(0,3000));
await H.finish();
