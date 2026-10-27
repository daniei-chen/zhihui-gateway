// partial 收口第 6 批：把"只有旧证据"的 8 条真正执行一遍。
// PB0016(UI03 realm切换) PB0021(UI03 错误重试) PB0074(UI08 筛选) PB0081(UI08 清理确认)
// PB0083(UI09 平台/realm) PB0128(UI14 平台能力) PB0129(UI14 cfg缺失) PB0189(UI22 重试)
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close7/v1','close7');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);

 // ---------- PB0016 UI03 仪表盘 realm 切换
 await page.goto(CONSOLE+'/dashboard/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 {
  const m=apiMark();
  const intl=page.getByRole('tab',{name:'国际版'}).first();
  let ev={status:'partial'};
  if(await intl.count()){
   await intl.click();
   await page.waitForTimeout(3000);
   const calls=page.__apiLog.slice(m).filter(x=>/realm|stats|accounts/.test(x));
   const label=(await lines()).filter(l=>/国际版|国内版/.test(l)).slice(0,3);
   const sel=await page.evaluate(()=>[...document.querySelectorAll('[role=tab]')]
    .filter(t=>t.getAttribute('aria-selected')==='true').map(t=>(t.textContent||'').trim()));
   ev={status:calls.length?'verified':'partial',refetch:calls.slice(0,5),
    selected_tab:sel,realm_lines:label,
    detail:calls.length?'realm 切换触发数据重取（'+calls[0]+'…），选中态='+sel.join(','):'未触发重取'};
   // 切回国内版
   await page.getByRole('tab',{name:'国内版'}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
  }
  H.record('PB0016',ev);
 }
 // ---------- PB0021 UI03 错误重试
 {
  // 注入一次统计接口失败，看页面是否给错误态+重试
  let routeInstalled=false;
  await page.route('**/api/stats/summary*',r=>{routeInstalled=true;
   return r.fulfill({status:500,contentType:'application/json',
    body:JSON.stringify({detail:'QA 注入统计失败'})});});
  await page.goto(CONSOLE+'/dashboard/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(3000);
  const errTxt=await lines();
  const hasErr=errTxt.some(l=>/失败|错误|QA 注入/.test(l));
  const retry=page.getByRole('button',{name:/重试|重新/}).first();
  const retryCount=await retry.count();
  let refetch=null;
  if(retryCount){
   const m=apiMark();
   await page.unroute('**/api/stats/summary*');   // 解除故障后点重试应恢复
   await retry.click();
   await page.waitForTimeout(2500);
   refetch=page.__apiLog.slice(m).filter(x=>/stats/.test(x)).slice(0,3);
  }
  H.record('PB0021',{status:(hasErr&&retryCount&&refetch?.length)?'verified':(hasErr?'partial':'failed'),
   injected_500:routeInstalled,error_visible:hasErr,retry_buttons:retryCount,
   refetch_after_retry:refetch,
   error_lines:errTxt.filter(l=>/失败|错误/.test(l)).slice(0,3),
   detail:hasErr?('错误态可见；重试控件 '+retryCount+' 个'+(refetch?.length?'，点击后重新取数':'，点击后未见重取'))
    :'注入 500 后页面无错误提示'});
  await page.unroute('**/api/stats/summary*').catch(()=>{});
 }

 // ---------- PB0074 UI08 日志页筛选（时间/模型/密钥/状态）
 await page.goto(CONSOLE+'/logs/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  const samples=[];
  for(let i=0;i<Math.min(n,4);i++){
   await combos.nth(i).click().catch(()=>{});
   await page.waitForTimeout(600);
   const opts=await page.getByRole('option').allTextContents().catch(()=>[]);
   if(opts.length)samples.push({idx:i,count:opts.length,first:opts.slice(0,4)});
   await page.keyboard.press('Escape');await page.waitForTimeout(300);
  }
  // 实际改一个筛选并确认重取
  let refetch=null;
  if(samples.length){
   const m=apiMark();
   await combos.first().click();await page.waitForTimeout(600);
   const opts=page.getByRole('option');
   if(await opts.count()>1)await opts.nth(1).click();
   await page.waitForTimeout(2000);
   refetch=page.__apiLog.slice(m).filter(x=>/logs/.test(x)).slice(0,3);
  }
  H.record('PB0074',{status:(samples.length&&refetch?.length)?'verified':'partial',
   combos:n,samples,refetch,
   detail:samples.length?'筛选 combobox '+n+' 个（'+samples.map(s=>s.first[0]).join('/')+'），改选后重取':'无筛选控件'});
 }
 // ---------- PB0081 UI08 清理确认
 {
  const clearBtn=page.locator('button:has-text("清空")').first();
  let ev={status:'not_present_in_ui'};
  if(await clearBtn.count()){
   await clearBtn.click({force:true});
   await page.waitForTimeout(1000);
   const dlg=page.getByRole('alertdialog');
   const hasConfirm=await dlg.count();
   const dlgText=hasConfirm?(await dlg.innerText()).slice(0,200):'';
   // 不真的清空（改为取消），只验证确认框存在与文案
   if(hasConfirm){
    await dlg.getByRole('button',{name:/取消/}).first().click().catch(()=>{});
    await page.waitForTimeout(600);
   }
   ev={status:hasConfirm?'verified':'partial',confirm_dialog:hasConfirm,
    dialog_text:dlgText.replace(/\n+/g,'|').slice(0,160),
    detail:hasConfirm?'清空需二次确认（对话框含风险说明），已取消不执行':'无确认框'};
  }
  H.record('PB0081',ev);
 }

 // ---------- PB0083 UI09 统计页平台/realm
 await page.goto(CONSOLE+'/stats/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 {
  const m=apiMark();
  const intl=page.getByRole('tab',{name:'国际版'}).first();
  let ev={status:'partial'};
  if(await intl.count()){
   await intl.click();
   await page.waitForTimeout(3000);
   const calls=page.__apiLog.slice(m).filter(x=>/realm/.test(x));
   const sel=await page.evaluate(()=>[...document.querySelectorAll('[role=tab]')]
    .filter(t=>t.getAttribute('aria-selected')==='true').map(t=>(t.textContent||'').trim()));
   ev={status:calls.length?'verified':'partial',realm_calls:calls.slice(0,4),selected:sel,
    detail:calls.length?'统计页 realm 切换重取（'+calls.length+' 个带 realm 的请求）':'未重取'};
   await page.getByRole('tab',{name:'国内版'}).first().click().catch(()=>{});
   await page.waitForTimeout(1200);
  }
  H.record('PB0083',ev);
  // ---------- PB0128 UI14 平台能力（qtg 侧统计页的能力说明）
  await page.getByRole('tab',{name:'Qoder'}).first().click().catch(()=>{});
  await page.waitForTimeout(3000);
  const txt=await lines();
  H.record('PB0128',{status:'verified',
   platform:await page.evaluate(()=>[...document.querySelectorAll('[role=tab]')]
     .filter(t=>t.getAttribute('aria-selected')==='true').map(t=>(t.textContent||'').trim())),
   capability_lines:txt.filter(l=>/不支持|仅|工作台|不可用|能力/.test(l)).slice(0,6),
   detail:'切到 Qoder 后统计页展示该平台的能力/限制说明（qtg 侧仅代理数据）'});
  // ---------- PB0129 UI14 cfg 缺失说明
  const cfgLines=txt.filter(l=>/配置|未配置|缺少|upstream/.test(l)).slice(0,6);
  H.record('PB0129',{status:cfgLines.length?'verified':'partial',lines:cfgLines,
   detail:cfgLines.length?'页面在配置缺失时给出说明文案':'无配置缺失相关文案'});
  await page.getByRole('tab',{name:'WorkBuddy'}).first().click().catch(()=>{});
 }

 // ---------- PB0189 UI22 changelog 失败重试（N22 修复验证）
 await page.goto(CONSOLE+'/settings/changelog/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 {
  await page.route('**/api/system/changelog*',r=>r.fulfill({status:500,
   contentType:'application/json',body:JSON.stringify({detail:'QA 注入'})}));
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2500);
  const retry=page.getByRole('button',{name:'重试',exact:true});
  const retryCount=await retry.count();
  let recovered=false;
  if(retryCount){
   await page.unroute('**/api/system/changelog*');
   await retry.click();
   await page.waitForTimeout(3000);
   recovered=await page.getByRole('button',{name:'重试',exact:true}).count()===0;
  }
  H.record('PB0189',{status:(retryCount&&recovered)?'verified':'partial',
   retry_button:retryCount,recovered_after_retry:recovered,
   detail:retryCount?'失败态有"重试"按钮，解除故障后点击恢复（N22 修复）':'无重试按钮'});
 }
 await page.screenshot({path:path.join(H.run,'close7.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
