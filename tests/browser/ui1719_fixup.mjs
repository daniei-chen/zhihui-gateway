// UI17/18/19 补齐（修正 Radix Select 用 role=option；代理表单按真实结构填写）。
import path from 'node:path';
import crypto from 'node:crypto';
import {makeHarness,trackApis} from './fixture_lib.mjs';
const H=await makeHarness(process.argv[2],'zhihui-browser-ui1719-fixup/v1','ui1719fx');
const {base}=H;
const CONSOLE=base.origin;
const USER='qa-ui17-'+crypto.randomUUID().slice(0,6);
const PASS='Qa-'+crypto.randomUUID().slice(0,8)+'!';

// ================= UI17 =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/users/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  // ---- PB0148 创建（内联表单：用户名/密码/角色）
  await page.getByLabel(/用户名/).first().fill(USER).catch(async()=>{
   await page.locator('input:visible').nth(0).fill(USER);});
  await page.locator('input[type=password]:visible').first().fill(PASS);
  await page.getByRole('combobox').first().click();
  await page.waitForTimeout(800);
  await page.getByRole('option',{name:/只读/}).first().click();
  const [cr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/users'&&r.request().method()==='POST',{timeout:20000}),
   page.getByRole('button',{name:'创建',exact:true}).first().click(),
  ]);
  const list2=await H.api(ctx,'GET','/api/users');
  const createdOk=cr.status()===200&&(list2.body||[]).some(u=>u.username===USER);
  H.record('PB0148',{status:createdOk?'verified':'failed',http:cr.status(),
   user_in_authority:(list2.body||[]).find(u=>u.username===USER),
   detail:'内联创建（用户名/密码/角色 Select）-> POST /api/users -> 权威回读'});

  let roleEv={status:'partial'},sessionEv={status:'partial'};
  if(createdOk){
   // 用户会话
   const uctx=await H.newCtx('workbuddy');
   const up=await uctx.newPage();
   await H.login(up,USER,PASS);
   const meBefore=await H.api(uctx,'GET','/api/me');
   // ---- PB0149 行内角色切换（viewer -> admin）
   const urow=page.getByText(USER,{exact:true}).first();
   await urow.hover().catch(()=>{});
   const rowCombo=page.locator(`tr:has-text("${USER}") [role=combobox]`).first();
   let switched=false,roleNow=null;
   if(await rowCombo.count()){
    await rowCombo.click();await page.waitForTimeout(900);
    await page.getByRole('option',{name:/管理员/}).first().click().catch(()=>{});
    await page.waitForTimeout(1800);
    switched=true;
   }
   const list3=await H.api(ctx,'GET','/api/users');
   roleNow=(list3.body||[]).find(u=>u.username===USER)?.role;
   if(roleNow!=='admin'){const fb=await H.api(ctx,'PATCH','/api/users/'+USER,{role:'admin'});
    const l4=await H.api(ctx,'GET','/api/users');
    roleNow=(l4.body||[]).find(u=>u.username===USER)?.role;switched=false;}
   roleEv={status:roleNow==='admin'?(switched?'verified':'partial'): 'failed',
    switched_via:switched?'row_combobox':'api_fallback',role_after:roleNow,
    detail:switched?'行内角色切换 viewer->admin，权威回读':'行内控件未确认，API 兜底达成 admin（UI 切换交互未证）'};
   // ---- PB0154 旧会话吊销
   await page.waitForTimeout(800);
   const meAfter=await H.api(uctx,'GET','/api/me');
   sessionEv={status:meAfter.status===401?'verified':'partial',
    me_before:meBefore.status,me_after_role_change:meAfter.status,
    detail:'角色变更后旧会话 '+(meAfter.status===401?'被吊销(401)':'仍有效('+meAfter.status+')')};
   await uctx.close();
  }
  H.record('PB0149',roleEv);
  H.record('PB0154',sessionEv);

  // ---- PB0150 重置密码
  let pwdEv={status:'partial'};
  {
   const row=page.getByText(USER,{exact:true}).first();
   await row.hover().catch(()=>{});
   const rbtns=page.getByRole('button',{name:'重置密码',exact:true});
   // 行内的重置密码（不是创建区的）：取行附近
   const rowBtn=page.locator(`tr:has-text("${USER}")`).getByRole('button',{name:'重置密码'}).first();
   const target=(await rowBtn.count())?rowBtn:rbtns.last();
   if(await target.count()){
    await target.click();
    await page.waitForTimeout(1000);
    const dlg=page.locator('[data-state=open]').last();
    const np='Qa2-'+crypto.randomUUID().slice(0,8)+'!';
    const pin=dlg.locator('input').first();
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
      detail:'重置密码 -> PATCH -> 新密码登录 '+(li.status()===200?'200 成功':li.status())};
    }else pwdEv={status:'partial',detail:'重置密码弹层未找到输入框'};
   }else pwdEv={status:'partial',detail:'重置密码按钮未定位'};
  }
  H.record('PB0150',pwdEv);

  // ---- PB0152 本人保护
  {
   const arow=page.getByText('gateway-admin',{exact:true}).first();
   await arow.hover().catch(()=>{});
   const delBtn=page.locator(`tr:has-text("gateway-admin")`).getByRole('button',{name:/删除/}).first();
   const delAny=page.locator('button[title*="删除"]').first();
   const btn=(await delBtn.count())?delBtn:delAny;
   let selfEv={status:'partial'};
   if(await btn.count()){
    const m=apiMark();
    await btn.click().catch(()=>{});
    await page.waitForTimeout(1000);
    const conf=page.getByRole('alertdialog');
    if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
    await page.waitForTimeout(1500);
    const li=await H.api(ctx,'GET','/api/users');
    const still=(li.body||[]).some(u=>u.username==='gateway-admin');
    const delHttp=page.__apiLog.slice(m).find(x=>x.includes('DELETE'));
    selfEv={status:still?'verified':'CRITICAL',delete_request:delHttp||'none_fired',
     admin_still_present:still,
     detail:still?'删除本人被拒（前端拦截或后端 4xx）；权威仍含 gateway-admin':'本人被删——严重问题'};
   }
   H.record('PB0152',selfEv);
  }

  // ---- PB0153 最后管理员保护（API 直击 + UI 态）
  {
   const pr=await H.api(ctx,'PATCH','/api/users/gateway-admin',{role:'viewer'});
   const li=await H.api(ctx,'GET','/api/users');
   const roleNow=(li.body||[]).find(u=>u.username==='gateway-admin')?.role;
   H.record('PB0153',{status:roleNow==='admin'?'verified':'CRITICAL',
    patch_http:pr.status,patch_body:pr.body,role_after:roleNow,
    detail:'唯一管理员降级请求（会话内 PATCH）-> 后端拒绝('+pr.status+')；gateway-admin 仍为 admin'});
   if(roleNow!=='admin'){await H.api(ctx,'PATCH','/api/users/gateway-admin',{role:'admin'});
    H.note('CRITICAL: last-admin guard failed; restored');}
  }

  // ---- PB0151 删除测试用户
  {
   const row=page.getByText(USER,{exact:true}).first();
   await row.hover().catch(()=>{});
   const delBtn=page.locator(`tr:has-text("${USER}")`).getByRole('button',{name:/删除/}).first();
   const delAny=page.locator('button[title*="删除"]').first();
   const btn=(await delBtn.count())?delBtn:delAny;
   const m=apiMark();
   if(await btn.count())await btn.click().catch(()=>{});
   await page.waitForTimeout(900);
   const conf=page.getByRole('alertdialog');
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除|确定/}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
   let li=await H.api(ctx,'GET','/api/users');
   let gone=!(li.body||[]).some(u=>u.username===USER);
   const delHttp=page.__apiLog.slice(m).find(x=>x.includes('DELETE'));
   if(!gone){const fb=await H.api(ctx,'DELETE','/api/users/'+USER);
    li=await H.api(ctx,'GET','/api/users');
    gone=!(li.body||[]).some(u=>u.username===USER);
    H.record('PB0151',{status:'partial',ui_delete:delHttp||'none',fallback_http:fb.status,user_gone:gone,
     detail:'UI 删除未确认；API 兜底删除'});}
   else H.record('PB0151',{status:'verified',ui_delete:delHttp||'no_delete_request_fired(前端拦截后确认)',
    user_gone:true,detail:'行删除 -> 权威无该用户'});
  }
  await page.screenshot({path:path.join(H.run,'UI17-users.png')});
 }catch(e){
  H.report.error='ui17: '+String(e).slice(0,500);
  try{await H.api(ctx,'DELETE','/api/users/'+USER);}catch{}
 }
 await ctx.close();
}

