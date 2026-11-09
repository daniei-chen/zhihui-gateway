const assert = require('node:assert/strict');
const path = require('node:path');

exports.run = async function(context, page, origin, output, controls) {
  const result = {scope: 'Synthetic local APIs, not provider or production acceptance', restricted: [], quotas: [], users: []};
  controls.setRole('account_admin');
  await context.addInitScript(() => {
    localStorage.setItem('workbuddy-manager:locale', 'zh-CN');
    localStorage.setItem('qtg.platform', 'workbuddy');
    localStorage.setItem('wb.realm', 'cn');
  });
  for (const target of ['/dashboard', '/keys', '/settings/users', '/stats', '/accounts']) {
    const start = controls.trace.length;
    await page.goto(origin + target, {waitUntil: 'networkidle'});
    assert.equal(new URL(page.url()).pathname.replace(/\/+$/, ''), target);
    assert.equal(await page.locator('[data-slot=account-intake]').count(), 0);
    assert.equal(await page.locator('[data-slot=dock]').count(), 1);
    assert.equal(await page.locator('[data-slot=dock-quick-add]').getAttribute('aria-disabled'), 'false');
    if (target === '/accounts') {
      assert.equal(await page.getByRole('button', {name: '添加账号', exact: true}).first().isVisible(), true);
      assert.equal(await page.getByRole('button', {name: '一键刷新令牌', exact: true}).count(), 0);
    }
    const requests = controls.trace.slice(start).map(row => row.path);
    assert.ok(requests.includes('/api/me'));
    assert.ok(requests.some(item => item !== '/api/me'), 'Read data must load instead of a blank intake shell');
    result.restricted.push({target, final: new URL(page.url()).pathname, requests});
  }
  await page.setViewportSize({width: 390, height: 844});
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.screenshot({path: path.join(output, 'account-admin-mobile.png'), fullPage: true, animations: 'disabled'});
  await page.locator('[data-slot=dock-profile-trigger]').click();
  await page.getByRole('button', {name: '退出登录', exact: true}).click();
  await page.getByRole('alertdialog').getByRole('button', {name: '退出登录', exact: true}).click();
  await page.getByText(/Simulated logout failure; session remains active/).waitFor();
  assert.match(page.url(), /\/accounts/);
  controls.setLogout('success');
  await page.getByRole('button', {name: '退出登录', exact: true}).click();
  await page.getByRole('alertdialog').getByRole('button', {name: '退出登录', exact: true}).click();
  await page.waitForURL(/\/login\/?$/);
  controls.authenticate();
  controls.setRole('admin');
  const userWrites = [];
  await page.route(origin + '/api/users**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (request.method() === 'GET' && url.pathname === '/api/users') {
      return route.fulfill({json: [{username: 'admin', role: 'admin'}, {username: 'alice', role: 'account_admin'}]});
    }
    if (['POST', 'PATCH'].includes(request.method())) {
      userWrites.push({method: request.method(), path: url.pathname, payload: request.postDataJSON()});
      return route.fulfill({json: {ok: true, relogin_required: false}});
    }
    return route.continue();
  });
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(origin + '/settings/users', {waitUntil: 'networkidle'});
  const rootRow = page.getByRole('row').filter({has: page.getByRole('cell', {name: 'admin', exact: true})});
  assert.equal(await rootRow.getByRole('combobox').count(), 0);
  assert.equal(await rootRow.getByText('超级管理员', {exact: true}).isVisible(), true);
  const role = page.getByRole('combobox', {name: '权限', exact: true}).first();
  await role.click();
  assert.deepEqual(await page.getByRole('option').allTextContents(), ['管理员', '超级管理员']);
  await page.getByRole('option', {name: '超级管理员', exact: true}).click();
  const password = page.getByLabel('密码', {exact: true});
  assert.equal(await password.getAttribute('inputmode'), null);
  assert.equal(await password.getAttribute('pattern'), null);
  assert.equal(await password.getAttribute('maxlength'), null, 'Do not silently truncate pasted passwords');
  await page.getByLabel('用户名', {exact: true}).fill('synthetic-new-admin');
  const create = page.getByRole('button', {name: '创建', exact: true});
  for (const value of ['1234567', 'letters', '\u{1f600}'.repeat(7)]) {
    await password.fill(value);
    assert.equal(await password.inputValue(), value);
    assert.equal(await create.isDisabled(), true);
  }
  for (const value of ['123456789', '1234567890', '12345678901', 'long-mixed-password',
    '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b', '\u{1f600}'.repeat(8)]) {
    await password.fill(value);
    assert.equal(await password.inputValue(), value);
    assert.equal(await create.isEnabled(), true);
  }
  await password.fill('mixed123');
  assert.equal(await create.isEnabled(), true);
  await page.screenshot({path: path.join(output, 'settings-user-min-eight-password.png'), fullPage: true, animations: 'disabled'});
  await create.click();
  await page.getByText('用户已创建', {exact: true}).waitFor();
  assert.deepEqual(userWrites[0], {method: 'POST', path: '/api/users',
    payload: {username: 'synthetic-new-admin', password: 'mixed123', role: 'admin'}});
  const aliceRow = page.getByRole('row').filter({has: page.getByRole('cell', {name: 'alice', exact: true})});
  await aliceRow.getByRole('combobox', {name: '权限', exact: true}).click();
  assert.deepEqual(await page.getByRole('option').allTextContents(), ['管理员', '超级管理员']);
  await page.getByRole('option', {name: '超级管理员', exact: true}).click();
  await page.waitForTimeout(100);
  assert.deepEqual(userWrites[1], {method: 'PATCH', path: '/api/users/alice', payload: {role: 'admin'}});
  await aliceRow.getByRole('button', {name: '重置密码', exact: true}).click();
  const reset = page.getByRole('alertdialog');
  const resetPassword = reset.getByLabel('密码（至少 8 位）', {exact: true});
  const confirmReset = reset.getByRole('button', {name: '重置密码', exact: true});
  assert.equal(await resetPassword.getAttribute('inputmode'), null);
  assert.equal(await resetPassword.getAttribute('pattern'), null);
  for (const value of ['1234567', 'letters', '\u{1f600}'.repeat(7)]) {
    await resetPassword.fill(value);
    assert.equal(await resetPassword.inputValue(), value);
    assert.equal(await confirmReset.isDisabled(), true);
  }
  for (const value of ['123456789', '1234567890', '12345678901', 'long-mixed-password',
    '\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03\u516b', '\u{1f600}'.repeat(8)]) {
    await resetPassword.fill(value);
    assert.equal(await resetPassword.inputValue(), value);
    assert.equal(await confirmReset.isEnabled(), true);
  }
  await resetPassword.fill('mixed123');
  assert.equal(await confirmReset.isEnabled(), true);
  await confirmReset.click();
  await reset.waitFor({state: 'hidden'});
  assert.deepEqual(userWrites[2], {method: 'PATCH', path: '/api/users/alice', payload: {password: 'mixed123'}});
  await rootRow.getByRole('button', {name: '重置密码', exact: true}).click();
  const rootPassword = reset.getByLabel('新密码（至少 12 位）', {exact: true});
  assert.equal(await rootPassword.inputValue(), '');
  assert.equal(await rootPassword.getAttribute('inputmode'), null);
  assert.equal(await rootPassword.getAttribute('pattern'), null);
  await rootPassword.fill('0123456789');
  assert.equal(await confirmReset.isDisabled(), true);
  await rootPassword.fill('Root-Password!');
  assert.equal(await confirmReset.isEnabled(), true);
  await confirmReset.click();
  await reset.waitFor({state: 'hidden'});
  assert.deepEqual(userWrites[3], {method: 'PATCH', path: '/api/users/admin', payload: {password: 'Root-Password!'}});
  result.users.push({roles: ['account_admin', 'admin'], rootRoleEditable: false,
    managedPasswordMinimum: 8, acceptsUnicodeAndMixed: true, rootResetMinimum: 12, syntheticWrites: userWrites.length});
  await page.unroute(origin + '/api/users**');
  let written;
  await page.route(origin + '/api/keys*', async route => {
    const request = route.request();
    if (new URL(request.url()).pathname !== '/api/keys') return route.continue();
    if (request.method() === 'POST') {
      written = request.postDataJSON();
      return route.fulfill({json: {id: 1, ...written, prefix: 'synthetic', key: 'synthetic-display-only'}});
    }
    return route.fulfill({json: []});
  });
  await page.setViewportSize({width: 1440, height: 1000});
  await page.goto(origin + '/keys', {waitUntil: 'networkidle'});
  await page.getByRole('button', {name: '新建密钥', exact: true}).first().click();
  await page.locator('#daily-tokens').fill('12500');
  await page.locator('#daily-credit').fill('3.25');
  await page.locator('#key-assigned-to').click();
  await page.getByRole('option', {name: 'alice', exact: true}).click();
  const dialog = page.getByRole('dialog');
  const name = dialog.locator('input').first();
  await name.fill('Synthetic allocation');
  await page.screenshot({path: path.join(output, 'key-daily-allocation-desktop.png'), fullPage: true, animations: 'disabled'});
  await dialog.getByRole('button', {name: '创建', exact: true}).click();
  await page.getByText('synthetic-display-only', {exact: true}).waitFor();
  assert.equal(written.daily_token_quota, 12500);
  assert.equal(written.daily_credit_quota, 3.25);
  assert.equal(written.assigned_to, 'alice');
  result.quotas.push({payload: written, plaintext: 'synthetic only'});
  await page.unroute(origin + '/api/keys*');
  return result;
};
