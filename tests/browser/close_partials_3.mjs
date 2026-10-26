// 极简：只处理 PB0041(导出下载) 与 PB0062/PB0063(playground popper)。
// 每步都单独 dump，失败也有可读证据。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close3/v1','close3');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);

 // ---------- PB0041：导出账号 = 纯前端生成 ZIP 并触发下载
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 {
  const open=page.locator('button:has-text("导入/导出账号")').first();
  let ev={status:'not_present_in_ui'};
  if(await open.count()){
   await open.click({force:true});
   await page.waitForTimeout(1200);
   const expBtn=page.locator('button:has-text("导出账号")').first();
   const n=await expBtn.count();
   if(n){
    // 监听下载（Blob 下载不会产生网络请求）
    let download=null;
    const dlPromise=page.waitForEvent('download',{timeout:20000}).catch(()=>null);
    // 同时监听可能的 zip 生成请求
    let req=null;
    page.on('request',r=>{if(/export|zip|download/i.test(r.url()))req=r.url();});
    await expBtn.click({force:true}).catch(()=>{});
    download=await dlPromise;
    await page.waitForTimeout(2500);
    const after=(await page.evaluate(()=>document.body.innerText)).split('\n')
     .filter(l=>/导出|打包|失败|成功/.test(l)).slice(-4);
    ev={status:download?'verified':'partial',
     export_button_found:true,
     download:{suggested:download?download.suggestedFilename():null},
     request_url:req,after_lines:after,
     detail:download?'「导出账号」触发浏览器下载 '+download.suggestedFilename()
      :'点击后未触发下载事件（需确认是否另有确认步骤）'};
   }else ev={status:'partial',detail:'对话框内无「导出账号」按钮'};
  }
  H.record('PB0041',ev);
  await page.keyboard.press('Escape');
 }

 // ---------- PB0062/PB0063：playground 模型 popover
 await page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(5000);
 {
  const info=await page.evaluate(()=>{
   const btns=[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean);
   return {buttons:btns.slice(0,20),hasTextarea:document.querySelectorAll('textarea').length};});
  H.note('playground buttons: '+JSON.stringify(info).slice(0,400));
  // 找模型 chip：可能是含 fake-model 文本的按钮，也可能是别的名字
  const chip=page.locator('button').filter({hasText:'fake-model'}).first();
  let ev62={status:'partial',buttons:info.buttons};
  if(await chip.count()){
   await chip.click({force:true});
   await page.waitForTimeout(1800);
   const dump=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    const all=[...document.querySelectorAll('*')].filter(el=>vis(el)&&el.children.length===0
      &&/fake-model/.test(el.textContent||''));
    return {matches:all.map(el=>({tag:el.tagName,cls:String(el.className).slice(0,50),
      text:(el.textContent||'').trim().slice(0,30),
      clickable:Boolean(el.closest('button,[role=option],[role=menuitem]'))})).slice(0,8)};});
   const target=page.getByText('fake-model-large',{exact:true}).first();
   let picked=false;
   if(await target.count()){
    const box=await target.boundingBox().catch(()=>null);
    await target.click({force:true}).catch(()=>{});
    await page.waitForTimeout(1200);
    picked=await page.locator('button').filter({hasText:'fake-model-large'}).count()>0;
    ev62={status:picked?'verified':'partial',before_chip:'fake-model',picked,dump,
     target_box:box,detail:picked?'chip -> large 选中':'点击了但 chip 未更新'};
   }else ev62={status:'partial',dump,detail:'popover 内无 fake-model-large 文本节点'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0062',ev62);

  // 思考强度
  const pc=page.locator('button').filter({hasText:'思考强度'}).first();
  let ev63={status:'not_present_in_ui'};
  if(await pc.count()){
   await pc.click({force:true});
   await page.waitForTimeout(1800);
   const dump=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    const panels=[...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu]')].filter(vis);
    return {panels:panels.length,
     options:panels.flatMap(p=>[...p.querySelectorAll('[role=option],[role=menuitem],button')].filter(vis)
      .map(x=>({role:x.getAttribute('role'),text:(x.textContent||'').trim().slice(0,16)}))).slice(0,10),
     text:panels.map(p=>(p.textContent||'').trim().slice(0,120))};});
   let chosen=null;
   const opt=page.locator('[data-radix-popper-content-wrapper] [role=option], [role=listbox] [role=option]').last();
   if(await opt.count()){
    chosen=(await opt.innerText().catch(()=>'')).trim();
    await opt.click({force:true}).catch(()=>{});
    await page.waitForTimeout(800);
   }
   ev63={status:chosen?'verified':'partial',dump,chosen,
    detail:chosen?'选择 '+chosen:'popper 无可点选项'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0063',ev63);
 }
 await page.screenshot({path:path.join(H.run,'close3.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
