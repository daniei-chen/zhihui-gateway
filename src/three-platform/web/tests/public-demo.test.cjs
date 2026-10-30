const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const ts = require('typescript');
const filename = path.join(__dirname, '../lib/public-demo.ts');
const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const result = {exports: {}};
new Function('exports', 'require', compiled)(result.exports, () => {throw Error('Unexpected runtime import');});
const adapter = result.exports.publicDemoAdapter;
const config = (url, method = 'get') => ({url, method, headers: {get: () => 'workbuddy'}});

test('public demo renders finite synthetic data entirely inside the client', async () => {
  const me = await adapter(config('/api/me'));
  assert.equal(me.data.role, 'viewer');
  const data = await adapter(config('/api/stats/daily'));
  assert.equal(data.data.length, 14);
  assert.ok(data.data.every(row => Number.isFinite(row.requests)));
  assert.doesNotMatch(fs.readFileSync(filename, 'utf8'), /\b(fetch|XMLHttpRequest|WebSocket|sendBeacon)\s*\(/);
  const api = fs.readFileSync(path.join(__dirname, '../lib/api.ts'), 'utf8');
  assert.match(api, /process\.env\.NEXT_PUBLIC_ZHIHUI_DEMO === '1' \? \{adapter: publicDemoAdapter\}/);
});

test('public demo denies every write and unknown route without network fallback', async () => {
  for (const method of ['post', 'put', 'patch', 'delete']) {
    await assert.rejects(adapter(config('/api/accounts', method)), e => e.response.status === 403);
  }
  await assert.rejects(adapter(config('/api/unknown-future-path')), e => e.response.status === 403);
});
