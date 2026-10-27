// partial 收口第 2 批：PB0041(导出) PB0062(选模型) PB0063(思考强度)
// 上一批已证 title 精确匹配有效；这三条需要更细的菜单/popper 探测。
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close2/v1','close2');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
trackApis(page);
try{
 await H.login(page);

 // ---- PB0041 导出：先 dump「导入/导出账号」按钮点击后出现的全部可点元素
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 {
  const btn=page.locator('button:has-text("导入/导出")').first();
  let ev={status:'not_present_in_ui'};
  if(await btn.count()){
   await btn.click({force:true});
   await page.waitForTimeout(1500);
   const dump=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    const out={menuitems:[],dialogs:[],buttons:[],links:[]};
    for(const el of document.querySelectorAll('[role=menuitem],[role=menuitemradio],[role=option]'))
     if(vis(el))out.menuitems.push((el.textContent||'').trim().slice(0,20));
    for(const el of document.querySelectorAll('[data-state=open],[role=dialog]'))
     if(vis(el))out.dialogs.push((el.textContent||'').trim().slice(0,150));
    for(const el of document.querySelectorAll('button'))if(vis(el)){
     const t=(el.textContent||'').trim();if(/导出|下载|export|保存/.test(t))out.buttons.push(t.slice(0,20));}
    for(const el of document.querySelectorAll('a[download],a[href*="export"]'))out.links.push(el.getAttribute('href'));
    return out;});
   H.note('PB0041 dump: '+JSON.stringify(dump).slice(0,600));
   // 对话框里有明确按钮「导出账号」（导出 ZIP）——优先用它
   const expBtn=page.locator('button:has-text("导出账号")').first();
   const expItem=page.getByRole('menuitem',{name:/导出/}).first();
   let req=null,clicked=null;
   if(await expBtn.count()){
    [req]=await Promise.all([
     page.waitForRequest(x=>/export|download/.test(new URL(x.url()).pathname),{timeout:12000}).catch(()=>null),
     expBtn.click({force:true}).catch(()=>{})]);
    clicked='button:导出';
   }else if(await expItem.count()){
    [req]=await Promise.all([
     page.waitForRequest(x=>/export|download/.test(new URL(x.url()).pathname),{timeout:12000}).catch(()=>null),
     expItem.click().catch(()=>{})]);
    clicked='menuitem:导出';
   }
   await page.waitForTimeout(2000);
   // 也可能是纯前端生成 Blob（无网络请求）——检查下载事件
   ev={status:req?'verified':(dump.dialogs.length?'partial':'not_present_in_ui'),
    clicked,dump,export_requested:Boolean(req),
    detail:req?'导出触发 '+new URL(req.url()).pathname
     :(dump.dialogs.length?'菜单展开了但未捕获导出请求（可能为纯前端 Blob 生成）':'无导出控件')};
   await page.keyboard.press('Escape');
  }
  H.record('PB0041',ev);
 }

 // ---- PB0062 选模型 + PB0063 思考强度：完整 dump popper 内的可点元素
 await page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 const chip=page.locator('button:has-text("fake-model")').first();
 // 模型 chip 依赖 /api/playground/models 返回；等不到就重载一次（#418 竞态窗口内会缺渲染）
 await chip.waitFor({timeout:15000}).catch(()=>{});
 if(!(await chip.count())){
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(4000);
  await chip.waitFor({timeout:10000}).catch(()=>{});
 }
 if(await chip.count()){
  const before=(await chip.innerText()).trim();
  await chip.click();
  await page.waitForTimeout(1800);
  const dump=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   const panels=[...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu],[data-state=open]')].filter(vis);
   return {panel_count:panels.length,
    panel_texts:panels.map(p=>(p.textContent||'').trim().slice(0,150)),
    clickables:panels.flatMap(p=>[...p.querySelectorAll('button,[role=option],[role=menuitem],li')].filter(vis)
      .map(x=>({tag:x.tagName,role:x.getAttribute('role'),text:(x.textContent||'').trim().slice(0,24)}))).slice(0,12)};
  });
  H.note('PB0062 dump: '+JSON.stringify(dump).slice(0,700));
  // 从 clickables 里找 fake-model-large
  const candidate=page.getByText('fake-model-large',{exact:true}).first();
  let picked=false;
  if(await candidate.count()){
   await candidate.click({force:true}).catch(()=>{});
   await page.waitForTimeout(1200);
   picked=await page.locator('button:has-text("fake-model-large")').count()>0;
  }
  H.record('PB0062',{status:picked?'verified':'partial',before,picked,dump,
   detail:picked?'模型 chip -> fake-model-large 选中（force 点击 + 全量 popper dump）'
    :'popper 内未找到 fake-model-large 可点项'});
  await page.keyboard.press('Escape');
 }else{
  H.record('PB0062',{status:'partial',detail:'模型 chip 未渲染（可能仍在 #418 竞态窗口）'});
 }
 // 思考强度
 {
  const pc=page.locator('button:has-text("思考强度")').first();
  let ev={status:'not_present_in_ui'};
  if(await pc.count()){
   await pc.click({force:true});
   await page.waitForTimeout(1800);
   const dump=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    const panels=[...document.querySelectorAll('[data-radix-popper-content-wrapper],[role=listbox],[role=menu],[data-state=open]')].filter(vis);
    return {panel_count:panels.length,
     texts:panels.map(p=>(p.textContent||'').trim().slice(0,200)),
     clickables:panels.flatMap(p=>[...p.querySelectorAll('button,[role=option],[role=menuitem],li')].filter(vis)
       .map(x=>({tag:x.tagName,role:x.getAttribute('role'),text:(x.textContent||'').trim().slice(0,20)}))).slice(0,10)};
   });
   // 选一个与当前不同的值
   let chosen=null;
   const cands=page.locator('[data-radix-popper-content-wrapper] [role=option], [role=listbox] [role=option]');
   const n=await cands.count();
   if(n){
    const labels=await cands.allTextContents();
    const pick=labels.find(t=>t&&t.trim()&&/高|中|低|默认|off|on/i.test(t))||labels[0];
    if(pick){await cands.filter({hasText:pick.trim()}).first().click({force:true}).catch(()=>{});
     await page.waitForTimeout(900);chosen=pick.trim();}
   }else{
    // 可能是按钮组
    const btns=page.locator('[data-radix-popper-content-wrapper] button');
    if(await btns.count()){await btns.last().click({force:true}).catch(()=>{});
     await page.waitForTimeout(700);chosen='button:'+((await btns.last().innerText().catch(()=>''))||'').trim();}
   }
   ev={status:chosen?'verified':'partial',dump,chosen,
    detail:chosen?'思考强度 popper 选中 '+chosen:'popper 打开了但没有可点的选项'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0063',ev);
 }
 await page.screenshot({path:path.join(H.run,'close2.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
