const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');

test('new user controls share a row, with password guidance outside the control grid', () => {
  const source = read('app/(main)/settings/layout.tsx');
  const start = source.indexOf('data-slot="new-user-form"');
  assert.ok(start > 0);
  const hint = source.indexOf('<p id="new-user-password-hint"', start);
  assert.ok(hint > start);
  const grid = source.slice(start, hint);
  assert.match(grid, /grid-cols-1 items-end gap-3 sm:grid-cols-4/);
  assert.match(grid, /<SelectTrigger className="w-full bg-background"/);
  assert.match(grid, /className="h-9 rounded-full"/);
  assert.match(grid, /aria-describedby="new-user-password-hint"/);
  assert.doesNotMatch(grid, /<p\b/);
  assert.match(source.slice(hint), /settings\.managedPasswordHint/);
});

test('workbench uses near-white surfaces without fading readable text or semantic errors', () => {
  const css = read('app/brand.css');
  assert.match(css, /--zh-paper: #fdfefe/);
  assert.match(css, /--muted: #fafcff/);
  assert.match(css, /--accent: #f4f8fd/);
  assert.match(css, /--zh-action: #4d6b91/);
  assert.match(css, /--muted-foreground: #687285/);
  assert.match(css, /html:not\(\.dark\):has\(#main-content\) body/);
  assert.match(css, /prefers-reduced-motion: reduce/);
  assert.doesNotMatch(css, /--destructive:/);
  assert.match(css, /outline: 2px solid #7a9cbe/);
});
