// 探：settings/upstream 新增上游表单 + settings/models 添加映射表单的真实结构。
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-settings/v1','diagst');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
async function dumpPanel(path,btnName,key){
 await page.goto(base.origin+path,{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2600);
 const btn=page.getByRole('button',{name:btnName,exact:true});
 if(await btn.count()===0){H.record(key,{button_found:false});return;}
 await btn.first().click();
 await page.waitForTimeout(1500);
 const dump=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const panel=document.querySelector('[role=dialog],[data-radix-popper-content-wrapper],[data-state=open]')||document.body;
  return {role:panel.getAttribute('role')||null,
   inputs:[...document.querySelectorAll('input,textarea,select')].filter(vis)
    .map(el=>({type:el.type,ph:el.placeholder||'',label:el.labels?.[0]?.textContent?.trim()||'',value:String(el.value).slice(0,20)})),
   buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(-12),
   lines:(panel.textContent||'').split('\n').map(t=>t.trim()).filter(Boolean).slice(0,14)};});
 H.record(key,{button_found:true,...dump});
 await page.screenshot({path:H.run+'/'+key+'.png'});
 await page.keyboard.press('Escape');
 await page.waitForTimeout(400);
}
await H.login(page);
await dumpPanel('/settings/upstream/','新增上游','upstream_add');
await dumpPanel('/settings/models/','添加映射','modelmap_add');
await ctx.close();
const r=H.report.interactions;
console.log('UPSTREAM:',JSON.stringify(r.upstream_add,null,.5).slice(0,1500));
console.log('MODELMAP:',JSON.stringify(r.modelmap_add,null,.5).slice(0,1200));
await H.finish();
