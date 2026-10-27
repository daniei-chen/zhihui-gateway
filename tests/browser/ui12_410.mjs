// PB0116 单项：Qoder 平台下红包 410 + UI 文案。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui12-410/v1','ui12q410');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
try{
 await H.login(page);
 await page.goto(base.origin+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2000);
 const qTab=page.getByRole('tab',{name:'Qoder'});
 await (await qTab.count()?qTab:page.getByRole('button',{name:'Qoder',exact:true})).first().click();
 await page.waitForTimeout(2000);
 const m=apiMark();
 await page.reload({waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2500);
 const apis=page.__apiLog.slice(m).filter(x=>x.includes('red-packets'));
 let http=null;try{http=(await page.waitForResponse(r=>new URL(r.url()).pathname==='/api/red-packets',{timeout:2500})).status();}catch{}
 const errs=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/不支持|410|Gone|已下线/.test(l)).slice(0,6);
 H.record('PB0116',{status:apis.length&&errs.length?'verified':'partial',apis,http,ui_error_lines:errs,
  detail:'Qoder platform: red-packets hits QTG 410; UI shows unsupported message'});
 await page.screenshot({path:path.join(H.run,'UI12-qoder-410.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
