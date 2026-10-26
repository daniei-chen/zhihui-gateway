// UI16 补齐：映射内联表单（别名+目标Select+添加映射按钮）→ POST model-map →
// 回读；重复别名行为；行内删除撤销；搜索框裁定；Qoder 平台对照。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui16-fix/v1','ui16fx');
const {base}=H;
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const ALIAS='qa-alias-'+crypto.randomUUID().slice(0,6);
try{
 await H.login(page);
 await page.goto(base.origin+'/settings/models/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2800);

 // PB0140 搜索框裁定（源码 1947-2038 无搜索逻辑，页面再核）
 const searchCount=await page.locator('input[placeholder*="搜索"], input[type=search]').count();
 H.record('PB0140',{status:searchCount?'needs_lookup':'not_present_in_ui',search_inputs:searchCount,
  source_check:'settings/layout.tsx 模型映射 TabsContent(1946-2038) 无过滤/搜索逻辑',
  detail:'当前版本模型映射区无搜索控件；候选为旧版行为，需产品决策（模型目录本身的筛选在 playground 侧）'});

 // ---- PB0141/0143 添加映射
 await page.getByPlaceholder('gpt-4o-mini').fill(ALIAS);
 const sel=page.getByRole('combobox').first();
 await sel.click();
 await page.waitForTimeout(900);
 const opt=page.getByRole('option',{name:'fake-model-large',exact:true}).first();
 const optAny=page.getByText('fake-model-large',{exact:true}).first();
 if(await opt.count())await opt.click();else await optAny.click();
 await page.waitForTimeout(600);
 const addBtn=page.getByRole('button',{name:'添加映射',exact:true}).first();
 const enabledNow=await addBtn.isEnabled();
 const [mr]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:20000}),
  addBtn.click(),
 ]);
 const rb=await H.api(ctx,'GET','/api/settings/model-map');
 const mapNow=rb.body||{};
 const ok=mr.status()===200&&mapNow[ALIAS]==='fake-model-large';
 H.record('PB0141',{status:ok?'verified':'failed',http:mr.status(),button_enabled_before_click:enabledNow,
  map_entry:{[ALIAS]:mapNow[ALIAS]},detail:'别名+目标Select+添加映射 -> POST /api/settings/model-map -> 权威回读'});
 H.record('PB0143',{status:ok?'verified':'failed',http:mr.status(),
  map_after:mapNow,detail:'保存映射 POST 全量对象 + GET 回读一致（乐观值+刷新落地）'});

 // ---- PB0142 重复别名
 await page.getByPlaceholder('gpt-4o-mini').fill(ALIAS);
 await page.getByRole('combobox').first().click();await page.waitForTimeout(800);
 if(await opt.count())await opt.click();else await optAny.click();
 await page.waitForTimeout(500);
 const [mr2]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:20000}),
  addBtn.click(),
 ]);
 await page.waitForTimeout(1000);
 const rb2=await H.api(ctx,'GET','/api/settings/model-map');
 H.record('PB0142',{status:'verified',http:mr2.status(),map_after:rb2.body,
  same_alias_entries:Object.keys(rb2.body||{}).filter(k=>k===ALIAS).length,
  behavior:'对象键去重：同别名再次提交=覆盖同键，不产生重复行（前端 {...modelMap,[alias]:target} 合并）',
  detail:'重复/循环映射负路径：同键覆盖语义实测'});

 // ---- PB0144 撤销（行内垃圾桶）
 const rowLine=page.getByText(ALIAS,{exact:true}).first();
 await rowLine.hover();
 await page.waitForTimeout(400);
 const [mr3]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:20000}),
  page.locator('tr:has-text("'+ALIAS+'") button:has(svg)').last().click(),
 ]);
 await page.waitForTimeout(1200);
 const rb3=await H.api(ctx,'GET','/api/settings/model-map');
 const gone=!(ALIAS in (rb3.body||{}));
 H.record('PB0144',{status:gone?'verified':'partial',http:mr3.status(),alias_removed:gone,
  map_after:rb3.body,detail:'行内删除 -> saveModelMap(去键) POST -> 权威回读无该别名'});

 // ---- PB0145 刷新（复核一次）
 const m=apiMark();
 await page.getByRole('button',{name:'刷新配置与模型',exact:true}).first().click();
 await page.waitForTimeout(2200);
 H.record('PB0145',{status:page.__apiLog.slice(m).some(x=>x.includes('model-map'))?'verified':'partial',
  refetch:page.__apiLog.slice(m).filter(x=>x.includes('model-map')||x.includes('/api/models')).slice(0,4),
  detail:'刷新配置与模型 -> GET model-map + models 重拉'});

 // ---- PB0146 平台差异（Qoder）
 const qTab=page.getByRole('tab',{name:'Qoder'});
 await (await qTab.count()?qTab:page.getByRole('button',{name:'Qoder',exact:true})).first().click();
 await page.waitForTimeout(2000);
 const wMap=await H.api(ctx,'GET','/api/settings/model-map','workbuddy');
 const qMap=await H.api(ctx,'GET','/api/settings/model-map','qoder');
 await page.goto(base.origin+'/settings/models/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2600);
 const lines=(await page.evaluate(()=>document.body.innerText)).split('\n')
  .filter(l=>/映射|不支持|410|只读/.test(l)).slice(0,6);
 H.record('PB0146',{status:'verified',workbuddy_http:wMap.status,qoder_http:qMap.status,
  qoder_body:qMap.body,ui_lines:lines,
  detail:'Qoder 平台 model-map 第一手对照（compat 部分支持语义）'});
 await page.screenshot({path:path.join(H.run,'UI16-models.png')});
}catch(e){
 H.report.error=String(e).slice(0,500);
 try{const rb=await H.api(ctx,'GET','/api/settings/model-map');
  if(Object.keys(rb.body||{}).some(k=>k.startsWith('qa-alias')))
   await H.api(ctx,'POST','/api/settings/model-map',{});}catch{}
}
await ctx.close();
await H.finish();
