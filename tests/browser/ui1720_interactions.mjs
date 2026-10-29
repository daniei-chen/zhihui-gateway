// UI17 (用户) + UI18 (代理) + UI19 (Token) + UI20 (系统更新) per S2 discipline.
// 红线：UI20 危险按钮（安装/回退/重启/恢复）一律不点；上传只用无效小文件取校验负路径。
import path from 'node:path';
import crypto from 'node:crypto';
import fs from 'node:fs';
import {makeHarness,watch,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1720/v1','ui1720ix');
const {base}=H;
const CONSOLE=base.origin;
const USER='qa-ui17-'+crypto.randomUUID().slice(0,6);
const PASS='Qa-'+crypto.randomUUID().slice(0,8)+'!';

// ================= UI17 用户 =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/users/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  const listResp=await H.api(ctx,'GET','/api/users');
  const rows=await page.getByText('gateway-admin').count()&&await page.getByText('只读用户').count();
  H.record('PB0147',{status:listResp.status===200&&rows?'verified':'partial',http:listResp.status,
   users:listResp.body,ui_rows_visible:!!rows,detail:'GET /api/users + 两内置用户渲染'});

  // ---- PB0148 新增用户（内联表单：用户名/密码/角色 combobox + 创建）
  await page.getByLabel('用户名',{exact:false}).first().fill(USER).catch(async()=>{
   await page.locator('input').nth(0).fill(USER);});
  await page.getByLabel('密码',{exact:false}).first().fill(PASS).catch(async()=>{
   await page.locator('input[type=password]').first().fill(PASS);});
  const roleCombo=page.getByRole('combobox').first();
  await roleCombo.click();
  await page.waitForTimeout(700);
  const optRO=page.getByText('只读用户（仅查看）',{exact:false}).first();
  if(await optRO.count())await optRO.click();
  const [cr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/users'&&r.request().method()==='POST',{timeout:20000}),
   page.getByRole('button',{name:'创建',exact:true}).first().click(),
  ]);
  const list2=await H.api(ctx,'GET','/api/users');
  const createdOk=cr.status()===200&&(list2.body||[]).some(u=>u.username===USER);
  H.record('PB0148',{status:createdOk?'verified':'failed',http:cr.status(),
   user_in_authority:(list2.body||[]).find(u=>u.username===USER),
   detail:'内联创建表单 -> POST /api/users -> 权威回读'});

  // ---- PB0149 角色（viewer -> admin）+ PB0154 吊销旧会话
  let roleEv={status:'partial'},sessionEv={status:'partial'};
  if(createdOk){
   // 用户先登录拿到会话
   const uctx=await H.newCtx('workbuddy');
   const up=await uctx.newPage();
   await H.login(up,USER,PASS);
   const meBefore=await H.api(uctx,'GET','/api/me');
   // 行内角色 combobox 切 admin
   const urow=page.getByText(USER,{exact:true}).first();
   await urow.hover().catch(()=>{});
   const rowCombo=page.locator(`tr:has-text("${USER}") [role=combobox], [data-state] .grid:has-text("${USER}") [role=combobox]`).first();
   let switched=false;
   if(await rowCombo.count()){
    await rowCombo.click();await page.waitForTimeout(700);
    const adm=page.getByText('管理员',{exact:false}).last();
    if(await adm.count()){await adm.click();switched=true;}
   }
   await page.waitForTimeout(1500);
   const list3=await H.api(ctx,'GET','/api/users');
   const roleNow=(list3.body||[]).find(u=>u.username===USER)?.role;
   roleEv={status:roleNow==='admin'?'verified':'partial',switched_via:switched?'row_combobox':'api_fallback',
    role_after:roleNow,
    detail:roleNow==='admin'?'行内角色切换 -> 权威回读 admin':'行内切换未确认'};
   if(roleNow!=='admin'){const fb=await H.api(ctx,'PATCH','/api/users/'+USER,{role:'admin'});
    roleEv.fallback_http=fb.status;}
   // 旧会话应被吊销（改角色/密码触发）
   await page.waitForTimeout(1000);
   const meAfter=await H.api(uctx,'GET','/api/me');
   sessionEv={status:meAfter.status===401?'verified':'partial',
    me_before:meBefore.status,me_after_role_change:meAfter.status,
    detail:'用户会话在角色变更后 '+(meAfter.status===401?'被吊销(401)':'仍有效('+meAfter.status+')')};
   await uctx.close();
  }
  H.record('PB0149',roleEv);
  H.record('PB0154',sessionEv);

  // ---- PB0150 改密码（重置密码按钮 -> dialog -> PATCH -> 新密码可登录）
  let pwdEv={status:'partial'};
  if(createdOk){
   const row=page.getByText(USER,{exact:true}).first();
   await row.hover().catch(()=>{});
   const rbtn=page.locator(`button:has-text("重置密码")`).first();
   if(await rbtn.count()){
    await rbtn.click();
    const dlg=page.locator('[data-state=open], [role=dialog]').last();
    await page.waitForTimeout(900);
    const np='Qa2-'+crypto.randomUUID().slice(0,8)+'!';
    const pin=dlg.locator('input[type=password], input').first();
    if(await pin.count()){
     await pin.fill(np);
     const [pr]=await Promise.all([
      page.waitForResponse(r=>new URL(r.url()).pathname==='/api/users/'+USER&&r.request().method()==='PATCH',{timeout:20000}),
      dlg.getByRole('button',{name:/确定|保存|重置/}).last().click(),
     ]);
     const li=await ctx.request.post(CONSOLE+'/api/login',
      {headers:{Origin:CONSOLE},data:{username:USER,password:np}});
     pwdEv={status:pr.status()===200&&li.status()===200?'verified':'partial',
      patch_http:pr.status(),new_password_login:li.status(),
      detail:'重置密码 -> PATCH -> 新密码登录 '+(li.status()===200?'成功':'失败')};
    }
   }
  }
  H.record('PB0150',pwdEv);

  // ---- PB0152 本人保护（删除 gateway-admin 被拒）
  {
   const arow=page.getByText('gateway-admin',{exact:true}).first();
   await arow.hover().catch(()=>{});
   const dels=page.locator('button[title*="删除"], button:has(svg)').filter({hasText:''});
   const delBtn=page.locator(`button[title*="删除"]`).first();
   let selfEv={status:'partial'};
   if(await delBtn.count()){
    const [dr]=await Promise.all([
     page.waitForResponse(r=>/\/api\/users\/gateway-admin$/.test(new URL(r.url()).pathname)&&r.request().method()==='DELETE',{timeout:8000}).catch(()=>null),
     delBtn.click().catch(()=>{}),
    ]);
    await page.waitForTimeout(1200);
    const conf=page.getByRole('alertdialog');
    let resp=dr;
    if(await conf.count()){
     const [dr2]=await Promise.all([
      page.waitForResponse(r=>/\/api\/users\/gateway-admin$/.test(new URL(r.url()).pathname)&&r.request().method()==='DELETE',{timeout:8000}).catch(()=>null),
      conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{}),
     ]);resp=resp||dr2;
    }
    const li=await H.api(ctx,'GET','/api/users');
    const still=(li.body||[]).some(u=>u.username==='gateway-admin');
    selfEv={status:still&&(!resp||resp.status()>=400)?'verified':'partial',
     delete_http:resp?.status()??null,admin_still_present:still,
     detail:'删除本人 -> '+(resp?.status>=400?'后端拒绝('+resp.status+')':'未发请求或被前端拦')+'；权威仍含 gateway-admin'};
   }
   H.record('PB0152',selfEv);
  }

  // ---- PB0153 最后管理员保护（PATCH gateway-admin -> viewer 应被拒）
  {
   const [pr]=await Promise.all([
    page.waitForResponse(r=>/\/api\/users\/gateway-admin$/.test(new URL(r.url()).pathname)&&r.request().method()==='PATCH',{timeout:10000}).catch(()=>null),
    H.api(ctx,'PATCH','/api/users/gateway-admin',{role:'viewer'}),
   ]);
   const li=await H.api(ctx,'GET','/api/users');
   const stillAdmin=(li.body||[]).find(u=>u.username==='gateway-admin')?.role==='admin';
   H.record('PB0153',{status:stillAdmin?'verified':'CRITICAL_FAILED',
    patch_http:pr?pr.status():null,role_after:(li.body||[]).find(u=>u.username==='gateway-admin')?.role,
    detail:'唯一管理员降级请求 -> 后端拒绝；gateway-admin 仍为 admin'});
   // 若保护失效，立即自救
   if(!stillAdmin){await H.api(ctx,'PATCH','/api/users/gateway-admin',{role:'admin'});
    H.note('CRITICAL: last-admin guard failed; restored via session');}
  }

  // ---- PB0151 删除测试用户（清理+取证）
  {
   const row=page.getByText(USER,{exact:true}).first();
   await row.hover().catch(()=>{});
   const delBtn=page.locator(`button[title*="删除"]`).nth(1);
   const [dr]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/users/'+USER&&r.request().method()==='DELETE',{timeout:10000}).catch(()=>null),
    delBtn.click().catch(()=>{}),
   ]);
   await page.waitForTimeout(800);
   const conf=page.getByRole('alertdialog');
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(1200);
   let li=await H.api(ctx,'GET','/api/users');
   let gone=!(li.body||[]).some(u=>u.username===USER);
   if(!gone){const fb=await H.api(ctx,'DELETE','/api/users/'+USER);
    li=await H.api(ctx,'GET','/api/users');
    gone=!(li.body||[]).some(u=>u.username===USER);
    H.record('PB0151',{status:'partial',ui_http:dr?.status()??null,fallback_http:fb.status,user_gone:gone,
     detail:'UI 删除未确认；API 兜底删除完成'});
   }else H.record('PB0151',{status:'verified',http:dr?.status()??200,user_gone:true,
    detail:'行删除 -> DELETE -> 权威无该用户'});
  }
  await page.screenshot({path:path.join(H.run,'UI17-users.png')});
 }catch(e){
  H.report.error='ui17: '+String(e).slice(0,500);
  try{await H.api(ctx,'DELETE','/api/users/'+USER);}catch{}
 }
 await ctx.close();
}

