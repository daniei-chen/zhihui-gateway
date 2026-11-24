import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';

const compile = name => ts.transpileModule(fs.readFileSync(new URL(name, import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const api = {exports: {}};
vm.runInNewContext(compile('./portal-api.ts'), {module:api,exports:api.exports,require:()=>({BASE_PATH:''})});
const display = {exports:{}};
vm.runInNewContext(compile('./portal-display.ts'), {module:display,exports:display.exports,require:()=>api.exports});
const {checkinSummary,requestStatus,requestTokens,requestLatency,requestCredit}=display.exports;

test('checkin progress counts only confirmed eligible accounts and keeps unknown rewards pending',()=>{
  const account={status:'active',initial_state:'confirmed'};
  const data=[null,'confirmed','confirmed_uncredited','unknown','running','reconciling','failed'].map(checkin_state=>({...account,checkin_state}));
  data.push({...account,status:'disabled',checkin_state:'confirmed'});
  data.push({...account,initial_state:'pending',checkin_state:'confirmed'});
  const result=checkinSummary(data);
  assert.equal(result.total,7);assert.equal(result.confirmed,1);assert.equal(result.ready,2);assert.equal(result.pending,4);
});

// 组件侧模板文案走 t()：这里的 t 直接把键名当译文返回，于是断言检查的是
// 「用了哪个键」而不是「用了哪句话」—— 措辞调整不该让测试变红。
const t = key => key;

test('request presentation keeps zero distinct from unknown and never fabricates measurements',()=>{
  assert.equal(requestCredit(0,t),'0');assert.equal(requestCredit(null,t),'portal.pending');assert.equal(requestCredit(-1,t),'portal.pending');
  assert.equal(requestTokens(3,7,t),'10');assert.equal(requestTokens(null,7,t),'portal.pending');
  assert.equal(requestTokens(Number.MAX_SAFE_INTEGER,1,t),'portal.pending');assert.equal(requestTokens(0,0,t),'0');
  assert.equal(requestLatency(0,t),'0 ms');assert.equal(requestLatency(1250,t),'1.25 s');assert.equal(requestLatency(NaN,t),'portal.pending');
  assert.equal(requestStatus(200,t).label,'portal.status.success');assert.equal(requestStatus(499,t).label,'portal.status.cancelled');assert.equal(requestStatus(503,t).label,'portal.status.failed');
});
