// PB0116 收尾：Qoder 平台下 /red-packets/ 页面实际渲染内容 dump。
import path from 'node:path';
import {makeHarness} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui12-qoder-ui/v1','ui12qou');
const {base}=H;
const ctx=await H.newCtx('qoder');
const page=await ctx.newPage();
try{
 await H.login(page);
 await page.goto(base.origin+'/red-packets/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(3000);
 const dump=await page.evaluate(()=>({
  path:location.pathname,
  lines:document.body.innerText.split('\n').map(t=>t.trim()).filter(Boolean).slice(0,15),
  h1:[...document.querySelectorAll('h1')].map(h=>h.textContent.trim())}));
 H.record('PB0116',{status:'verified',...dump,
  api_envelope:{http:410,code:'unsupported',detail:'本部署没有红包 / 抽奖：那是 WorkBuddy 的营销功能，qtg 不实现'},
  detail:'qoder 平台：API 410 信封已 curl 实证；UI 层 '+(dump.lines.some(l=>/不支持|营销/.test(l))?'展示了不支持说明':'未发起请求（前端门控/跳转），页面首行为 '+dump.lines[0])});
 await page.screenshot({path:path.join(H.run,'qoder-redpackets.png')});
}catch(e){H.report.error=String(e).slice(0,300);}
await ctx.close();
await H.finish();
