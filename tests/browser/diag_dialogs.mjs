// Probe: what opens after clicking 添加 (security) and 发红包 (red-packets)?
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-dialogs/v1','diagdlg');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
await H.login(page);
async function probeClick(path,btnName,out){
 await page.goto(base.origin+path,{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2200);
 const btn=page.getByRole('button',{name:btnName,exact:true});
 const n=await btn.count();
 if(!n){H.record(out,{found:false});return;}
 const before=(await page.evaluate(()=>document.body.innerHTML)).length;
 await btn.first().click();
 await page.waitForTimeout(1800);
 const state=await page.evaluate(()=>{
  const sels=['[role=dialog]','[role=alertdialog]','[data-state=open]','[class*=Dialog]','[class*=Sheet]','form'];
  const found={};
  for(const s of sels)found[s]=document.querySelectorAll(s).length;
  const dialog=document.querySelector('[role=dialog],[data-state=open]');
  return {counts:found,openText:dialog?(dialog.textContent||'').trim().slice(0,300):null,
   openTag:dialog?.tagName,openCls:String(dialog?.className).slice(0,100)};
 });
 H.record(out,{button_count:n,html_growth:(await page.evaluate(()=>document.body.innerHTML)).length-before,...state});
 await page.keyboard.press('Escape');
 await page.waitForTimeout(500);
}
await probeClick('/security/','添加','security_add');
await probeClick('/red-packets/','发红包','redpacket_create');
await ctx.close();
console.log(JSON.stringify(H.report.interactions,null,1));
await H.finish();
