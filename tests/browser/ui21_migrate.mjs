// UI21 PB0181 终局：开始迁移 -> 确认 -> 记录真实失败（无 PG 目标）+ 状态机。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui21-migrate/v1','ui21mg');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 await page.goto(CONSOLE+'/settings/backup/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3200);
 const vals=['127.0.0.1','1','qa_none','qa','qa','prefer','5'];
 const inputs=page.locator('input:visible');
 const n=await inputs.count();
 for(let i=0;i<Math.min(n,vals.length);i++)await inputs.nth(i).fill(vals[i]).catch(()=>{});
 const sw=page.locator('[role=switch]').first();
 if(await sw.count()&&await sw.getAttribute('data-state')!=='checked')await sw.click().catch(()=>{});
 await page.waitForTimeout(700);
 const save=page.getByRole('button',{name:/保存/}).first();
 if(await save.count()&&!await save.isDisabled().catch(()=>true)){await save.click().catch(()=>{});await page.waitForTimeout(1500);}

 // 点开始迁移 -> 确认
 const migrate=page.getByRole('button',{name:'开始迁移',exact:true}).first();
 const [r]=await Promise.all([
  page.waitForResponse(x=>x.request().method()!=='GET'&&/pg-sync/.test(new URL(x.url()).pathname),{timeout:30000}).catch(()=>null),
  migrate.click()]);
 await page.waitForTimeout(1500);
 const alert=page.getByRole('alertdialog');
 const dlgText=await alert.count()?(await alert.innerText()).slice(0,200):'';
 let confirmResp=null;
 if(await alert.count()){
  const [c]=await Promise.all([
   page.waitForResponse(x=>x.request().method()!=='GET'&&/pg-sync/.test(new URL(x.url()).pathname),{timeout:30000}).catch(()=>null),
   alert.getByRole('button',{name:/确认|开始|迁移/}).last().click().catch(()=>{})]);
  confirmResp=c;
 } else confirmResp=r;
 await page.waitForTimeout(6000);
 const st=await H.api(ctx,'GET','/api/settings/pg-sync');
 const after=(await lines()).filter(l=>/失败|错误|连接|迁移|同步|进度/.test(l)).slice(-10);
 H.record('PB0181',{status:confirmResp?'verified':'partial',
  first_http:r?.status()??null,confirm_http:confirmResp?.status()??null,
  confirm_path:confirmResp?new URL(confirmResp.url()).pathname:null,
  dialog:dlgText.replace(/\n+/g,'|').slice(0,140),
  status_after:st.body?.status,config_enabled:st.body?.config?.enabled,
  ui_lines:after,
  detail:confirmResp?'开始迁移->确认 -> '+new URL(confirmResp.url()).pathname+' ('
   +confirmResp.status()+')；lab 无 PG 目标=真实失败路径；状态机 '
   +(st.body?.status?.ok===false?'ok=false':JSON.stringify(st.body?.status?.step||''))
   :'确认框未触发写请求'});
 await page.screenshot({path:path.join(H.run,'UI21-migrate.png')});
 // 复位
 const sw2=page.locator('[role=switch]').first();
 if(await sw2.count()&&await sw2.getAttribute('data-state')==='checked'){await sw2.click().catch(()=>{});await page.waitForTimeout(500);}
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
