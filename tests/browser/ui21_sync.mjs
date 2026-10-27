// UI21 补齐 PB0181：完整填连接（含必填）后触发同步，记录真实失败与状态机。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui21-sync/v1','ui21sy');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 await page.goto(CONSOLE+'/settings/backup/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 // 逐字段填写（依据上一轮 placeholder 顺序：host/port/dbname/user/password/sslmode/interval）
 const vals=['127.0.0.1','1','qa_none','qa','qa','prefer','5'];
 const inputs=page.locator('input:visible');
 const n=await inputs.count();
 for(let i=0;i<Math.min(n,vals.length);i++)await inputs.nth(i).fill(vals[i]).catch(()=>{});
 await page.waitForTimeout(600);
 // 找到启用开关并打开
 const sw=page.locator('[role=switch]').first();
 if(await sw.count()){
  const st=await sw.getAttribute('data-state');
  if(st!=='checked')await sw.click().catch(()=>{});
  await page.waitForTimeout(800);
 }
 // 保存配置（若有保存按钮）
 const save=page.getByRole('button',{name:/保存/}).first();
 if(await save.count()&&!await save.isDisabled().catch(()=>true)){
  await save.click().catch(()=>{});await page.waitForTimeout(1500);
 }
 // 点同步
 const syncBtns=await page.evaluate(()=>[...document.querySelectorAll('button')]
  .map(b=>({t:(b.textContent||'').trim(),disabled:b.disabled,title:b.title||''}))
  .filter(x=>/同步|迁移|推送|备份/.test(x.t+x.title)));
 H.note('sync candidates: '+JSON.stringify(syncBtns));
 let result=null,tried=[];
 for(const cand of syncBtns){
  if(cand.disabled)continue;
  const btn=page.getByRole('button',{name:cand.t,exact:true}).first();
  const [r]=await Promise.all([
   page.waitForResponse(x=>x.request().method()!=='GET'&&/pg-sync|sync/.test(new URL(x.url()).pathname),{timeout:25000}).catch(()=>null),
   btn.click().catch(()=>{})]);
  await page.waitForTimeout(3500);
  tried.push({button:cand.t,http:r?.status()??null,path:r?new URL(r.url()).pathname:null});
  if(r){result=r;break;}
 }
 const after=(await lines()).filter(l=>/同步|失败|错误|成功|迁移|恢复/.test(l)).slice(-8);
 const st=await H.api(ctx,'GET','/api/settings/pg-sync');
 H.record('PB0181',{status:result?'verified':'partial',tried,
  status_api:st.body?.status,config_enabled:st.body?.config?.enabled,
  ui_lines:after,
  detail:result?'同步触发 -> '+result.request().method()+' '+new URL(result.url()).pathname+' ('+result.status()
   +')；lab 无 PG = 真实失败路径'
   :'同步按钮不可用或未产生写请求（如实记录）'});
 await page.screenshot({path:path.join(H.run,'UI21-sync.png')});
 // 复位：关闭启用 + 清空
 const sw2=page.locator('[role=switch]').first();
 if(await sw2.count()&&await sw2.getAttribute('data-state')==='checked'){await sw2.click().catch(()=>{});await page.waitForTimeout(600);}
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
