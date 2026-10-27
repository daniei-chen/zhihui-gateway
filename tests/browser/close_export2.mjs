// 验证 PB0041：导出需二次确认（ConfirmDialog）。点导出 -> 确认 -> 下载。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-export2/v1','exp2');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);
 await page.goto(base.origin+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 const open=page.locator('button:has-text("导入/导出账号")').first();
 await open.click({force:true});
 await page.waitForTimeout(1300);
 // 第一次点击"导出账号" -> 应弹二次确认
 const exp1=page.locator('button:has-text("导出账号")').last();
 const confirmVisibleBefore=await page.getByRole('alertdialog').count();
 await exp1.click({force:true});
 await page.waitForTimeout(1200);
 const dlg=page.getByRole('alertdialog');
 const dlgCount=await dlg.count();
 const dlgText=dlgCount?(await dlg.innerText()).slice(0,220):'';
 // 在确认框里点"导出账号"
 let req=null,download=null;
 if(dlgCount){
  const dlp=page.waitForEvent('download',{timeout:25000}).catch(()=>null);
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/accounts/export')req=r.url();});
  const confirmBtn=dlg.getByRole('button',{name:/导出|确认/}).last();
  await confirmBtn.click({force:true}).catch(()=>{});
  download=await dlp;
  await page.waitForTimeout(2500);
 }
 const toast=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return [...document.querySelectorAll('[data-sonner-toast],[role=status]')].filter(vis)
   .map(t=>(t.textContent||'').trim().slice(0,120));});
 H.record('PB0041',{status:(download||req)?'verified':'partial',
  confirm_dialog_appeared:dlgCount>0,
  confirm_visible_before_click:confirmVisibleBefore>0,
  dialog_text:dlgText.replace(/\n+/g,'|'),
  export_request:req,download:download?download.suggestedFilename():null,toast,
  detail:(download||req)?'导出需二次确认（ConfirmDialog）-> 确认后 GET /api/accounts/export + 下载 ZIP'
   :'确认后仍未触发导出'});
 await page.screenshot({path:path.join(H.run,'export2.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
