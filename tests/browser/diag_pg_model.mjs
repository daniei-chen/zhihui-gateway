// 验证 playground 模型数据是否进入前端状态（决定 chip 文案）。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-pgmodel/v1','pgm');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const seen=[];
page.on('response',async r=>{
 const u=new URL(r.url());
 if(u.pathname.includes('playground/models'))seen.push({url:u.pathname+u.search,status:r.status(),
  body:(await r.text().catch(()=>'')).slice(0,200)});
});
try{
 await H.login(page);
 await page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(6000);
 const info=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const btns=[...document.querySelectorAll('button')].filter(vis).map(b=>(b.textContent||'').trim());
  // 输入区上方的控件区
  const composer=document.querySelector('textarea')?.closest('div')?.parentElement;
  return {buttons:btns.slice(0,25),
   composer_html:composer?composer.innerHTML.slice(0,600):null,
   body_has_selectmodel:/选择模型/.test(document.body.innerText),
   body_has_nomodels:/暂无可用模型|没有可用模型/.test(document.body.innerText),
   body_snippet:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(-12)};});
 H.record('PGMODEL',{model_api_calls:seen,ui:info,
  verdict:seen.length? (info.body_has_selectmodel?'chip 显示"选择模型"=前端未拿到模型':'chip 已渲染具体模型')
   :'未发出 models 请求'});
 await page.screenshot({path:path.join(H.run,'pg.png')});
}catch(e){H.report.error=String(e).slice(0,300);}
await ctx.close();
await H.finish();
