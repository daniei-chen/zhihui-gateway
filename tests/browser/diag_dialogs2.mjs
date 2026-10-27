// Probe2: dump the form controls that appear after 添加 / 发红包 clicks.
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-dialogs2/v1','diagdlg2');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
async function probeClick(path,btnName,out){
 await page.goto(base.origin+path,{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);
 await page.getByRole('button',{name:btnName,exact:true}).first().click();
 await page.waitForTimeout(1800);
 const state=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const inputs=[...document.querySelectorAll('input,textarea,select')].filter(vis)
   .map(el=>({tag:el.tagName,type:el.type,ph:el.placeholder||'',id:el.id||'',
    label:el.labels?.[0]?.textContent?.trim()||'',value:el.value}));
  const buttons=[...document.querySelectorAll('button')].filter(vis)
   .map(b=>b.textContent.trim()).filter(Boolean);
  const headings=[...document.querySelectorAll('h1,h2,h3,[class*=title]')].filter(vis)
   .map(h=>h.textContent.trim()).filter(Boolean).slice(0,10);
  return {inputs,buttons:buttons.slice(-15),headings};
 });
 H.record(out,state);
 await page.screenshot({path:H.run+'/'+out+'.png'});
 await page.keyboard.press('Escape');
 await page.waitForTimeout(400);
}
await probeClick('/security/','添加','security_add');
await probeClick('/red-packets/','发红包','redpacket_create');
await ctx.close();
const r=H.report.interactions;
console.log('SEC inputs:',JSON.stringify(r.security_add.inputs));
console.log('SEC buttons:',JSON.stringify(r.security_add.buttons));
console.log('RP inputs:',JSON.stringify(r.redpacket_create.inputs));
console.log('RP buttons:',JSON.stringify(r.redpacket_create.buttons));
console.log('RP headings:',JSON.stringify(r.redpacket_create.headings));
await H.finish();
