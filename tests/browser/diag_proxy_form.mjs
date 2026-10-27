// 探：代理页 添加线路 表单结构。
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-proxy/v1','diagpx');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
await page.goto(base.origin+'/settings/proxy/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(2600);
await page.getByRole('button',{name:'添加线路',exact:true}).first().click();
await page.waitForTimeout(1500);
const dump=await page.evaluate(()=>{
 const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
 return {inputs:[...document.querySelectorAll('input,textarea,select')].filter(vis)
   .map(el=>({type:el.type,ph:el.placeholder||'',label:el.labels?.[0]?.textContent?.trim()||'',val:String(el.value).slice(0,15)})),
  buttons:[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim()).filter(Boolean).slice(-10),
  lines:(document.querySelector('[data-state=open]')||document.body).textContent.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,12)};
});
H.record('proxy_form',dump);
console.log(JSON.stringify(dump,null,1).slice(0,1600));
await page.screenshot({path:H.run+'/proxy-form.png'});
await ctx.close();
await H.finish();
