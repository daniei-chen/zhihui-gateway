// 诊断：代理添加线路保存时的请求与表单校验。
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-proxysave/v1','diagpxs');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apis=[];
page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push('>'+r.method()+' '+u.pathname);});
page.on('response',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push('<'+r.status()+' '+u.pathname);});
try{
 await H.login(page);
 await page.goto(base.origin+'/settings/proxy/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2600);
 await page.getByRole('button',{name:'添加线路',exact:true}).first().click();
 await page.waitForTimeout(1500);
 await page.getByPlaceholder(/px-cn/).fill('qa-diag-'+crypto.randomUUID().slice(0,4));
 await page.getByPlaceholder(/user:pass@host:7890/).fill('http://qauser:secret@127.0.0.1:9');
 // dump 表单底部按钮与保存可用性
 const before=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  return {saves:[...document.querySelectorAll('button')].filter(b=>/保存/.test(b.textContent||'')&&vis(b))
    .map((b,i)=>({i,disabled:b.disabled})),
   openPanels:document.querySelectorAll('[data-state=open]').length};});
 apis.length=0;
 // 尝试每个保存按钮（从最后往前）
 let clicked=null;
 for(let i=before.saves.length-1;i>=0;i--){
  const btn=page.getByRole('button',{name:'保存',exact:true}).nth(i);
  if(await btn.isDisabled().catch(()=>true))continue;
  await btn.click({timeout:3000}).then(()=>{clicked=i;}).catch(()=>{});
  if(clicked!==null)break;
 }
 await page.waitForTimeout(3500);
 const errs=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/失败|错误|无效|必填|格式|请/.test(l)).slice(-6);
 H.record('proxy_save',{buttons:before,clicked_index:clicked,apis,
  form_still_open:await page.getByPlaceholder(/px-cn/).count(),errors:errs});
}catch(e){H.report.error=String(e).slice(0,300);}
await ctx.close();
console.log(JSON.stringify(H.report.interactions,null,1).slice(0,1500));
await H.finish();
