const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

test('account administrators retain full navigation and never get forced into intake', () => {
  const layout = read('app/(main)/layout.tsx');
  assert.doesNotMatch(layout, /atIntake|limited|replace\('\/accounts'\)/);
  assert.match(layout, /!loading && me && <MemoizedManagementBar/);
  assert.match(layout, /!loading && me && <CommandPalette/);
  assert.match(layout, /!loading && me \? children : null/);
  const accounts = read('app/(main)/accounts/page.tsx');
  assert.doesNotMatch(accounts, /AccountIntake/);
  assert.match(accounts, /canAddAccounts && \([\s\S]*?setAddOpen\(true\)/);
  assert.match(accounts, /isAdmin && renderActions/);
  const dock = read('components/common/layout/ManagementBar.tsx');
  assert.match(dock, /aria-disabled=\{!canAddAccounts\}/);
  assert.match(read('app/(auth)/login/page.tsx'), /router\.replace\('\/dashboard'\)/);
});

test('settings and task pages show actual read data while writes remain disabled', () => {
  const tokens = read('components/common/settings/TokensPanel.tsx');
  assert.doesNotMatch(tokens, /if \(!isAdmin\)|if \(isAdmin\) void load/);
  assert.match(tokens, /isAdmin && <TableCell/);
  for (const file of ['components/common/settings/ProxyPanel.tsx',
    'components/common/accounts/RefreshPlanCard.tsx', 'components/common/tasks/TaskRunnerPanel.tsx']) {
    assert.match(read(file), /<fieldset disabled=\{!isAdmin\}/, file);
  }
  const tasks = read('app/(main)/tasks/page.tsx');
  assert.match(tasks, /const planCard = <RefreshPlanCard/);
  assert.doesNotMatch(tasks, /const runner = isAdmin/);
});

test('role descriptions state visibility separately from action authority in all languages', () => {
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const data = JSON.parse(read(`lib/i18n/locales/${locale}.json`));
    assert.ok(data.settings.usersNewDesc.length > 25, locale);
    assert.ok(data.intake.permission.length > 20, locale);
  }
  const zh = JSON.parse(read('lib/i18n/locales/zh-CN.json'));
  assert.match(zh.settings.usersNewDesc, /查看全部页面/);
  assert.doesNotMatch(zh.settings.usersNewDesc, /不能查看/);
});
