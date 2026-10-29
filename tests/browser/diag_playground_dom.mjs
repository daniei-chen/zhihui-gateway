// One-shot DOM probe: what does a rendered playground round look like?
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-playground/v1','diagpg');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
await page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'});
await page.waitForTimeout(2500);
const ta=page.locator('textarea').first();
await ta.fill('QA探针：请回复"收到"');
await ta.press('Enter');
await page.waitForTimeout(4000);
const probe=await page.evaluate(()=>{
 const main=document.querySelector('main')||document.body;
 const grab=(el)=>({tag:el.tagName,cls:String(el.className).slice(0,120),
  text:(el.textContent||'').trim().slice(0,80)});
 const out={messages:[],buttons:[],inputs:[],icons:[]};
 // find nodes mentioning the reply / usage
 for(const el of main.querySelectorAll('*')){
  const t=(el.textContent||'').trim();
  if(/tokens/.test(t)&&t.length<40&&el.children.length<3)out.messages.push({...grab(el),kind:'usage'});
  if(/收到/.test(t)&&t.length<60&&el.children.length<=2)out.messages.push({...grab(el),kind:'reply'});
 }
 for(const b of main.querySelectorAll('button'))out.buttons.push(grab(b));
 for(const i of main.querySelectorAll('input,textarea,[role=slider]'))out.inputs.push(grab(i));
 return out;
});
H.record('DOM',{probe});
await page.screenshot({path:H.run.replace(/[\\/]$/,'')+'/probe.png'});
console.log(JSON.stringify(probe,null,1).slice(0,4000));
await ctx.close();
await H.finish();
