// N22 修复验证：changelog 失败时出现"重试"按钮，点击后重新取数。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-n22/v1','n22ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
try{
 await H.login(page);
 // 1) 正常态：changelog 有内容
 await page.goto(CONSOLE+'/settings/changelog/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 const okTxt=await lines();
 const okHasRetry=await page.getByRole('button',{name:'重试',exact:true}).count();
 // 2) 注入失败态：让 changelog API 502
 await page.route('**/api/system/changelog*',r=>r.fulfill({status:502,contentType:'application/json',
  body:JSON.stringify({detail:'QA 注入失败'})}));
 await page.reload({waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 const failTxt=await lines();
 const retryBtn=page.getByRole('button',{name:'重试',exact:true});
 const retryCount=await retryBtn.count();
 // 3) 点击重试（仍失败）→ 仍显示失败态且按钮还在；再解除拦截点重试 → 恢复
 const m0=page.evaluate(()=>1);
 let refetch=0;
 page.on('request',r=>{if(new URL(r.url()).pathname.includes('changelog'))refetch++;});
 await retryBtn.click().catch(()=>{});
 await page.waitForTimeout(2600);
 const afterRetryFailTxt=await lines();
 const stillRetry=await page.getByRole('button',{name:'重试',exact:true}).count();
 await page.unroute('**/api/system/changelog*');
 await page.getByRole('button',{name:'重试',exact:true}).click().catch(()=>{});
 await page.waitForTimeout(3000);
 const recoveredTxt=await lines();
 const retryGone=await page.getByRole('button',{name:'重试',exact:true}).count();
 H.record('N22',{status:retryCount>0&&retryGone===0&&recoveredTxt.length?'verified':'failed',
  normal_has_retry:okHasRetry,
  failure_state:{retry_visible:retryCount>0,warning_lines:failTxt.filter(l=>/更新日志|失败|不可用/.test(l)).slice(0,4)},
  after_retry_still_failing:{retry_still_visible:stillRetry>0,
   lines:afterRetryFailTxt.filter(l=>/更新日志|失败|不可用/.test(l)).slice(0,3)},
  refetch_requests:refetch,
  recovered:{retry_button_gone:retryGone===0,
   content_lines:recoveredTxt.filter(l=>/版本|新增|修复|v\d/.test(l)).slice(0,5)},
  detail:'失败态出现"重试"按钮 -> 点击触发重新取数（仍失败则保持失败态）-> 解除故障后再点恢复内容；不再需要整页刷新'});
 await page.screenshot({path:path.join(H.run,'n22-retry.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
