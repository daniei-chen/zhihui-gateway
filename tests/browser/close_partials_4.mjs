// PB0062/PB0063 收口：模型 chip 是"图标+文本"按钮，文本在 span 里；
// 用「当前模型 fake-model」这段文本来锚定 chip（它就在页面文案里）。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-close4/v1','close4');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
try{
 await H.login(page);
 // 引导/提示遮罩会挡住输入区控件：先清掉（"知道了"是引导，"下一步"是新手步骤）
 await page.goto(base.origin+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(4000);
 for(let k=0;k<6;k++){
  const dismiss=page.getByRole('button',{name:/知道了|下一步|跳过|关闭/}).first();
  if(!(await dismiss.count()))break;
  await dismiss.click({force:true}).catch(()=>{});
  await page.waitForTimeout(700);
 }
 await page.waitForTimeout(2000);
 // 输入区是**可折叠**的：模型 chip 与思考强度只在 expanded 时渲染
 // （expanded = isActive || value || modelOpen || effortOpen，见 ai-chat-input.tsx:86）。
 // 所以先点输入框激活，让这两组控件出现。
 const ta=page.locator('textarea').first();
 await ta.click({force:true}).catch(()=>{});
 await page.waitForTimeout(1500);
 await ta.fill('qa-expand');            // value 非空也满足 expanded
 await page.waitForTimeout(1200);
 // dump 输入区的完整按钮结构（含内部 span 文本与 title）
 const struct=await page.evaluate(()=>{
  const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
  const all=[...document.querySelectorAll('button')].filter(vis);
  return all.map((b,i)=>({i,text:(b.textContent||'').trim().slice(0,30),
   title:b.title||'',aria:b.getAttribute('aria-label')||'',
   cls:String(b.className).slice(0,40),
   rect:(r=>({x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width)}))(b.getBoundingClientRect())}));
 });
 H.note('composer buttons: '+JSON.stringify(struct).slice(0,900));
 // 找到含 fake-model 或"选择模型"的按钮
 const chipIdx=struct.find(b=>/fake-model|选择模型/.test(b.text||b.title||b.aria));
 let ev={status:'partial',struct};
 if(chipIdx){
  const btn=page.locator('button').nth(0); // 用 rect 精确点击
  await page.mouse.click(chipIdx.rect.x+Math.round(chipIdx.rect.w/2), chipIdx.rect.y+10);
  await page.waitForTimeout(1800);
  // 现在应有模型列表（每个模型一个 button）
  const opts=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   return [...document.querySelectorAll('button')].filter(vis)
    .map(b=>(b.textContent||'').trim()).filter(t=>/fake-model/.test(t));});
  const large=page.locator('button').filter({hasText:'fake-model-large'}).first();
  let picked=false;
  if(await large.count()){
   await large.click({force:true});
   await page.waitForTimeout(1200);
   picked=/fake-model-large/.test(await page.evaluate(()=>document.body.innerText));
  }
  ev={status:picked?'verified':'partial',chip:chipIdx,options:opts,picked,
   detail:picked?'模型 chip -> fake-model-large 选中（坐标点击）':'模型列表出现但未选中'};
 }else{
  ev.detail='未找到含 fake-model/选择模型 的按钮';
 }
 H.record('PB0062',ev);
 // 思考强度：切换模型后再找
 await page.keyboard.press('Escape');
 await page.waitForTimeout(500);
 const pc=page.locator('button').filter({hasText:'思考强度'}).first();
 let ev63={status:'not_present_in_ui'};
 if(await pc.count()){
  await pc.click({force:true});
  await page.waitForTimeout(1500);
  const opts=await page.evaluate(()=>{
   const vis=el=>{const r=el.getBoundingClientRect();return r.width>0&&r.height>0;};
   return [...document.querySelectorAll('button,[role=option],[role=menuitem]')].filter(vis)
    .map(x=>(x.textContent||'').trim()).filter(t=>/^(高|中|低|关闭|默认|off|on)$/i.test(t));});
  let chosen=null;
  if(opts.length){await page.getByText(opts[opts.length-1],{exact:true}).first().click({force:true}).catch(()=>{});
   await page.waitForTimeout(800);chosen=opts[opts.length-1];}
  ev63={status:chosen?'verified':'partial',options:opts,chosen,detail:chosen?'思考强度 -> '+chosen:'无选项'};
 }else{
  // 列出所有按钮文本，确认是否存在
  const btns=await page.evaluate(()=>[...document.querySelectorAll('button')].map(b=>(b.textContent||'').trim()).filter(Boolean));
  ev63={status:'not_present_in_ui',all_buttons:btns.slice(0,20),
   detail:'当前页面无"思考强度"控件（模型 efforts 为空时不渲染该控件——fake 模型无推理档位）'};
 }
 H.record('PB0063',ev63);
 await page.screenshot({path:path.join(H.run,'close4.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
