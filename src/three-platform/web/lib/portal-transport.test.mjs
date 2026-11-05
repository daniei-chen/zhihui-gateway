import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';

const code = ts.transpileModule(fs.readFileSync(new URL('./portal-api.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const flush = async () => {for (let i = 0; i < 10; i++) await Promise.resolve();};
function api(fetch, timers = {}) {
  const compiled = {exports: {}};
  vm.runInNewContext(code, {module: compiled, exports: compiled.exports, require: () => ({BASE_PATH: ''}), fetch,
    AbortController, setTimeout, clearTimeout, ...timers});
  return compiled.exports;
}

test('portal rejects successful non-JSON, null, array and malformed workspace responses', async () => {
  for (const value of ['<html>proxy error</html>', 'null', '[]', '{}', '{"id":9,"username":"wrong"}']) {
    const {portalFetch} = api(async () => new Response(value));
    await assert.rejects(portalFetch('portal/me'), /响应|数据/);
  }
  const {portalFetch} = api(async () => Response.json({accounts: null, dispatch_enabled: 'true'}));
  await assert.rejects(portalFetch('portal/overview'), /响应|数据/);
});

test('authorization status survives invalid JSON and valid business errors remain exact', async () => {
  const unauthorized = api(async () => new Response('not JSON', {status: 401}));
  await assert.rejects(unauthorized.portalFetch('portal/me'), error => error instanceof unauthorized.PortalError && error.status === 401);
  const conflict = api(async () => Response.json({detail: '积分待核实'}, {status: 409}));
  await assert.rejects(conflict.portalFetch('portal/keys', {method: 'POST'}), /积分待核实/);
});

test('known authorization rejection is not hidden by a stalled error body', async () => {
  let expire, signal;
  const transport = api(async (url, options) => {
    signal = options.signal;
    return {ok: false, status: 401, json: () => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), {once: true});
    })};
  }, {setTimeout(fn) {expire = fn; return 1;}, clearTimeout() {}});
  const work = transport.portalFetch('portal/me'); await flush(); expire();
  await assert.rejects(work, error => error instanceof transport.PortalError && error.status === 401);
});

test('read deadline covers response body and clears its timer without retrying', async () => {
  let timeout, cleared = 0, signal, calls = 0;
  const {portalFetch} = api(async (url, options) => {
    calls++; signal = options.signal;
    return {ok: true, status: 200, json: () => new Promise((resolve, reject) => {
      signal.addEventListener('abort', () => reject(signal.reason), {once: true});
    })};
  }, {setTimeout(fn) {timeout = fn; return 1;}, clearTimeout() {cleared++;}});
  const work = portalFetch('portal/me');
  await flush(); assert.equal(typeof timeout, 'function'); timeout();
  await assert.rejects(work, /超时/);
  assert.equal(signal.aborted, true); assert.equal(calls, 1); assert.equal(cleared, 1);
});

test('caller abort propagates and mutation failure is uncertain, with no retry', async () => {
  const controller = new AbortController(); controller.abort(new Error('caller aborted'));
  let calls = 0, timers = 0;
  const cancelled = api(async (url, options) => {calls++; throw options.signal.reason;});
  await assert.rejects(cancelled.portalFetch('portal/me', {signal: controller.signal}), /caller aborted/);
  assert.equal(calls, 0);
  const mutation = api(async () => {calls++; throw new Error('private connection internals');}, {
    setTimeout() {timers++; return 1;}, clearTimeout() {},
  });
  await assert.rejects(mutation.portalFetch('portal/keys', {method: 'POST'}), /结果待确认/);
  assert.equal(calls, 1); assert.equal(timers, 1);
});

test('mutation deadline bounds waiting and reports uncertainty without replay', async () => {
  let expire, wait, calls=0, cleared=0;
  const mutation=api((url,options)=>{
    calls++;
    return new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>reject(options.signal.reason),{once:true}));
  },{setTimeout(fn,ms){expire=fn;wait=ms;return 7;},clearTimeout(){cleared++;}});
  const work=mutation.portalFetch('portal/register',{method:'POST'});
  await flush();
  assert.equal(wait,45000);expire();
  await assert.rejects(work,/结果待确认.*不要重复提交.*没有自动重试/);
  assert.equal(calls,1);assert.equal(cleared,1);
});

test('valid read contracts preserve zero and null, but reject unsafe page and key shapes', async () => {
  const row = {ts: 1, model: 'synthetic-model', status: 200, prompt_tokens: null, completion_tokens: 0, latency_ms: null, credit: null};
  const good = api(async () => Response.json({page: 1, total: 1, items: [row]}));
  assert.equal((await good.portalFetch('portal/requests?page=1')).items[0].credit, null);
  for (const body of [{page: 1, total: 1, items: [null]}, {page: 1, total: -1, items: []}]) {
    await assert.rejects(api(async () => Response.json(body)).portalFetch('portal/requests?page=1'), /响应|数据/);
  }
  await assert.rejects(api(async () => Response.json({items: null, allowed_models: []})).portalFetch('portal/keys'), /响应|数据/);
});

test('workspace transport accepts only a complete overview bound to its authenticated owner', async () => {
  const data={owner_id:'A',scale:1000000,balance_units:0,account_count:0,active_account_count:0,
    expiring_7d_units:0,today_checkin_units:0,held_units:0,debt_units:0,callable_units:0,
    pending_pool_units:0,snapshot_at:1,dispatch_enabled:false,pool_enabled:false,dispatch_note:'fixture',
    allowed_models:[],accounts:[],grants:[],events:[]};
  const packet={me:{id:'A',username:'fixture-A'},data};
  assert.equal((await api(async()=>Response.json(packet)).portalFetch('portal/workspace')).data.balance_units,0);
  for(const body of [{...packet,data:{...data,owner_id:'B'}},{...packet,data:{...data,owner_id:null}},
    {...packet,me:{id:7,username:'fixture-A'}},{...packet,data:{...data,balance_units:null}}]){
    await assert.rejects(api(async()=>Response.json(body)).portalFetch('portal/workspace'),/响应|数据/);
  }
});
