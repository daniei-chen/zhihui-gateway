import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';

const code = ts.transpileModule(fs.readFileSync(new URL('./portal-members.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const result = {exports: {}};
vm.runInNewContext(code, {module: result, exports: result.exports});
const {passwordCharacters, portalPasswordLengthProblem, portalUsernameProblem, filterPortalMembers} = result.exports;

test('portal character count matches backend without trimming or normalizing secrets', () => {
  for (const value of ['07392641', 'D7x!p9Q2', '  raw  8', '\u{1f680}'.repeat(7) + 'X', 'xy'.repeat(64)]) {
    assert.equal(portalPasswordLengthProblem(value), null);
  }
  assert.equal(passwordCharacters('\u{1f680}'.repeat(7)), 7);
  assert.equal(portalPasswordLengthProblem('\u{1f680}'.repeat(7)), '用户登录密码至少 8 位');
  assert.equal(portalPasswordLengthProblem('x'.repeat(129)), '用户登录密码最多 128 位');
});

test('portal username checks match creation and search does not mutate the member list', () => {
  for (const name of ['alice', 'user_123', 'local-demo']) assert.equal(portalUsernameProblem(name), null);
  for (const name of ['al', 'alice ', '名字', 'x'.repeat(41)]) assert.notEqual(portalUsernameProblem(name), null);
  const users = [{username: 'Alice', enabled: 1}, {username: 'bob', enabled: 0}];
  assert.deepEqual(Array.from(filterPortalMembers(users, ' ali ', 'all')), [users[0]]);
  assert.deepEqual(Array.from(filterPortalMembers(users, '', 'disabled')), [users[1]]);
  assert.deepEqual(Array.from(filterPortalMembers(users, 'bob', 'enabled')), []);
  assert.equal(users.length, 2);
});

test('user management is primary and wallet exceptions load only in their tab', () => {
  const page = fs.readFileSync(new URL('../app/(main)/members/page.tsx', import.meta.url), 'utf8');
  assert.match(page, /useState\('users'\)/);
  assert.match(page, /tab === 'wallet' && <WalletOperations/);
  assert.match(page, /aria-label="搜索用户名"/);
  assert.match(page, /aria-label="筛选用户状态"/);
  assert.match(page, /generation === sequence.current/);
  assert.match(page, /<CreateMemberDialog/);
  assert.doesNotMatch(page, /本地候选|未切换线上/);
});
