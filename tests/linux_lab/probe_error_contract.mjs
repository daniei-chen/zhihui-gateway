// K29 error-contract probe: per-endpoint anonymous/viewer/admin error envelopes
// across platform routing. Read-only or rejection-only probes: admin credentials
// are never sent to action endpoints (restart/update/clear/run/revoke family);
// those are probed anonymous+viewer only (401/403 shape is their contract).
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const BASE='https://127.0.0.1:18443';
const fixture=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));
if(fixture.production_connected!==false)throw new Error('local fixture only');

async function login(username){
 const r=await fetch(BASE+'/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({username,password:'synthetic-password'})});
 if(r.status!==200)throw new Error('login failed for '+username+': '+r.status);
 return r.headers.getSetCookie().map(c=>c.split(';')[0]).join('; ');
}
const ADMIN=await login('gateway-admin');
const VIEWER=await login('只读用户');

// endpoint, method, adminProbe: 'read'|'none'|'invalid'
// 'invalid' = admin + empty JSON body on write endpoints whose validation runs first.
const E=(m,p,adminProbe='read')=>({m,p,adminProbe});
const endpoints=[
 E('GET','/api/me','read'),E('GET','/api/status','read'),E('GET','/api/models','read'),
 E('GET','/api/model-catalog','read'),E('GET','/api/playground/models','read'),
 E('GET','/api/accounts','read'),E('GET','/api/accounts/daily-plan','read'),
 E('GET','/api/accounts/refresh-plan','read'),E('GET','/api/accounts/export','read'),
 E('GET','/api/keys','read'),E('GET','/api/keys/import-local/status','read'),
 E('GET','/api/keys/import-local/detect','read'),E('GET','/api/stats/summary','read'),
 E('GET','/api/stats/daily','read'),E('GET','/api/stats/hourly','read'),
 E('GET','/api/stats/by-key','read'),E('GET','/api/stats/by-model','read'),
 E('GET','/api/stats/credits','read'),E('GET','/api/stats/upstream','read'),
 E('GET','/api/audit-logs','read'),E('GET','/api/checkin-logs','read'),
 E('GET','/api/task-logs','read'),E('GET','/api/task-run','read'),
 E('GET','/api/task-claim-schedule','read'),E('GET','/api/upstreams','read'),
 E('GET','/api/upstream/logs','read'),E('GET','/api/upstream/reload-state','read'),
 E('GET','/api/proxies','read'),E('GET','/api/proxy-routes','read'),
 E('GET','/api/proxy-routes/bindings','read'),E('GET','/api/users','read'),
 E('GET','/api/tokens','read'),E('GET','/api/red-packets','read'),
 E('GET','/api/red-packets/999999','read'),E('GET','/api/keys/999999','read'),
 E('GET','/api/security/config','read'),E('GET','/api/security/logs','read'),
 E('GET','/api/security/rules','read'),E('GET','/api/settings/upstream','read'),
 E('GET','/api/settings/proxy','read'),E('GET','/api/settings/pg-sync','read'),
 E('GET','/api/settings/pg-sync/status','read'),E('GET','/api/settings/github-mirror','read'),
 E('GET','/api/settings/github-token','read'),E('GET','/api/system/versions','read'),
 E('GET','/api/system/changelog','read'),E('GET','/api/system/check-update','read'),
 E('GET','/api/system/update-status','read'),E('GET','/api/system/update-package','read'),
 E('GET','/api/auth/poll','read'),E('GET','/api/nonexistent-endpoint','read'),
 // write/validation endpoints: anonymous + viewer shape; admin only where validation
 // clearly precedes any side effect ('invalid' = empty JSON body -> 422 expected)
 E('POST','/api/keys','invalid'),E('POST','/api/tokens','invalid'),E('POST','/api/users','invalid'),
 E('POST','/api/proxy-routes','invalid'),E('POST','/api/red-packets','invalid'),
 E('POST','/api/settings/pg-sync/test','none'),E('POST','/api/settings/upstash/test','none'),
 E('POST','/api/settings/proxy/probe','none'),E('POST','/api/upstreams/999999/probe','none'),
 E('PATCH','/api/keys/999999','invalid'),E('PATCH','/api/tokens/999999','invalid'),
 E('PATCH','/api/users/nonexistent-user','invalid'),E('PATCH','/api/upstreams/999999','invalid'),
 E('DELETE','/api/keys/999999','invalid'),E('DELETE','/api/tokens/999999','invalid'),
 E('DELETE','/api/users/nonexistent-user','invalid'),E('DELETE','/api/upstreams/999999','invalid'),
 E('DELETE','/api/proxy-routes/nonexistent','invalid'),E('DELETE','/api/security/rules/999999','invalid'),
 // action family: NEVER with admin credentials here
 E('POST','/api/restart','none'),E('POST','/api/system/update','none'),
 E('POST','/api/system/update-package','none'),E('DELETE','/api/system/update-package','none'),
 E('POST','/api/logout','none'),E('POST','/api/sessions/revoke','none'),
 E('POST','/api/logs/clear','none'),E('POST','/api/task-logs/clear','none'),
 E('POST','/api/checkin-logs/clear','none'),E('POST','/api/security/logs/clear','none'),
 E('POST','/api/accounts/checkin-all','none'),E('POST','/api/task-run/stop','none'),
 E('POST','/api/accounts/refresh-plan/run','none'),E('POST','/api/accounts/daily-plan/run','none'),
 E('POST','/api/stats/rebuild-usage','none'),E('POST','/api/stats/repair-usage','none'),
 E('POST','/api/task-logs/collect','none'),
];
const auths=[['anon',null],['viewer',VIEWER]];
const results=[];
async function probe(platform,authName,cookie,ep){
 const headers={};
 if(cookie)headers.Cookie=cookie;
 if(platform!=='workbuddy')headers['X-QTG-Platform']=platform;
 if(ep.adminProbe==='invalid'&&authName==='admin')headers['Content-Type']='application/json';
 const init={method:ep.m,headers};
 if(ep.adminProbe==='invalid'&&authName==='admin')init.body=JSON.stringify({});
 const r=await fetch(BASE+ep.p,{...init,signal:AbortSignal.timeout(15000)}).catch(e=>({status:'ERR',headers:new Map(),text:async()=>String(e)}) );
 const ct=r.headers.get?.('content-type')||'';
 let shape='';
 try{const t=await r.text();const j=t.startsWith('{')||t.startsWith('[')?JSON.parse(t):null;
  shape=j===null?(t.slice(0,40).includes('<!DOCTYPE')||t.slice(0,40).includes('<html')?'html':'text')
   :Array.isArray(j)?'json:array':Object.keys(j).slice(0,6).map(k=>k+':'+(typeof j[k]==='object'&&j[k]?'obj':typeof j[k]==='string'?'str':j[k])).join(',');}catch{shape='parse-err';}
 results.push({ep:ep.m+' '+ep.p,platform,auth:authName,status:r.status,ct:ct.split(';')[0],shape});
}
const targets=[];
for(const ep of endpoints){
 for(const platform of ['workbuddy','qoder']){
  for(const [name,c] of auths)targets.push([platform,name,c,ep]);
  if(ep.adminProbe!=='none')targets.push([platform,'admin',ADMIN,ep]);
 }
}
let i=0;
for(const [platform,name,c,ep] of targets){await probe(platform,name,c,ep);if(++i%100===0)console.log('...',i,'/',targets.length);}
const run=path.resolve('evidence/k29-error-contract-'+new Date().toISOString().slice(0,10)+'-'+crypto.randomUUID().slice(0,6));
fs.mkdirSync(run,{recursive:true});
const out={schema:'zhihui-k29-error-contract/v1',fixture:fixture.source_sha256,production_connected:false,
 probes:targets.length,results,generated_at:new Date().toISOString()};
fs.writeFileSync(path.join(run,'matrix.json'),JSON.stringify(out,null,1));
// summary: status x platform x auth
const agg={};
for(const r of results){const k=`${r.platform}/${r.auth}`;(agg[k]??={});(agg[k][r.status]??=0);agg[k][r.status]++;}
fs.writeFileSync(path.join(run,'summary.json'),JSON.stringify(agg,null,1));
console.log('probes:',targets.length);
console.log(JSON.stringify(agg,null,1));
console.log('K29_RUN '+run);
