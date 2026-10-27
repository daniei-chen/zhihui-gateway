// N27 复核：任务页开关改的是 draft，需点「保存」才提交（不是静默失效）。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-n27/v1','n27');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const sws=page.locator('[role=switch]');
 const n=await sws.count();
 H.note('switches: '+n);
 let ev={status:'partial',switches:n};
 if(n){
  // 1) 拨开关：应只改草稿（无写请求）
  const m1=apiMark();
  const before=await sws.first().getAttribute('data-state');
  await sws.first().click();
  await page.waitForTimeout(2000);
  const writesAfterToggle=page.__apiLog.slice(m1).filter(x=>!x.startsWith('GET'));
  const after=await sws.first().getAttribute('data-state');
  // 2) 点保存：应产生写请求
  let saved=null,writesAfterSave=[];
  const saves=page.getByRole('button',{name:'保存',exact:true});
  const sn=await saves.count();
  for(let i=0;i<sn;i++){
   if(await saves.nth(i).isDisabled().catch(()=>true))continue;
   const m2=apiMark();
   const [r]=await Promise.all([
    page.waitForResponse(x=>x.request().method()!=='GET',{timeout:15000}).catch(()=>null),
    saves.nth(i).click()]);
   await page.waitForTimeout(1800);
   writesAfterSave=page.__apiLog.slice(m2).filter(x=>!x.startsWith('GET'));
   if(r){saved={idx:i,method:r.request().method(),path:new URL(r.url()).pathname,
    status:r.status(),body:(await r.text().catch(()=>'')).slice(0,150)};break;}
  }
  // 3) 刷新后开关状态是否保持（持久化验证）
  let persisted=null;
  if(saved){
   await page.reload({waitUntil:'domcontentloaded'});
   await page.waitForTimeout(3500);
   const sw2=page.locator('[role=switch]').first();
   persisted=await sw2.getAttribute('data-state').catch(()=>null);
  }
  ev={status:saved?'verified':'partial',switches:n,
   toggle_changes_draft_only:writesAfterToggle.length===0,
   draft_state:{before,after},
   save_buttons:sn,saved,writes_after_save:writesAfterSave.slice(0,3),
   state_after_reload:persisted,
   detail:saved?'开关改草稿（无写请求）-> 点保存 -> '+saved.method+' '+saved.path+
     ' ('+saved.status+')；刷新后状态='+persisted
    :'保存未产生写请求'};
  H.note('toggle writes: '+JSON.stringify(writesAfterToggle.slice(0,3)));
 }
 H.record('N27',ev);
 await page.screenshot({path:path.join(H.run,'n27.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
