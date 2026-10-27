// N25 复核：不用 route.abort（会被识别为用户取消），改用真实 5xx / 错误 JSON 响应，
// 验证 playground 是否给用户错误文案。同时覆盖 chat-stream 的 HTTP 错误分支。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-n25/v1','n25ix');
const {base}=H;
const CONSOLE=base.origin;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const lines=()=>page.evaluate(()=>document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean));
const ASST='div.flex.gap-2\\.5.justify-start';
try{
 await H.login(page);
 await page.goto(CONSOLE+'/playground/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3500);
 const ta=page.locator('textarea').first();
 const cases=[];
 // 场景1：502 + JSON detail
 await page.route('**/api/playground/chat',route=>route.fulfill({
  status:502,contentType:'application/json',
  body:JSON.stringify({detail:'上游不可用（QA 注入）'})}));
 {
  const before=(await lines()).join('\n');
  await ta.fill('qa-http-502');await ta.press('Enter');
  await page.waitForTimeout(3000);
  const after=await lines();
  cases.push({case:'502_json_detail',
   new_lines:after.filter(l=>!before.includes(l)&&l!=='qa-http-502'),
   assistant_text:await page.locator(ASST).last().innerText().catch(()=> '')});
 }
 await page.unroute('**/api/playground/chat');
 await page.reload({waitUntil:'domcontentloaded'});await page.waitForTimeout(3000);
 // 场景2：401 未授权
 await page.route('**/api/playground/chat',route=>route.fulfill({
  status:401,contentType:'application/json',body:JSON.stringify({detail:'登录已过期'})}));
 {
  const before=(await lines()).join('\n');
  await page.locator('textarea').first().fill('qa-http-401');
  await page.locator('textarea').first().press('Enter');
  await page.waitForTimeout(2800);
  const after=await lines();
  cases.push({case:'401_json_detail',
   new_lines:after.filter(l=>!before.includes(l)&&l!=='qa-http-401'),
   assistant_text:await page.locator(ASST).last().innerText().catch(()=> '')});
 }
 await page.unroute('**/api/playground/chat');
 // 场景3：SSE 内错误帧
 await page.reload({waitUntil:'domcontentloaded'});await page.waitForTimeout(3000);
 await page.route('**/api/playground/chat',route=>route.fulfill({
  status:200,contentType:'text/event-stream',
  body:'data: {"error":{"message":"上游炸了","code":500}}\n\ndata: [DONE]\n\n'}));
 {
  const before=(await lines()).join('\n');
  await page.locator('textarea').first().fill('qa-sse-error');
  await page.locator('textarea').first().press('Enter');
  await page.waitForTimeout(2800);
  const after=await lines();
  cases.push({case:'sse_error_frame',
   new_lines:after.filter(l=>!before.includes(l)&&l!=='qa-sse-error'),
   assistant_text:await page.locator(ASST).last().innerText().catch(()=> '')});
 }
 const hasMsg=cases.some(c=>c.assistant_text&&c.assistant_text.trim().length>0);
 H.record('PB0072',{status:hasMsg?'verified':'partial',cases,
  verdict:hasMsg
   ?'playground 对真实 HTTP/SSE 错误给出可见文案（N25 限定为"传输中断被误判为用户取消"这一窄场景）'
   :'确认无错误文案（N25 成立）'});
 H.record('PB0068',{status:(await page.getByRole('button',{name:/重试/}).count())?'verified':'not_present_in_ui',
  retry_controls:await page.getByRole('button',{name:/重试/}).count(),
  verdict:'错误后无重试控件（与 N22 同族；产品决策）'});
 await page.screenshot({path:path.join(H.run,'n25.png')});
}catch(e){H.report.error=String(e).slice(0,400);}
await ctx.close();
await H.finish();