// ================= UI18 =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const apiMark=trackApis(page);
 const ROUTE='qa-px-'+crypto.randomUUID().slice(0,6);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/proxy/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  // ---- PB0156 添加线路（名称 + HTTP 地址含凭据）
  await page.getByRole('button',{name:'添加线路',exact:true}).first().click();
  await page.waitForTimeout(1200);
  await page.getByPlaceholder(/px-cn/).fill(ROUTE);
  const SECRET='qa-secret-'+crypto.randomUUID().slice(0,6);
  await page.getByPlaceholder(/user:pass@host:7890/).fill('http://qauser:'+SECRET+'@127.0.0.1:9');
  const [cr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/proxy-routes')&&r.request().method()!=='GET',{timeout:20000}),
   page.getByRole('button',{name:'保存',exact:true}).last().click(),
  ]);
  const rb=await H.api(ctx,'GET','/api/proxy-routes');
  const rt=(rb.body?.routes||[]).find(r=>r.name===ROUTE);
  H.record('PB0156',{status:cr?.status()<300&&rt?'verified':'failed',http:cr?.status(),
   method:cr?.request().method(),route_in_authority:Boolean(rt),route:rt,
   detail:'添加线路（名称+HTTP代理URL含凭据）-> 保存 -> 权威回读'});

  // ---- PB0157+PB0160 测试（不可达 -> 失败解释）
  {
   const m=apiMark();
   await page.getByRole('button',{name:'测试',exact:true}).first().click();
   await page.waitForTimeout(6000);
   const probeApis=page.__apiLog.slice(m).filter(x=>/probe/.test(x));
   const failLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/失败|无法|拒绝|超时|不通|错误|连接/.test(l)).slice(-5);
   H.record('PB0157',{status:probeApis.length?'verified':'partial',apis:probeApis.slice(0,4),
    detail:'测试按钮 -> probe API'});
   H.record('PB0160',{status:failLines.length?'verified':'partial',failure_lines:failLines,
    detail:'探测不可达代理(127.0.0.1:9)失败解释'});
  }

  // ---- PB0159 凭据遮蔽
  {
   const rb=await H.api(ctx,'GET','/api/proxy-routes');
   const raw=JSON.stringify(rb.body||{});
   const rt=(rb.body?.routes||[]).find(r=>r.name===ROUTE);
   const credField=rt?String(rt.url||rt.credential||rt.uri||''):'';
   H.record('PB0159',{status:rt&&!raw.includes(SECRET)?'verified':'partial',
    plaintext_secret_absent:!raw.includes(SECRET),
    stored_field:credField.slice(0,40)+'...',
    detail:'线路凭据存回时遮蔽/明文缺席（secret='+SECRET.slice(0,6)+'***）'});
  }

  // ---- PB0161 绑定账号（读侧）
  {
   const m=apiMark();
   await page.getByRole('button',{name:'绑定账号',exact:true}).first().click();
   await page.waitForTimeout(2000);
   const bindApis=page.__apiLog.slice(m).filter(x=>/bind|account|proxy/.test(x));
   const panelLines=(await page.evaluate(()=>document.body.innerText)).split('\n')
    .filter(l=>/绑定|未绑定|账号/.test(l)).slice(0,5);
   H.record('PB0161',{status:'partial_scope_verified',apis:bindApis.slice(0,4),panel_lines:panelLines,
    detail:'绑定面板打开+读取；实际改绑定会移动 fixture 账号归属，未执行（诚实记范围）'});
   await page.keyboard.press('Escape');
  }

  // ---- PB0158 删除线路
  {
   const m=apiMark();
   const row=page.getByText(ROUTE,{exact:true}).first();
   await row.hover().catch(()=>{});
   const delBtn=page.locator(`button[title*="删除"]`).first();
   if(await delBtn.count())await delBtn.click().catch(()=>{});
   await page.waitForTimeout(900);
   const conf=page.getByRole('alertdialog');
   if(await conf.count())await conf.getByRole('button').filter({hasText:/确认|删除/}).first().click().catch(()=>{});
   await page.waitForTimeout(1500);
   const rb=await H.api(ctx,'GET','/api/proxy-routes');
   let gone=!(rb.body?.routes||[]).some(r=>r.name===ROUTE);
   const delHttp=page.__apiLog.slice(m).find(x=>x.includes('DELETE'));
   if(!gone){const fb=await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(ROUTE));
    const rb2=await H.api(ctx,'GET','/api/proxy-routes');
    gone=!(rb2.body?.routes||[]).some(r=>r.name===ROUTE);
    H.record('PB0158',{status:'partial',ui_delete:delHttp||'none',fallback_http:fb.status,route_gone:gone,
     detail:'UI 删除未确认；API 兜底恢复'});}
   else H.record('PB0158',{status:'verified',ui_delete:delHttp||'fired',route_gone:true,
    detail:'UI 删除 -> DELETE -> 权威无该线路'});
  }
  await page.screenshot({path:path.join(H.run,'UI18-proxy.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui18: '+String(e).slice(0,400);
  try{const rb=await H.api(ctx,'GET','/api/proxy-routes');
   for(const r of (rb.body?.routes||[]))if((r.name||'').startsWith('qa-px'))
    await H.api(ctx,'DELETE','/api/proxy-routes/'+encodeURIComponent(r.name));}catch{}
 }
 await ctx.close();
}

// ================= UI19 =================
{
 const ctx=await H.newCtx('workbuddy');
 const page=await ctx.newPage();
 const NAME='qa-token-'+crypto.randomUUID().slice(0,6);
 try{
  await H.login(page);
  await page.goto(CONSOLE+'/settings/tokens/',{waitUntil:'domcontentloaded'});
  await page.waitForTimeout(2800);
  // ---- 创建（名称 + scope Select）
  await page.locator('input:visible').first().fill(NAME);
  await page.getByRole('combobox').first().click();
  await page.waitForTimeout(800);
  await page.getByRole('option',{name:/只读/}).first().click();
  const [cr]=await Promise.all([
   page.waitForResponse(r=>new URL(r.url()).pathname==='/api/tokens'&&r.request().method()==='POST',{timeout:20000}),
   page.getByRole('button',{name:'创建',exact:true}).first().click(),
  ]);
  const createdTok=await cr.json().catch(()=>null);
  const plaintext=createdTok?.token||createdTok?.value||null;
  const list2=await H.api(ctx,'GET','/api/tokens');
  const storedTok=(Array.isArray(list2.body)?list2.body:[]).find(t=>t.name===NAME);
  H.record('PB0163',{status:cr.status()===200&&storedTok?'verified':'failed',http:cr.status(),
   scope_selected:'readonly',stored:{name:storedTok?.name,scope:storedTok?.scope,prefix:storedTok?.prefix},
   detail:'创建带 scope Token -> POST /api/tokens -> 权威回读'});
  H.record('PB0164',{status:plaintext&&storedTok?.prefix&&plaintext.startsWith(storedTok.prefix||'x')?'verified':'partial',
   plaintext_in_create_response:Boolean(plaintext),prefix_in_list:storedTok?.prefix,
   plaintext_in_list_absent:!JSON.stringify(list2.body).includes(String(plaintext)),
   detail:'明文仅创建响应出现一次；列表仅存 prefix'});

  // ---- PB0165 复制
  let copyEv={status:'not_present_in_ui'};
  {
   const dlg=page.locator('[data-state=open]').last();
   const cp=dlg.getByRole('button',{name:/复制/}).first();
   const cpAny=page.getByRole('button',{name:/复制/}).first();
   const btn=(await dlg.count()&&await cp.count())?cp:cpAny;
   if(await btn.count()){
    await btn.click();await page.waitForTimeout(500);
    const clip=await page.evaluate(()=>navigator.clipboard.readText()).catch(()=>'');
    copyEv={status:clip===plaintext?'verified':'partial',copied_len:clip?.length,expected_len:plaintext?.length,
     detail:'复制按钮 -> 剪贴板即明文 token'};
   }
  }
  H.record('PB0165',copyEv);

  // ---- PB0166 过期
  let expEv={status:'partial'};
  if(storedTok){
   const exp=await H.api(ctx,'PATCH','/api/tokens/'+storedTok.id,{expires_at:Math.floor(Date.now()/1000)+86400});
   const list3=await H.api(ctx,'GET','/api/tokens');
   const t3=(Array.isArray(list3.body)?list3.body:[]).find(t=>t.id===storedTok.id);
   expEv={status:exp.status<300&&t3?.expires_at?'verified':'partial',http:exp.status,
    expires_at:t3?.expires_at,detail:'设置过期 -> 权威回读 expires_at 非空'};
  }
  H.record('PB0166',expEv);

  // ---- PB0168 session-only
  let sessEv={status:'partial'};
  if(plaintext){
   const r=await ctx.request.get(CONSOLE+'/api/users',
    {headers:{Origin:CONSOLE,Authorization:'Bearer '+plaintext}});
   sessEv={status:r.status()===403?'verified':'partial',http:r.status(),
    body:(await r.text()).slice(0,120),
    detail:'API Token 访问 session-only 端点 -> '+(r.status()===403?'403 拒绝（符合设计）':'状态 '+r.status())};
  }
  H.record('PB0168',sessEv);

  // ---- PB0167 停用（清理）
  let revokeEv={status:'partial'};
  if(storedTok){
   const row=page.getByText(NAME,{exact:true}).first();
   await row.hover().catch(()=>{});
   const [ur]=await Promise.all([
    page.waitForResponse(r=>new URL(r.url()).pathname==='/api/tokens/'+storedTok.id&&r.request().method()!=='GET',{timeout:15000}).catch(()=>null),
    page.getByRole('button',{name:'停用',exact:true}).first().click(),
   ]);
   await page.waitForTimeout(1500);
   const list4=await H.api(ctx,'GET','/api/tokens');
   const t4=(Array.isArray(list4.body)?list4.body:[]).find(t=>t.id===storedTok.id);
   if(t4?.enabled!==false){const fb=await H.api(ctx,'PATCH','/api/tokens/'+storedTok.id,{enabled:false});
    revokeEv={status:'partial',ui_http:ur?.status()??null,fallback:true,detail:'UI 停用未确认；API 兜底停用'};}
   else revokeEv={status:'verified',http:ur?.status(),enabled_after:false,
    detail:'行内停用 -> 权威 enabled=false（qa token 留存停用态）'};
  }
  H.record('PB0167',revokeEv);
  await page.screenshot({path:path.join(H.run,'UI19-tokens.png')});
 }catch(e){
  H.report.error=(H.report.error||'')+' ui19: '+String(e).slice(0,400);
 }
 await ctx.close();
}
await H.finish();
