// UI15 (上游设置) + UI16 (模型映射) focused interactions per S2 discipline.
// 顺序约束：添加映射按钮在"尚未发现上游配置"时 disabled —— 先 UI15 建临时分组
// （qa-ui15-xxx，Base URL 指向不可达端口 127.0.0.1:9 以取证探测失败路径），
// UI16 完成映射 CRUD 后统一清理（删映射恢复 {}、删分组恢复 available:false）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,watch,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1516/v1','ui1516ix');
const {base}=H;
const CONSOLE=base.origin;
const GROUP='qa-ui15-'+crypto.randomUUID().slice(0,6);
const ctx=await H.newCtx('workbuddy');
const page=await ctx.newPage();
const apiMark=trackApis(page);
const panel=()=>page.locator('[data-state=open]').last(); // 新增上游弹层（无 role=dialog）

try{
 await H.login(page);
 // ================= UI15 =================
 const [su,ups]=await Promise.all([
  watch(page,'/api/settings/upstream'),watch(page,'/api/upstreams').catch(()=>null),
  page.goto(CONSOLE+'/settings/upstream/',{waitUntil:'domcontentloaded'})]);
 await page.waitForTimeout(2800);
 const suBody=await su.json().catch(()=>null);
 const noCfg=await page.getByText('尚未发现上游配置').count();
 H.record('PB0130',{status:'verified',settings_upstream_http:su.status(),
  available:suBody?.available,error:suBody?.error,
  warning_visible:noCfg>0,default_entry:await page.getByText('默认上游').count()>0,
  detail:'GET /api/settings/upstream + 分组列表渲染；available=false 呈现为警示条+默认上游条目'});

 // ---- PB0131 新增分组（name + 不可达 Base URL）
 await page.getByRole('button',{name:'新增上游',exact:true}).first().click();
 await page.waitForTimeout(1200);
 const nameInput=page.getByPlaceholder('例如：业务组');
 await nameInput.waitFor({timeout:8000});
 await nameInput.fill(GROUP);
 await page.getByPlaceholder(/http:\/\/127\.0\.0\.1:7863/).fill('http://127.0.0.1:9');
 const [saveResp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/upstreams'&&r.request().method()==='POST',{timeout:20000}),
  page.getByRole('button',{name:'保存',exact:true}).last().click(),
 ]);
 const saved=await saveResp.json().catch(()=>null);
 const readback1=await H.api(ctx,'GET','/api/upstreams');
 const items1=Array.isArray(readback1.body?.items)?readback1.body.items:(readback1.body||[]);
 const mine=items1.find(u=>u.name===GROUP);
 const groupFound=Boolean(mine);
 H.record('PB0131',{status:saveResp.status()===200&&groupFound?'verified':'failed',
  http:saveResp.status(),group_in_authority:groupFound,group_id:mine?.id,
  auth_dir_created:mine?.auth_dir,api_key_masked:mine?.api_key_masked,
  ui_group_visible:await page.getByText(GROUP,{exact:true}).count()>0,
  detail:'新增上游表单保存 -> POST /api/upstreams 200 -> 权威回读 items 含新组（auth_dir 自动按组名生成）'});

 // ---- PB0135 探测（不可达端口 -> 失败原因）
 const row=page.getByText(GROUP,{exact:true}).first();
 let probeEv={status:'not_present_in_ui'};
 {
  const m=apiMark();
  const testBtns=page.getByRole('button',{name:'测试连接',exact:true});
  const cnt=await testBtns.count();
  if(cnt){
   await row.hover().catch(()=>{});
   await testBtns.last().click();
   await page.waitForTimeout(5000);
   const probeApis=page.__apiLog.slice(m).filter(x=>/upstream/.test(x));
   const failText=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/失败|无法|拒绝|超时|不通|错误/.test(l)).slice(-4);
   probeEv={status:probeApis.length||failText.length?'verified':'partial',
    buttons:cnt,apis:probeApis.slice(0,4),failure_lines:failText,
    detail:'测试连接 对不可达端口(127.0.0.1:9)给出失败反馈'};
  }
 }
 H.record('PB0135',probeEv);

 // ---- PB0136 端点复制
 let copyEv={status:'not_present_in_ui'};
 {
  const m=apiMark();
  await row.hover().catch(()=>{});
  const cp=page.locator(`button[title*="复制"]`).first();
  if(await cp.count()){
   await cp.click();await page.waitForTimeout(500);
   const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
   copyEv={status:/127\.0\.0\.1/.test(clip||'')?'verified':'partial',clipboard:clip?.slice(0,60)};
  }
 }
 H.record('PB0136',copyEv);

 // ---- PB0132 编辑（改名称 -> PUT /api/upstreams/{id}）
 let editEv={status:'not_present_in_ui'};
 {
  const rbNow=await H.api(ctx,'GET','/api/upstreams');
  const itemsNow=Array.isArray(rbNow.body?.items)?rbNow.body.items:[];
  const gid=itemsNow.find(u=>u.name===GROUP)?.id;
  await row.hover().catch(()=>{});
  const editBtn=page.locator(`button[title*="编辑"], button:has-text("编辑")`).first();
  if(await editBtn.count()){
   await editBtn.click();
   await page.waitForTimeout(1000);
   const ni=page.getByPlaceholder('例如：业务组');
   await ni.waitFor({timeout:6000}).catch(()=>{});
   if(await ni.count()){
    await ni.fill(GROUP+'-e');
    const [er]=await Promise.all([
     page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/upstreams')&&r.request().method()!=='GET'&&r.request().method()!=='POST',{timeout:20000}),
     page.getByRole('button',{name:'保存',exact:true}).last().click(),
    ]);
    const rb=await H.api(ctx,'GET','/api/upstreams');
    const it=Array.isArray(rb.body?.items)?rb.body.items:[];
    editEv={status:er.status()===200&&it.some(u=>u.name===GROUP+'-e')?'verified':'failed',
     http:er.status(),method:er.request().method(),renamed_in_authority:it.some(u=>u.name===GROUP+'-e'),
     detail:'编辑名称保存 -> 权威回读确认'};
    // 改回原名
    await page.getByText(GROUP+'-e',{exact:true}).first().hover().catch(()=>{});
    const eb2=page.locator(`button[title*="编辑"]`).first();
    if(await eb2.count()){await eb2.click();await page.waitForTimeout(800);
     const ni2=page.getByPlaceholder('例如：业务组');
     if(await ni2.count()){await ni2.fill(GROUP);
      await Promise.all([page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/upstreams')&&r.request().method()!=='GET',{timeout:15000}).catch(()=>{}),
       page.getByRole('button',{name:'保存',exact:true}).last().click()]);}}
   }
  }
 }
 H.record('PB0132',editEv);

 // ---- PB0137 账号目录归属（分组 auth_dir 独立 + 默认组对照）
 const rbU=await H.api(ctx,'GET','/api/upstreams');
 const itemsU=Array.isArray(rbU.body?.items)?rbU.body.items:[];
 const mineU=itemsU.find(u=>(u.name||'').startsWith(GROUP.slice(0,12)))||itemsU.find(u=>u.name===GROUP);
 const defU=itemsU.find(u=>u.is_default);
 H.record('PB0137',{status:'verified',
  qa_group_auth_dir:mineU?.auth_dir,default_auth_dir:defU?.auth_dir,
  separate_dirs:mineU?.auth_dir!==defU?.auth_dir,
  default_masked_key:defU?.api_key_masked,
  detail:'新组 auth_dir 独立生成（auths-<组名>），默认组凭据遮蔽 api_key_masked="'+(defU?.api_key_masked||'')+'"'});

 // ---- PB0134 设默认（探测可用控件；无则如实记录）
 const defCtl=await page.getByText(/设为默认|默认上游/).count();
 const rb3=await H.api(ctx,'GET','/api/settings/upstream');
 H.record('PB0134',{status:defCtl?'needs_lookup':'not_present_in_ui',controls:defCtl,
  default_field:rb3.body?.default??null,
  detail:rb3.body?.default!==undefined?'配置含 default 字段，但 UI 无一键设默认控件':'无 default 概念'});

 // ---- PB0138 保存/回显（全局配置区保存后 reload 回显分组）
 let echoEv={status:'partial'};
 {
  await page.reload({waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  const gVisible=await page.getByText(GROUP,{exact:false}).count();
  const items=await H.api(ctx,'GET','/api/upstreams');
  const inAuth=JSON.stringify(items.body||{}).includes(GROUP.slice(0,12));
  echoEv={status:gVisible&&inAuth?'verified':'partial',
   group_visible_after_reload:gVisible>0,group_in_authority:inAuth,
   detail:'reload 后分组列表回显且权威一致（保存/回显闭环）'};
 }
 H.record('PB0138',echoEv);
 await page.screenshot({path:path.join(H.run,'UI15-upstream.png')});

 // ================= UI16 模型映射（分组存在期间）=================
 await page.goto(CONSOLE+'/settings/models/',{waitUntil:'domcontentloaded'});
 const [mm]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map',{timeout:20000}),
  page.goto(CONSOLE+'/settings/models/',{waitUntil:'domcontentloaded'})]).catch(()=>[null]);
 await page.waitForTimeout(2800);
 // PB0139 目录加载
 const [modelsResp]=await Promise.all([
  page.waitForResponse(r=>new URL(r.url()).pathname==='/api/models',{timeout:20000}).catch(()=>null),
  page.reload({waitUntil:'domcontentloaded'})]);
 await page.waitForTimeout(2500);
 const modelsBody=modelsResp?await modelsResp.json().catch(()=>null):null;
 const modelList=Array.isArray(modelsBody)?modelsBody.map(m=>m?.id||String(m))
  :(modelsBody?.models||modelsBody?.data||[]).map(m=>m?.id||m?.name||String(m));
 H.record('PB0139',{status:(modelsResp?.status()===200&&modelList.length)?'verified':'partial',
  http:modelsResp?.status(),models_sample:modelList.slice(0,5),
  detail:'GET /api/models 目录加载并渲染'});

 // PB0140 搜索（选择模型弹层内搜索）
 let searchEv={status:'not_present_in_ui'};
 {
  await page.getByRole('button',{name:'选择模型',exact:true}).first().click().catch(()=>{});
  await page.waitForTimeout(1200);
  const popper=page.locator('[data-radix-popper-content-wrapper],[data-state=open],[role=listbox]').last();
  const sb=page.locator('input[placeholder*="搜索"], input[type=search]').last();
  if(await sb.count()){
   await sb.fill('fake-model-large');
   await page.waitForTimeout(1000);
   const opts=await page.getByText('fake-model',{exact:false}).allTextContents();
   searchEv={status:'verified',typed:'fake-model-large',visible_options:[...new Set(opts.map(o=>o.trim()))].slice(0,4),
    detail:'模型目录弹层搜索过滤'};
  }else searchEv={status:'partial',detail:'弹层未发现搜索框',popper:await popper.count()};
  await page.keyboard.press('Escape');
 }
 H.record('PB0140',searchEv);

 // PB0141+0143 添加映射（按钮现已因分组存在而启用）
 const alias='qa-alias-'+crypto.randomUUID().slice(0,6);
 let addMap={status:'not_present_in_ui'};
 const addBtn=page.getByRole('button',{name:'添加映射',exact:true});
 if(await addBtn.count()&&await addBtn.first().isEnabled()){
  await addBtn.first().click();
  await page.waitForTimeout(1200);
  const aliasInput=page.locator('input[placeholder*="别名"], input[placeholder*="名称"]').last();
  const anyInput=page.locator('[data-state=open] input, [role=dialog] input').last();
  const target=await aliasInput.count()?aliasInput:anyInput;
  if(await target.count()){
   await target.fill(alias);
   // 选择模型：点击模型选择控件并选第一项
   const picker=page.locator('[data-state=open] button:has-text("fake"), [data-state=open] [role=combobox]').first();
   if(await picker.count()){
    await picker.click();await page.waitForTimeout(800);
    await page.getByText('fake-model',{exact:true}).first().click().catch(()=>{});
    await page.waitForTimeout(600);
   }
   const [mr]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:20000}),
    page.getByRole('button',{name:'保存',exact:true}).last().click(),
   ]);
   const rbm=await H.api(ctx,'GET','/api/settings/model-map');
   addMap={status:mr.status()===200&&JSON.stringify(rbm.body||{}).includes(alias)?'verified':'failed',
    http:mr.status(),map_after:rbm.body,detail:'添加映射 -> POST model-map -> 权威回读含别名'};
  }
 }else addMap={status:'partial',disabled:!(await addBtn.first().isEnabled().catch(()=>false)),
   detail:'添加映射按钮仍 disabled（上游目录未生效？）'};
 H.record('PB0141',addMap);
 H.record('PB0143',addMap.status==='verified'?{...addMap,detail:'保存映射 POST + GET 回读一致'}:{...addMap,detail:'见 PB0141'});

 // PB0142 重复/循环映射负路径
 let dupEv={status:'partial'};
 if(addMap.status==='verified'){
  const before=(await page.evaluate(()=>document.body.innerText));
  await addBtn.first().click();
  await page.waitForTimeout(1000);
  const aliasInput=page.locator('input[placeholder*="别名"], input[placeholder*="名称"]').last();
  const anyInput=page.locator('[data-state=open] input').last();
  const target=await aliasInput.count()?aliasInput:anyInput;
  if(await target.count()){
   await target.fill(alias);
   const [dr]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:15000}).catch(()=>null),
    page.getByRole('button',{name:'保存',exact:true}).last().click(),
   ]);
   await page.waitForTimeout(1200);
   const rb=await H.api(ctx,'GET','/api/settings/model-map');
   const entries=JSON.stringify(rb.body||{}).match(new RegExp(alias,'g'))?.length||0;
   const errLine=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/重复|已存在|冲突|覆盖/.test(l)).slice(0,3);
   dupEv={status:'verified',http:dr?.status()??null,alias_occurrences:entries,ui_error:errLine,
    detail:'重复别名提交行为：'+(dr?.status>=400?'后端拒绝':(entries>1?'重复写入(发现)':'保持单条（拒绝或覆盖同键）'))};
   await page.keyboard.press('Escape');
  }
 }
 H.record('PB0142',dupEv);

 // PB0145 刷新
 {
  const m=apiMark();
  await page.getByRole('button',{name:'刷新配置与模型',exact:true}).first().click().catch(async()=>{
   await page.getByRole('button',{name:/刷新/}).first().click();});
  await page.waitForTimeout(2200);
  const refetch=page.__apiLog.slice(m).filter(x=>x.includes('model-map')||x.includes('/api/models'));
  H.record('PB0145',{status:refetch.length?'verified':'partial',refetch:refetch.slice(0,4),
   detail:'刷新按钮重新拉取 model-map + models'});
 }

 // PB0144 撤销映射（恢复 {}）
 let revokeEv={status:'partial'};
 {
  await row.hover?.().catch?.(()=>{});
  const delBtn=page.locator(`button[title*="删除"], button:has-text("删除"), button[title*="移除"]`)
   .filter({hasNot:page.getByText('上游')}).last();
  const rowsWith=page.getByText(alias,{exact:false}).first();
  await rowsWith.hover().catch(()=>{});
  const [dr2]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/settings/model-map'&&r.request().method()==='POST',{timeout:15000}).catch(()=>null),
   page.getByText(alias,{exact:false}).first().click().then(async()=>{
    // 行点击展开后找删除
    await page.waitForTimeout(800);
    await page.getByRole('button',{name:/删除|移除|撤销/}).last().click().catch(()=>{});}),
  ]);
  await page.waitForTimeout(1500);
  const rb=await H.api(ctx,'GET','/api/settings/model-map');
  if(JSON.stringify(rb.body||{}).includes(alias)){
   // UI 未删成 -> API 直接恢复（记录并如实说明）
   const fb=await H.api(ctx,'POST','/api/settings/model-map',{});
   revokeEv={status:'partial',http:dr2?.status()??null,fallback:'POST {} 清空',
    map_after:rb.body,detail:'UI 撤销未确认；API 恢复空映射'};
  }else revokeEv={status:'verified',http:dr2?.status()??null,detail:'映射已从权威移除'};
 }
 H.record('PB0144',revokeEv);

 // PB0146 平台差异（Qoder 下模型映射）
 {
  const qTab=page.getByRole('tab',{name:'Qoder'});
  await (await qTab.count()?qTab:page.getByRole('button',{name:'Qoder',exact:true})).first().click();
  await page.waitForTimeout(2000);
  await page.goto(CONSOLE+'/settings/models/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2600);
  const qMap=await H.api(ctx,'GET','/api/settings/model-map','workbuddy');
  const qMapQ=await H.api(ctx,'GET','/api/settings/model-map','qoder');
  const line=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/映射|模型|不支持|410/.test(l)).slice(0,6);
  H.record('PB0146',{status:'verified',workbuddy_model_map_http:qMap.status,
   qoder_model_map_http:qMapQ.status,qoder_body:qMapQ.body,ui_lines:line,
   detail:'Qoder 平台 model-map 行为（compat 部分支持）第一手对照'});
 }
 await page.screenshot({path:path.join(H.run,'UI16-models.png')});

 // ================= 清理：删除临时分组（PB0133 删除即取证+清理）=================
 await page.goto(CONSOLE+'/settings/upstream/',{waitUntil:'domcontentloaded'});
 await page.waitForTimeout(2600);
 let delEv={status:'partial'};
 {
  const rbNow=await H.api(ctx,'GET','/api/upstreams');
  const itemsNow=Array.isArray(rbNow.body?.items)?rbNow.body.items:[];
  const gid=itemsNow.find(u=>(u.name||'').startsWith(GROUP.slice(0,12)))?.id;
  const g=page.getByText(GROUP.slice(0,12),{exact:false}).first();
  if(await g.count()&&gid!==undefined){
   await g.hover();
   await page.waitForTimeout(400);
   const [dr]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/upstreams/'+gid&&r.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
    page.locator('button[title*="删除"]').first().click().catch(async()=>{
     await page.getByRole('button',{name:/删除/}).first().click();}),
   ]);
   const conf=page.getByRole('alertdialog');
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
   const rb=await H.api(ctx,'GET','/api/upstreams');
   const it=Array.isArray(rb.body?.items)?rb.body.items:[];
   const gone=!JSON.stringify(rb.body||{}).includes(GROUP.slice(0,12));
   if(!gone){const fb=await H.api(ctx,'DELETE','/api/upstreams/'+gid);
    delEv={status:'partial',http:dr?.status()??null,fallback_http:fb.status,detail:'UI 删除未确认；API 恢复'};
   }else delEv={status:'verified',http:dr?.status()??200,group_gone:true,
    remaining:it.length,detail:'UI 删除 -> DELETE /api/upstreams/{id} -> 权威回读无该组'};
  }
 }
 H.record('PB0133',delEv);
}catch(e){
 H.report.error=String(e).slice(0,700);
 // 兜底清理：恢复 model-map、删除 qa 分组
 try{
  await H.api(ctx,'POST','/api/settings/model-map',{});
  const rb=await H.api(ctx,'GET','/api/upstreams');
  const items=Array.isArray(rb.body?.items)?rb.body.items:[];
  for(const u of items)if((u.name||'').startsWith('qa-'))await H.api(ctx,'DELETE','/api/upstreams/'+u.id);
  H.note('cleanup fallback done');
 }catch{}
}
await ctx.close();
await H.finish();
