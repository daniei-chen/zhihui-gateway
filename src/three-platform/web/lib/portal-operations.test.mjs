import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import ts from 'typescript';
import vm from 'node:vm';
const code = ts.transpileModule(fs.readFileSync(new URL('./portal-operations.ts', import.meta.url), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const output = {exports: {}};
vm.runInNewContext(code, {module: output, exports: output.exports});
const {exactCredit, creditInputUnits, operationLabel} = output.exports;
test('operator amounts preserve six decimal places without rounding away a debt', () => {
  for (const units of [0, 1, 1234567, 999999999999999, 1000000000000000]) {
    assert.equal(creditInputUnits(exactCredit(units)), units);
  }
  assert.equal(exactCredit(1), '0.000001');
  for (const invalid of ['', '-1', '1e3', ' 1', '0.0000001', 'Infinity', 'NaN', '01', '1000000001']) {
    assert.equal(creditInputUnits(invalid), null, invalid);
  }
  assert.throws(() => exactCredit(1.5));
});
test('debt waiver is distinct from adding credits and requests keep explicit confirmation', () => {
  assert.equal(operationLabel('waive'), '管理员豁免');
  const source = fs.readFileSync(new URL('../app/(main)/members/WalletOperations.tsx', import.meta.url), 'utf8');
  assert.match(source, /crypto.randomUUID/);
  assert.match(source, /expected_units: target.row.amount_units/);
  assert.match(source, /setSubmitted\(operation\)/);
  assert.match(source, /can_resolve/);
  assert.match(source, /我已核对用户、金额和凭证/);
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});
test('admin bonus requires exact root identity and retry-stable actual grants, not credit lines', () => {
  assert.equal(operationLabel('bonus'), '管理员赠送积分');
  const page = fs.readFileSync(new URL('../app/(main)/members/page.tsx', import.meta.url), 'utf8');
  const dialog = fs.readFileSync(new URL('../app/(main)/members/BonusDialog.tsx', import.meta.url), 'utf8');
  const portal = fs.readFileSync(new URL('../app/portal/page.tsx', import.meta.url), 'utf8');
  assert.match(page, /isAdmin && me\?\.username === 'admin'/);
  assert.match(dialog, /crypto.randomUUID/);
  assert.match(dialog, /const body = submitted \|\|/);
  assert.match(dialog, /有效天数（默认30天）/);
  assert.match(portal, /额度已用尽，请联系管理员赠送额度/);
  assert.match(portal, /admin_bonus: '管理员赠送积分'/);
  assert.doesNotMatch(dialog, /localStorage|sessionStorage/);
});
test('dispatch hold and request-end proof are displayed without a force-unlock affordance', () => {
  const page = fs.readFileSync(new URL('../app/portal/page.tsx', import.meta.url), 'utf8');
  const operations = fs.readFileSync(new URL('../app/(main)/members/WalletOperations.tsx', import.meta.url), 'utf8');
  assert.match(page, /!data.dispatch_enabled/);
  assert.match(page, /data.dispatch_note/);
  assert.match(page, /!data.pool_enabled/);
  assert.match(operations, /row.end_confirmed/);
  assert.match(operations, /不代表费用为0/);
  assert.match(operations, /没有强制解锁入口/);
});