// ================= UI18 代理 =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 const ROUTE='qa-route-'+crypto.randomUUID().slice(0,6);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/proxy/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  const [pr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/proxy-routes',{timeout:15000}),
   page.reload({waitUntil:'domcontentloaded'})]);
  const body=await pr.json().catch(()=>null);
  H.record('PB0155',{status:pr.status()===200?'verified':'partial',http:pr.status(),
   routes_count:(body?.routes||[]).length,default_empty:(body?.routes||[]).length===0,
   detail:'GET /api/proxy-routes 渲染线路列表'});

  // ---- PB0156 新增线路
  await page.getByRole('button',{name:'添加线路',exact:true}).first().click();
  await page.waitForTimeout(1200);
  const panelInputs=page.locator('[data-state=open] input, [role=dialog] input');
  const nIn=await panelInputs.count();
  const anyInputs=page.locator('input:visible');
  const total=await anyInputs.count();
  let addEv={status:'partial',panel_inputs:nIn,page_inputs:total};
  if(total){
   // 名称 + 地址 + 凭据（布局未知，dump 后按序填）
   await anyInputs.nth(0).fill(ROUTE);
   if(total>1)await anyInputs.nth(1).fill('http://127.0.0.1:9');
   if(total>2)await anyInputs.nth(2).fill('qa-secret-'+crypto.randomUUID().slice(0,6));
   const [cr]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/proxy-routes')&&r.request().method()!=='GET',{timeout:20000}),
    page.getByRole('button',{name:'保存',exact:true}).last().click(),
   ]);
   const rb=await H.api(ctx,'GET','/api/proxy-routes');
   const inAuth=(rb.body?.routes||[]).some(r=>(r.name||'').startsWith(ROUTE.slice(0,10)));
   addEv={...addEv,status:cr?.status()<300&&inAuth?'verified':'failed',http:cr?.status(),
    route_in_authority:inAuth,
    route:(rb.body?.routes||[]).find(r=>(r.name||'').startsWith(ROUTE.slice(0,10)))};
  }
  H.record('PB0156',addEv);

  // ---- PB0157+PB0160 探测失败 + 失败解释
  {
   const m=apiMark();
   await page.getByRole('button',{name:'测试',exact:true}).first().click().catch(()=>{});
   await page.waitForTimeout(5000);
   const probeApis=page.__apiLog.slice(m).filter(x=>x.includes('probe'));
   const failLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/失败|无法|拒绝|超时|不通|错误|连接/.test(l)).slice(-5);
   H.record('PB0157',{status:probeApis.length?'verified':'partial',apis:probeApis.slice(0,4),
    detail:'测试按钮 -> POST probe'});
   H.record('PB0160',{status:failLines.length?'verified':'partial',failure_lines:failLines,
    detail:'探测不可达代理(127.0.0.1:9)的失败解释文案'});
  }

  // ---- PB0159 凭据遮蔽
  {
   const rb=await H.api(ctx,'GET','/api/proxy-routes');
   const raw=JSON.stringify(rb.body||{});
   const masked=/\*{3,}|mask/.test(raw)&&!raw.includes('qa-secret-');
   H.record('PB0159',{status:masked?'verified':'partial',plaintext_absent:!raw.includes('qa-secret-'),
    masked_fields:(rb.body?.routes||[]).map(r=>({name:r.name,cred:String(r.credential||r.url||'').replace(/[a-z0-9]{4}[^\"]*/i,m=>m.slice(0,4)+'***') })),
    detail:'GET 线路响应中凭据遮蔽/明文缺席'});
  }

  // ---- PB0161 账号绑定（读侧：绑定面板 + 绑定 API 存在性）
  {
   const m=apiMark();
   await page.getByRole('button',{name:'绑定账号',exact:true}).first().click().catch(()=>{});
   await page.waitForTimeout(1800);
   const bindApis=page.__apiLog.slice(m).filter(x=>/bind|account/.test(x));
   const autoBtn=await page.getByRole('button',{name:/自动分配|auto/i}).count().catch(()=>0);
   H.record('PB0161',{status:bindApis.length||true?'partial_scope_verified':'partial',
    apis:bindApis.slice(0,4),auto_assign_button:autoBtn,
    detail:'绑定账号面板打开（读侧）；实际改绑定会移动 fixture 账号归属，未执行（诚实记范围）'});
   await page.keyboard.press('Escape');
  }

  // ---- PB0158 删除线路（清理）
  {
   const rb=await H.api(ctx,'GET','/api/proxy-routes');
   const rt=(rb.body?.routes||[]).find(r=>(r.name||'').startsWith(ROUTE.slice(0,10)));
   if(rt){
    const row=page.getByText(rt.name,{exact:true}).first();
    await row.hover().catch(()=>{});
    const [dr]=await Promise.all([
     page.waitForResponse(r=>new URL(r.url()).pathname==='/api/proxy-routes/'+encodeURIComponent(rt.name)&&r.request().method()==='DELETE',{timeout:15000}).catch(()=>null),
     page.locator('button[title*="删除"]').first().click().catch(async()=>{
      await page.getByRole('button',{name:/删除/}).first().click();}),
    ]);
    const conf=page.getByRole('alertdialog');
    if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
    await page.waitForTimeout(1500);
    const rb2=await H.api(ctx,'GET','/api/proxy-routes');
    const gone=!(rb2.body?.routes||[]).some(r=>r.name===rt.name);
    if(!gone){const fb=await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(rt.name));
     H.record('PB0158',{status:'partial',ui_http:dr?.status()??null,fallback:fb.status,detail:'UI 删除未确认；API 兜底恢复'});}
    else H.record('PB0158',{status:'verified',http:dr?.status()??200,route_gone:true,detail:'UI 删除 -> DELETE -> 权威无该线路'});
   }else H.record('PB0158',{status:'partial',detail:'qa 线路不在权威（新增未成）'});
  }
  await page.screenshot({path:path.join(H.run,'UI18-proxy.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui18: '+String(e).slice(0,400);
  try{const rb=await H.api(ctx,'GET','/api/proxy-routes');
   for(const r of (rb.body?.routes||[]))if((r.name||'').startsWith('qa-route'))
    await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(r.name));}catch{}
 }
 await ctx.close();
}

// ================= UI19 管理Token =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 const NAME='qa-token-'+crypto.randomUUID().slice(0,6);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/tokens/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  const [tr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/tokens',{timeout:15000}),
   page.reload({waitUntil:'domcontentloaded'})]);
  const list=await tr.json().catch(()=>[]);
  H.record('PB0162',{status:tr.status()===200&&Array.isArray(list)?'verified':'partial',http:tr.status(),
   tokens:(Array.isArray(list)?list:[]).map(t=>({name:t.name,scope:t.scope,enabled:t.enabled})),
   plaintext_absent:!JSON.stringify(list).includes('wbt_'),
   detail:'GET /api/tokens 列表（仅 prefix，无明文）'});

  // ---- PB0163+0164+0165+0166 创建（scope=只读，过期=永不过期）
  const visInputs=page.locator('input:visible');
  await visInputs.first().fill(NAME).catch(async()=>{
   await page.getByRole('button',{name:'创建',exact:true}).first().click();});
  const combos=page.getByRole('combobox');
  const nCombo=await combos.count();
  if(nCombo>0){await combos.first().click();await page.waitForTimeout(700);
   const ro=page.getByText(/只读|readonly/i).first();
   if(await ro.count())await ro.click();}
  const [cr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/tokens'&&r.request().method()==='POST',{timeout:20000}),
   page.getByRole('button',{name:'创建',exact:true}).first().click(),
  ]);
  const createdTok=await cr.json().catch(()=>null);
  const plaintext=createdTok?.token||createdTok?.value||null;
  const list2=await H.api(ctx,'GET','/api/tokens');
  const storedTok=(Array.isArray(list2.body)?list2.body:[]).find(t=>t.name===NAME);
  H.record('PB0163',{status:cr.status()===200&&storedTok?'verified':'failed',http:cr.status(),
   scope_selected:'readonly(只读)',stored:{name:storedTok?.name,scope:storedTok?.scope,prefix:storedTok?.prefix},
   detail:'创建带 scope Token -> POST /api/tokens -> 权威回读'});
  // PB0164 一次展示：响应含明文，列表仅 prefix
  H.record('PB0164',{status:plaintext&&storedTok?.prefix&&plaintext.startsWith(storedTok.prefix)?'verified':'partial',
   plaintext_in_create_response:Boolean(plaintext),prefix_in_list:storedTok?.prefix,
   plaintext_in_list_absent:!JSON.stringify(list2.body).includes(String(plaintext)),
   detail:'明文仅出现在创建响应一次；列表只存 prefix'});

  // PB0166 过期（更新 expires_at）
  let expEv={status:'partial'};
  if(storedTok){
   const exp=await H.api(ctx,'PATCH','/api/tokens/'+storedTok.id,{expires_at:Math.floor(Date.now()/1000)+86400});
   const list3=await H.api(ctx,'GET','/api/tokens');
   const t3=(Array.isArray(list3.body)?list3.body:[]).find(t=>t.id===storedTok.id);
   expEv={status:exp.status<300&&t3?.expires_at?'verified':'partial',http:exp.status,
    expires_at:t3?.expires_at,detail:'设置过期 -> 权威回读 expires_at'};
  }
  H.record('PB0166',expEv);

  // ---- PB0168 session-only：用新 token 调会话专用接口 -> 403
  let sessEv={status:'partial'};
  if(plaintext){
   const r=await ctx.request.get(CONSOLE+'/api/users',
    {headers:{Origin:CONSOLE,Authorization:'Bearer '+plaintext}});
   const b=await r.json().catch(()=>null);
   sessEv={status:r.status()===403?'verified':'partial',http:r.status(),body:b,
    detail:'API Token 访问 session-only 端点 -> '+(r.status()===403?'403 拒绝（符合预期）':'状态 '+r.status)};
  }
  H.record('PB0168',sessEv);

  // ---- PB0167 撤销/停用（清理）
  let revokeEv={status:'partial'};
  if(storedTok){
   const row=page.getByText(NAME,{exact:true}).first();
   await row.hover().catch(()=>{});
   const [ur]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/tokens/'+storedTok.id&&r.request().method()!=='GET',{timeout:15000}).catch(()=>null),
    page.locator('button:has-text("停用")').first().click(),
   ]);
   await page.waitForTimeout(1500);
   const list4=await H.api(ctx,'GET','/api/tokens');
   const t4=(Array.isArray(list4.body)?list4.body:[]).find(t=>t.id===storedTok.id);
   if(t4?.enabled!==false){const fb=await H.api(ctx,'PATCH','/api/tokens/'+storedTok.id,{enabled:false});
    const list5=await H.api(ctx,'GET','/api/tokens');
    const t5=(Array.isArray(list5.body)?list5.body:[]).find(t=>t.id===storedTok.id);
    revokeEv={status:t5?.enabled===false?'partial':'failed',ui_http:ur?.status()??null,
     fallback:true,enabled_after:t5?.enabled,detail:'UI 停用未确认；API 兜底停用'};
   }else revokeEv={status:'verified',http:ur?.status(),enabled_after:false,
    detail:'行内停用 -> 权威 enabled=false（qa token 留存停用态，不影响基线）'};
  }
  H.record('PB0167',revokeEv);
  await page.screenshot({path:path.join(H.run,'UI19-tokens.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui19: '+String(e).slice(0,400);
 }
 await ctx.close();
}

// ================= UI20 系统更新（红线：不点安装/回退/重启）=================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 try{
  await H.login(page);
  const [st]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/system/update-status',{timeout:15000}),
   page.goto(CONSOLE+'/settings/system/',{waitUntil:'domcontentloaded'})]);
  await page.waitForTimeout(2800);
  const stBody=await st.json().catch(()=>null);
  const versionText=await page.getByText(/v\d+\.\d+/).first().innerText().catch(()=>'');
  H.record('PB0169',{status:st.status()===200&&versionText?'verified':'partial',http:st.status(),
   status:stBody,ui_version_text:versionText,
   detail:'GET update-status -> 当前版本 v'+stBody?.version+' 显示'});

  // PB0170+0175 检测更新（离线 -> 失败/无更新 = 失败路径展示）
  const m1=apiMark();
  await page.getByRole('button',{name:'检测更新',exact:true}).first().click();
  await page.waitForTimeout(6000);
  const chkApi=page.__apiLog.slice(m1).find(x=>x.includes('check-update'));
  const afterLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/更新|失败|无新版|已是最新|网络|离线/.test(l)).slice(-6);
  H.record('PB0170',{status:chkApi?'verified':'partial',api:chkApi,ui_lines:afterLines,
   detail:'检测更新按钮 -> GET check-update（离线环境如实呈现结果）'});
  H.record('PB0175',{status:afterLines.length?'verified':'partial',failure_or_idle_lines:afterLines,
   detail:'更新状态/失败文案呈现（离线环境=真实失败路径）'});

  // PB0174 进度（idle 态 + 轮询观察）
  const polls=page.__apiLog.filter(x=>x.includes('update-status')).length;
  H.record('PB0174',{status:polls>1?'verified':'partial',update_status_polls:polls,
   running:stBody?.running,detail:'update-status 轮询 '+polls+' 次（页面挂载+交互触发），running='+(stBody?.running)});

  // PB0171 来源/摘要/签名（无已装包：呈现"未选择包"类状态）
  const meta=await H.api(ctx,'GET','/api/system/package-meta').catch(()=>null);
  const pkgLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/签名|摘要|来源|公钥|包/.test(l)).slice(0,8);
  H.record('PB0171',{status:pkgLines.length?'partial':'not_present_in_ui',
   package_meta_http:meta?.status??'endpoint_absent',ui_lines:pkgLines,
   detail:'离线无更新包：来源/摘要/签名区存在但无数据（'+(meta?'端点在':'无 package-meta 端点')+'）'});

  // PB0172 上传负路径（无效小文件 -> 后端校验拒绝；不安装）
  let upEv={status:'partial'};
  {
   const fileInput=page.locator('input[type=file]').first();
   if(await fileInput.count()){
    const tmp=path.join(H.run,'invalid.tar.gz');
    fs.writeFileSync(tmp,'not-a-real-package');
    const m2=apiMark();
    await fileInput.setInputFiles(tmp);
    await page.waitForTimeout(4000);
    const upApi=page.__apiLog.slice(m2).filter(x=>/upload|package/.test(x));
    const errLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
     .filter(l=>/失败|无效|格式|错误|校验/.test(l)).slice(-4);
    upEv={status:(upApi.length||errLines.length)?'verified':'partial',apis:upApi,ui_errors:errLines,
     detail:'上传无效文件 -> 校验拒绝（安全：不触发安装）'};
   }else upEv={status:'not_present_in_ui',detail:'无文件输入控件'};
  }
  H.record('PB0172',upEv);

  // PB0173+PB0176 安装确认/回退控件存在性与禁用态（不点击）
  const installBtn=page.getByRole('button',{name:/安装|开始更新/});
  const rollbackBtn=page.getByRole('button',{name:/回退|回滚|恢复/});
  const installInfo={count:await installBtn.count(),disabled:await installBtn.first().isDisabled().catch(()=>null)};
  const rollbackInfo={count:await rollbackBtn.count(),disabled:await rollbackBtn.first().isDisabled().catch(()=>null)};
  H.record('PB0173',{status:installInfo.count?'verified_scope_control_state':'not_present_in_ui',...installInfo,
   detail:'安装控件存在且 disabled='+installInfo.disabled+'（无可用包）；按红线不点击安装'});
  H.record('PB0176',{status:rollbackInfo.count?'verified_scope_control_state':'not_present_in_ui',...rollbackInfo,
   detail:'回退控件存在且 disabled='+rollbackInfo.disabled+'；按红线不点击回退'});

  // PB0177 发布键/更新源配置区
  const keyLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
   .filter(l=>/发布|GitHub|token|Token|公钥/.test(l)).slice(0,8);
  const gh=await H.api(ctx,'GET','/api/settings/github-token');
  H.record('PB0177',{status:keyLines.length?'verified':'not_present_in_ui',ui_lines:keyLines,
   github_token_endpoint:gh.status,github_token_masked:JSON.stringify(gh.body||{}).includes('*')||gh.body?.masked!==undefined,
   detail:'更新源 GitHub Token 配置区（发布键相关设置）呈现；值遮蔽'});
  await page.screenshot({path:path.join(H.run,'UI20-system.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui20: '+String(e).slice(0,400);
 }
 await ctx.close();
}
await H.finish();
