import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';
const code = ts.transpileModule(fs.readFileSync(new URL('./portal-api.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const output = {exports: {}};
vm.runInNewContext(code, {module: output, exports: output.exports,
  require: () => ({BASE_PATH: ''}), fetch: () => { throw new Error('no network in pure tests'); }});
const {points, checkinLabel, canCheckin} = output.exports;
test('portal displays only confirmed daily credit and blocks repeated effects', () => {
  assert.equal(points(100_000_000), '100');
  assert.equal(points(1), '0.000001');
  for (const state of ['running', 'reconciling', 'confirmed', 'confirmed_uncredited', 'unknown']) {
    assert.equal(canCheckin({status: 'active', initial_state: 'confirmed', checkin_state: state}), false);
  }
  assert.equal(canCheckin({status: 'active', initial_state: 'confirmed', checkin_state: null}), true);
  assert.equal(canCheckin({status: 'pending', initial_state: 'pending', checkin_state: null}), false);
  assert.match(checkinLabel('confirmed_uncredited'), /已签到/);
  assert.match(checkinLabel('unknown'), /待确认/);
});
test('portal fetch stays separate from manager session interceptors', () => {
  const source = fs.readFileSync(new URL('./portal-api.ts', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /from ['"]\.\/api/);
  assert.match(source, /credentials: 'same-origin'/);
  assert.match(source, /cache: 'no-store'/);
});
test('request history pages and password reset preserve visible security boundaries', () => {
  const portal = fs.readFileSync(new URL('../app/portal/page.tsx', import.meta.url), 'utf8');
  const page = fs.readFileSync(new URL('../app/(main)/members/page.tsx', import.meta.url), 'utf8');
  const reset = fs.readFileSync(new URL('../app/(main)/members/PasswordDialog.tsx', import.meta.url), 'utf8');
  const requests = fs.readFileSync(new URL('../app/portal/PortalRequests.tsx', import.meta.url), 'utf8');
  assert.match(portal, /tab === 'requests' && <PortalRequests key=\{me.id\} onUnauthorized=\{reset\}/);
  assert.match(requests, /portal\/requests\?page=\$\{page\}/);
  assert.match(requests, /请求记录分页/);
  assert.match(requests, /data\?\.page === page \? data : null/);
  assert.match(page, /PasswordDialog/);
  assert.match(reset, /旧会话和旧调用密钥/);
  assert.match(reset, /password === repeat/);
  assert.doesNotMatch(reset, /localStorage|sessionStorage|console\./);
});
