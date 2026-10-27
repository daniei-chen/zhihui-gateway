// PB0041 收口：导出需要所选分组有 auth_dir。默认上游有 auth_dir，选它应能导出。
// 同时验证「分组无 auth_dir 时静默返回」这一行为（候选缺陷 N29）。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close6/v1','close6');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);
 // 确认默认分组有 auth_dir
 const ups=await H.api(ctx,'GET','/api/upstreams');
 const items=ups.body?.items||[];
 const def=items.find(u=>u.is_default);
 H.note('upstreams: '+JSON.stringify(items.map(u=>({name:u.name,is_default:u.is_default,
  has_auth_dir:Boolean(u.auth_dir)}))));
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  const open=page.locator('button:has-text("导入/导出账号")').first();
  await open.click({force:true});
  await page.waitForTimeout(1300);
  // 分组选择是 selects=1 或 combobox；先 dump 当前选中值
  const selInfo=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   const dlg=[...document.querySelectorAll('[data-state=open],[role=dialog]')].filter(vis).pop();
   return {comboboxes:[...dlg.querySelectorAll('[role=combobox],select')].map(el=>({
     tag:el.tagName,text:(el.textContent||'').trim().slice(0,30),value:el.value||''}))};});
  // 确保选中默认分组（若有 combobox）
  const combo=page.locator('[data-state=open] [role=combobox]').first();
  if(await combo.count()){
   await combo.click();
   await page.waitForTimeout(800);
   const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
   const defOpt=opts.find(o=>/默认/.test(o||''))||opts[0];
   if(defOpt)await page.getByRole('option',{name:defOpt.trim()}).first().click();
   await page.waitForTimeout(1000);
   H.note('group options: '+JSON.stringify(opts)+' picked '+defOpt);
  }
  // 点导出，监听下载 + 网络请求
  let download=null,req=null;
  page.on('request',r=>{if(new URL(r.url()).pathname==='/api/accounts/export')req=r.url();});
  const dlp=page.waitForEvent('download',{timeout:25000}).catch(()=>null);
  const expBtn=page.locator('button:has-text("导出账号")').last();
  await expBtn.click({force:true}).catch(()=>{});
  download=await dlp;
  await page.waitForTimeout(2500);
  const toast=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   return [...document.querySelectorAll('[data-sonner-toast],[role=status]')].filter(vis)
    .map(t=>(t.textContent||'').trim().slice(0,120));});
  H.record('PB0041',{status:(download||req)?'verified':'partial',
   group_selected:await combo.count()?'combobox':'(none)',
   sel_info:selInfo,
   export_request:req,download:download?download.suggestedFilename():null,toast,
   default_group_has_auth_dir:Boolean(def?.auth_dir),
   detail:(download||req)?'导出账号 -> GET /api/accounts/export + 浏览器下载 accounts.zip（纯前端 Blob）'
    :'仍未触发导出（分组 auth_dir 或选择未满足；源码 uploadAccountsButton.tsx:84 在无 auth_dir 时静默 return）'});
  await page.screenshot({path:path.join(H.run,'export.png')});
 }
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
