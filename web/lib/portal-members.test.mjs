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
const {passwordCharacters, portalFieldForError, portalPasswordLengthProblem, portalUsernameProblem, filterPortalMembers} = result.exports;

test('portal character count matches backend without trimming or normalizing secrets', () => {
  for (const value of ['07392641', 'D7x!p9Q2', '  raw  8', '\u{1f680}'.repeat(7) + 'X', 'xy'.repeat(64)]) {
    assert.equal(portalPasswordLengthProblem(value), null);
  }
  assert.equal(passwordCharacters('\u{1f680}'.repeat(7)), 7);
  assert.equal(portalPasswordLengthProblem('\u{1f680}'.repeat(7)), 'portal.members.errPasswordShort');
  assert.equal(portalPasswordLengthProblem('x'.repeat(129)), 'portal.members.errPasswordLong');
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

test('server rejection reason decides which field is marked without translating the message', () => {
  // 后端只回一句中文原因、没有错误码，所以分派必须认中文。这条测试固定住
  // 「认的是关键词、不是整句措辞」——后端换句话只要还带关键词就仍然分派正确。
  assert.equal(portalFieldForError('密码过于常见，请换一个'), 'password');
  assert.equal(portalFieldForError('这个密码不符合要求'), 'password');
  assert.equal(portalFieldForError('用户名已存在'), 'username');
  assert.equal(portalFieldForError('服务器内部错误'), '');
  assert.equal(portalFieldForError(''), '');
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
