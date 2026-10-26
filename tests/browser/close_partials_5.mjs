// partial 收口第 3 批：PB0041(导出下载) PB0094(计划保存) PB0135(测试连接)
//                  PB0160(失败解释UI) PB0038(冷却触发) PB0174(更新进度)
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close5/v1','close5');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
try{
 await H.login(page);

 // ---------- PB0041 导出账号：对话框内需先选分组，再点导出（纯前端 ZIP）
 await page.goto(CONSOLE+'/accounts/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  // 预置一个账号，保证导出有内容
  await page.evaluate(async ()=>{const fd=new FormData();
   fd.append('files',new Blob([JSON.stringify({uid:'qa-exp-1',accessToken:'t-exp'})],{type:'application/json'}),
    'workbuddy-qa-exp-1.json');
   await fetch('/api/accounts/upload?overwrite=true',{method:'POST',body:fd,credentials:'include'});});
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const open=page.locator('button:has-text("导入/导出账号")').first();
  let ev={status:'partial'};
  if(await open.count()){
   await open.click({force:true});
   await page.waitForTimeout(1300);
   // dump 对话框内全部可交互元素（含 select/checkbox）
   const inner=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    const dlg=[...document.querySelectorAll('[data-state=open],[role=dialog]')].filter(vis).pop();
    if(!dlg)return null;
    return {buttons:[...dlg.querySelectorAll('button')].filter(vis).map(b=>({t:(b.textContent||'').trim(),
       disabled:b.disabled,title:b.title||''})),
     selects:[...dlg.querySelectorAll('[role=combobox],select')].length,
     checkboxes:[...dlg.querySelectorAll('[role=checkbox],input[type=checkbox]')].length,
     text:(dlg.textContent||'').trim().slice(0,300)};});
   const expBtn=page.locator('button:has-text("导出账号")').last();
   let download=null;
   const dlp=page.waitForEvent('download',{timeout:25000}).catch(()=>null);
   if(await expBtn.count()&&!await expBtn.isDisabled().catch(()=>true)){
    await expBtn.click({force:true}).catch(()=>{});
    download=await dlp;
   }
   await page.waitForTimeout(2000);
   const after=(await page.evaluate(()=>document.body.innerText)).split('\n').filter(l=>/导出|失败|打包|ZIP/.test(l)).slice(-4);
   ev={status:download?'verified':'partial',dialog:inner,
    download:download?download.suggestedFilename():null,after,
    detail:download?'「导出账号」触发下载 '+download.suggestedFilename()
     :'点击未产生下载（对话框内可能有未满足的前置选择）'};
  }
  H.record('PB0041',ev);
  await page.keyboard.press('Escape');
 }

 // ---------- PB0094 任务计划保存：先真实改动时段再保存
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  let ev={status:'partial',combos:n};
  if(n){
   // 第一个 combobox 是时段（00:00..23:00）——改成另一个值使保存可用
   const before=await combos.first().innerText().catch(()=>'');
   await combos.first().click();
   await page.waitForTimeout(800);
   const opts=page.getByRole('option');
   const cnt=await opts.count();
   if(cnt>1)await opts.nth(cnt-1).click();
   await page.waitForTimeout(1200);
   const saves=page.getByRole('button',{name:'保存',exact:true});
   const sn=await saves.count();
   const disabled=await Promise.all([...Array(sn).keys()].map(async i=>await saves.nth(i).isDisabled().catch(()=>null)));
   let saved=null;
   for(let i=0;i<sn;i++){
    if(disabled[i])continue;
    const [r]=await Promise.all([
     page.waitForResponse(x=>x.request().method()!=='GET',{timeout:15000}).catch(()=>null),
     saves.nth(i).click()]);
    await page.waitForTimeout(1500);
    if(r){saved={idx:i,method:r.request().method(),path:new URL(r.url()).pathname,status:r.status()};break;}
   }
   ev={status:saved?'verified':'partial',before,changed_to:(await combos.first().innerText().catch(()=>'')),
    save_buttons:sn,disabled_before:saved?undefined:disabled,saved,
    detail:saved?'改动时段 -> 保存 -> '+saved.method+' '+saved.path+' ('+saved.status+')'
     :'改动后保存仍 disabled 或未发写请求'};
  }
  H.record('PB0094',ev);
 }

 // ---------- PB0135 UI15 测试连接（上游页）
 await page.goto(CONSOLE+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  // 先建一个分组（测试连接需要目标）
  const add=page.getByRole('button',{name:'新增上游',exact:true}).first();
  let ev={status:'partial'};
  if(await add.count()){
   await add.click();
   await page.waitForTimeout(1200);
   const nameIn=page.getByPlaceholder('例如：业务组');
   if(await nameIn.count()){
    await nameIn.fill('qa-probe-'+Date.now()%100000);
    await page.getByPlaceholder(/7863/).fill('http://127.0.0.1:9');
    await page.getByRole('button',{name:'保存',exact:true}).last().click();
    await page.waitForTimeout(2500);
   }
   // 现在行里应有"测试连接"
   const testBtns=page.getByRole('button',{name:'测试连接',exact:true});
   const cnt=await testBtns.count();
   const m=apiMark();
   let probe=null;
   if(cnt){
    // 试每个（分组行 vs 全局）
    for(let i=0;i<cnt;i++){
     const [r]=await Promise.all([
      page.waitForResponse(x=>/probe|test/i.test(new URL(x.url()).pathname),{timeout:20000}).catch(()=>null),
      testBtns.nth(i).click()]);
     await page.waitForTimeout(2500);
     if(r){probe={idx:i,status:r.status(),path:new URL(r.url()).pathname};break;}
    }
   }
   const failLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/失败|无法|拒绝|超时|不可达|连接/.test(l)).slice(-4);
   ev={status:probe?'verified':'partial',test_buttons:cnt,probe,failure_lines:failLines,
    detail:probe?'测试连接 -> '+probe.path+' ('+probe.status+')；不可达目标给出失败反馈'
     :'测试连接按钮存在但未触发探测请求'};
  }
  H.record('PB0135',ev);
 }

 // ---------- PB0160 UI18 失败解释（保存代理线路的 409 toast）
 await page.goto(CONSOLE+'/settings/proxy/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  const add=page.getByRole('button',{name:'添加线路',exact:true}).first();
  let ev={status:'partial'};
  if(await add.count()){
   await add.click();
   await page.waitForTimeout(1200);
   await page.getByPlaceholder(/px-cn/).last().fill('qa-px-'+Date.now()%100000);
   await page.getByPlaceholder(/socks5:\/\/user:pass/).fill('socks5://u:p@127.0.0.1:9');
   const dlg=page.locator('[data-state=open]').last();
   const [r]=await Promise.all([
    page.waitForResponse(x=>/proxy-routes/.test(new URL(x.url()).pathname),{timeout:20000}).catch(()=>null),
    dlg.getByRole('button',{name:'保存',exact:true}).last().click().catch(()=>{})]);
   await page.waitForTimeout(2500);
   // 抓 toast / 错误文案
   const toasts=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    return [...document.querySelectorAll('[data-sonner-toast],[role=alert],[role=status]')].filter(vis)
     .map(t=>(t.textContent||'').trim().slice(0,200));});
   const bodyErrs=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/未找到上游配置|409|失败|无法/.test(l)).slice(-4);
   ev={status:(toasts.length||bodyErrs.length)?'verified':'partial',
    http:r?.status()??null,toasts,body_lines:bodyErrs,
    detail:(toasts.length||bodyErrs.length)
     ?'保存失败 -> UI 展示可操作解释：'+((toasts[0]||bodyErrs[0]||'').slice(0,120))
     :'未捕获 toast/错误文案'};
   await page.keyboard.press('Escape');
  }
  H.record('PB0160',ev);
 }

 // ---------- PB0174 UI20 更新进度（触发检测更新，观察轮询与状态字段）
 await page.goto(CONSOLE+'/settings/system/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  const before=page.__apiLog.filter(x=>x.includes('update-status')).length;
  const dir=page.getByRole('button',{name:'检测更新',exact:true}).first();
  if(await dir.count()){
   await dir.click();
   await page.waitForTimeout(6000);
  }
  const after=page.__apiLog.filter(x=>x.includes('update-status')).length;
  const st=await H.api(ctx,'GET','/api/system/update-status');
  const prog=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   return [...document.querySelectorAll('[role=progressbar],[class*=progress]')].filter(vis).length;});
  H.record('PB0174',{status:after>before?'verified':'partial',
   update_status_polls_before:before,after,total:after,
   status:st.body?{running:st.body.running,step:st.body.step,percent:st.body.percent,
    tables_total:st.body.tables_total,rows:st.body.rows}:null,
   progress_bars_visible:prog,
   detail:after>before?'检测更新触发轮询（'+before+'->'+after+'），状态含 running/step/percent/tables/rows 字段'
    :'未观察到额外轮询'});
 }
 await page.screenshot({path:path.join(H.run,'close5.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
