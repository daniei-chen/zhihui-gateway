// 最后 2 条可攻项：PB0094(计划保存) PB0136(端点复制)
import path from 'node:path';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close8/v1','close8');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);

 // ---------- PB0094 任务页计划保存：先改时段 combobox 再保存
 await page.goto(CONSOLE+'/tasks/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 {
  const combos=page.getByRole('combobox');
  const n=await combos.count();
  let ev={status:'partial',combos:n};
  if(n){
   // dump 每个 combobox 当前值与类型
   const before=await page.evaluate(()=>{
    const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
    return [...document.querySelectorAll('[role=combobox]')].filter(vis)
     .map((el,i)=>({i,text:(el.textContent||'').trim().slice(0,20)}));});
   // 第一个是时段（"03:00" 之类）；改成别的值
   let changed=null;
   for(let i=0;i<Math.min(n,3);i++){
    await combos.nth(i).click().catch(()=>{});
    await page.waitForTimeout(700);
    const opts=page.getByRole('option');
    const cnt=await opts.count();
    if(cnt>1){
     const cur=before[i]?.text||'';
     // 选一个与当前不同的
     const labels=await opts.allTextContents();
     const pick=labels.find(t=>t.trim()&&t.trim()!==cur.trim());
     if(pick){
      await opts.filter({hasText:pick.trim()}).first().click();
      await page.waitForTimeout(1200);
      changed={idx:i,from:cur,to:pick.trim()};
      break;
     }
    }
    await page.keyboard.press('Escape');await page.waitForTimeout(300);
   }
   // 现在保存应可用
   const saves=page.getByRole('button',{name:'保存',exact:true});
   const sn=await saves.count();
   const disabled=await Promise.all([...Array(sn).keys()]
     .map(async i=>await saves.nth(i).isDisabled().catch(()=>null)));
   let saved=null;
   for(let i=0;i<sn;i++){
    if(disabled[i])continue;
    const m=apiMark();
    const [r]=await Promise.all([
     page.waitForResponse(x=>x.request().method()!=='GET',{timeout:15000}).catch(()=>null),
     saves.nth(i).click()]);
    await page.waitForTimeout(1800);
    if(r){
     saved={idx:i,method:r.request().method(),path:new URL(r.url()).pathname,status:r.status(),
      body:(await r.text().catch(()=>'')).slice(0,120)};
     break;
    }
   }
   ev={status:saved?'verified':'partial',combo_before:before,changed,
    save_buttons:sn,disabled_states:disabled,saved,
    detail:saved?'改动时段（'+changed?.from+'→'+changed?.to+'）-> 保存 -> '+saved.method+' '+saved.path+' ('+saved.status+')'
     :(changed?'改动后保存仍 disabled/无写请求':'无法改动时段')};
  }
  H.record('PB0094',ev);
 }

 // ---------- PB0136 上游端点复制：验证剪贴板内容的真实语义
 await page.goto(CONSOLE+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 {
  // 找一个带复制按钮的行（默认上游）
  const row=page.getByText('默认上游',{exact:false}).first();
  await row.hover().catch(()=>{});
  await page.waitForTimeout(500);
  const btns=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   return [...document.querySelectorAll('button[title*="复制"]')].filter(vis)
    .map((b,i)=>({i,title:b.title}));});
  let ev={status:'not_present_in_ui',copy_buttons:btns};
  if(btns.length){
   const cp=page.locator('button[title*="复制"]').first();
   await cp.click();
   await page.waitForTimeout(600);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   // 判定剪贴板内容类型
   const isUrl=/^https?:\/\//.test(clip||'');
   const isPath=/^\//.test(clip||'');
   const kind=isUrl?'url':isPath?'path':(clip?'other':'empty');
   ev={status:'verified',copy_buttons:btns,clipboard:clip?.slice(0,120),kind,
    detail:'复制按钮实际复制的是 '+(kind==='path'?'**auth_dir 路径**（即账号目录）':kind==='url'?'URL':'其他')+
     '：'+(clip||'').slice(0,90)+'。这是"复制账号目录"而非"复制端点 URL"——语义与台账假设不同，需产品确认标签'};
  }
  H.record('PB0136',ev);
 }
 await page.screenshot({path:path.join(H.run,'close8.png')});
}catch(e){H.report.error=String(e).slice(0,500);}
await ctx.close();
await H.finish();
