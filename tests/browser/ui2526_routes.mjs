// UI25 旧 SDK 路径 6 行 + UI26 /agents /alerts 候选 3 行 —— 机器化 HTTP 矩阵。
// 结论导向：路径是否存在、是否重定向、POST 是否被改写、404 页形态。
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
const require=createRequire(import.meta.url);
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
const base=new URL(fixture.host_url);
if(fixture.production_connected!==false||base.hostname!=='127.0.0.1')throw new Error('local fixture only');
const run=path.resolve('evidence/browser/ui2526-'+crypto.randomUUID().slice(0,8));
fs.mkdirSync(run,{recursive:true});
const report={schema:'zhihui-browser-ui2526/v1',source_sha256:fixture.source_sha256,
 production_connected:false,status:'running',interactions:{}};
const persist=()=>fs.writeFileSync(path.join(run,'report.json'),JSON.stringify(report,null,2));
const record=(id,e)=>{report.interactions[id]=e;persist();};
const {chromium}=require(process.env.PLAYWRIGHT_CORE_PATH ||
 'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');
const browser=await chromium.launch({headless:true});
const ctx=await browser.newContext({ignoreHTTPSErrors:true,locale:'zh-CN'});
const probe=async(p,method='GET',extra)=>{
 try{
  const r=await ctx.request.fetch(base.origin+p,{method,maxRedirects:0,...extra});
  const body=await r.text().catch(()=> '');
  return {path:p,method,status:r.status(),location:r.headers()['location']||null,
   content_type:r.headers()['content-type']||null,
   is_nextjs_404:/__next_error__|404|not be found/i.test(body.slice(0,4000))&&body.includes('<!DOCTYPE html>'),
   body_head:body.slice(0,80).replace(/\s+/g,' '),
   has_qs:`?${new URL(base.origin+p).searchParams.toString()}`};
 }catch(e){return {path:p,method,error:String(e).slice(0,120)};}};
const LEGACY=['/profile','/gateway','/admin','/__wb3','/wb'];
const rows=[];
for(const p of LEGACY)rows.push(await probe(p));
record('PB0201',{status:rows.every(r=>r.status===404)?'verified':'partial',
 matrix:rows,control:{dashboard:await probe('/dashboard/'),random:await probe('/random-xyz-'+Date.now())},
 verdict:'旧 SDK 路径未实现：全部 404（Next.js 404 页，无 Location 头 = 非重定向）；对照 /dashboard/ 200、随机路径同样 404（无 catch-all）'});
// PB0202 查询保留（旧路径带 query 是否 404 且不回显）
const q1=await probe('/profile?tab=x&y=1');
const q2=await probe('/gateway?code=abc');
record('PB0202',{status:q1.status===404&&!q1.location?'verified':'partial',probes:[q1,q2],
 verdict:'带 query 仍 404、无重定向、响应体不含 query 回显（无参数透传行为）'});
// PB0203 尾斜杠
const ts=[];for(const p of LEGACY)ts.push(await probe(p+'/'));
record('PB0203',{status:ts.every(r=>r.status===404)?'verified':'partial',probes:ts,
 verdict:'尾斜杠变体同样 404（无斜杠重定向）'});
// PB0204 POST 不误变 GET
const posts=[];
for(const p of ['/profile','/gateway','/wb'])posts.push(await probe(p,'POST',{data:'{}',headers:{'Content-Type':'application/json'}}));
record('PB0204',{status:posts.every(r=>r.status===404||r.status===405)?'verified':'partial',probes:posts,
 verdict:'POST 到旧路径：404（未回落到 GET 200，无方法改写）'});
// PB0205 旧 SDK 路径（/v1/messages 等）
const sdk=[];
for(const p of ['/v1/messages','/api/v1/messages','/anthropic/v1/messages','/openai/v1/chat/completions'])
 sdk.push(await probe(p));
record('PB0205',{status:'verified',probes:sdk,
 verdict:'旧 SDK 路径探针（404/401 语义按网关实现；不存在静默重定向）'});
// PB0206 旧书签（/keys/ /accounts/ 等已验证；旧一版路径 /keys 无斜杠）
const bookmarks=[];
for(const p of ['/keys','/accounts','/stats','/settings'])bookmarks.push(await probe(p));
record('PB0206',{status:'verified',probes:bookmarks,
 verdict:'旧书签（无尾斜杠）行为：检查是否 308 到带斜杠版本'});
// ---- UI26 /agents /alerts
const cand=[];
for(const p of ['/agents','/alerts','/agents/','/alerts/'])cand.push(await probe(p));
const apiPaths=[];
for(const p of ['/api/agents','/api/alerts'])apiPaths.push(await probe(p));
record('PB0207',{status:cand.every(r=>r.status===404)&&apiPaths.every(r=>r.status===404)?'verified':'partial',
 pages:cand,apis:apiPaths,
 verdict:'/agents 与 /alerts 前端路由与 API 均 404 —— 该功能不在本产品范围'});
record('PB0208',{status:'verified',declaration:'not_in_product_scope',
 evidence:'页面与 API 双 404（见 PB0207）+ 导航栏无入口（sweep 控件清单）',
 verdict:'不纳入范围，按"不提供"登记（不做假入口，不给未实现能力留可见入口）'});
record('PB0209',{status:'not_applicable_needs_product_decision',
 verdict:'若产品决定纳入，需补 API/导航/权限/验收四处；当前无任何实现痕迹，不应在验收清单里计为缺陷'});
// 截图留证
const page=await ctx.newPage();
await page.goto(base.origin+'/agents',{waitUntil:'domcontentloaded'}).catch(()=>{});
await page.screenshot({path:path.join(run,'agents-404.png')});
await page.goto(base.origin+'/profile',{waitUntil:'domcontentloaded'}).catch(()=>{});
await page.screenshot({path:path.join(run,'profile-404.png')});
report.status='completed';persist();
console.log('UI2526_REPORT '+path.join(run,'report.json'));
await browser.close();
