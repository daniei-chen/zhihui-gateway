// 诊断：新增上游表单保存时到底发生什么（请求/错误/按钮状态）。
import crypto from 'node:crypto';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-diag-upsave/v1','diagup');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apis=[];
page.on('request',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push('>'+r.method()+' '+u.pathname);});
page.on('response',r=>{const u=new URL(r.url());if(u.pathname.startsWith('/api/'))apis.push('<'+r.status()+' '+u.pathname);});
try{
 await H.login(page);
 await page.goto(base.origin+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2800);
 await page.getByRole('button',{name:'新增上游',exact:true}).first().click();
 await page.waitForTimeout(1200);
 const nameInput=page.getByPlaceholder('例如：业务组');
 await nameInput.waitFor({timeout:8000});
 await nameInput.fill('qa-diag-'+crypto.randomUUID().slice(0,4));
 await page.getByPlaceholder(/7863/).fill('http://127.0.0.1:9');
 // dump 表单当前可见必填态与按钮
 const before=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const openEl=document.querySelector('[data-state=open][role], .fixed.inset-0, [class*=DialogContent]');
  return {openRole:openEl?.getAttribute('role')||null,
   saveButtons:[...document.querySelectorAll('button')].filter(b=>/保存/.test(b.textContent||'')&&vis(b)).length,
   saveDisabled:[...document.querySelectorAll('button')].filter(b=>/保存/.test(b.textContent||'')&&vis(b)).map(b=>b.disabled)};});
 apis.length=0;
 await page.getByRole('button',{name:'保存',exact:true}).last().click();
 await page.waitForTimeout(4000);
 const bodyAfter=(await page.evaluate(()=>document.body.innerText)).split('\n').map(t=>t.trim());
 H.record('upstream_save',{form:before,apis,form_open_still:await page.getByPlaceholder('例如：业务组').count(),
  new_lines:bodyAfter.filter(l=>/失败|错误|必填|请输入|成功|已添加|分组/.test(l)).slice(0,8)});
 await page.screenshot({path:H.run+'/after-save.png'});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
console.log(JSON.stringify(H.report.interactions,null,1));
await H.finish();
