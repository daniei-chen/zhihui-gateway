const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const {normalizeStaticExport} = require('../scripts/normalize-static-export.cjs');

test('Windows RSC segment normalization preserves bytes and is idempotent', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'zhihui-rsc-test-'));
  const nested = path.join(root, 'settings/system/__next.!KG1haW4p/settings/system');
  fs.mkdirSync(nested, {recursive: true});
  const bytes = Buffer.from('1:["$","$L2",null,{"children":"fixture"}]\n');
  fs.writeFileSync(path.join(nested, '__PAGE__.txt'), bytes);
  fs.writeFileSync(path.join(root, 'index.html'), '<html></html>');
  const changes = normalizeStaticExport(root);
  assert.equal(changes.length, 1);
  assert.deepEqual(fs.readFileSync(path.join(root, 'settings/system/__next.!KG1haW4p.settings.system.__PAGE__.txt')), bytes);
  assert.equal(fs.readFileSync(path.join(root, 'index.html'), 'utf8'), '<html></html>');
  assert.deepEqual(normalizeStaticExport(root), []);
  assert.equal(fs.existsSync(path.join(nested, '__PAGE__.txt')), false);
});

test('RSC segment normalization fails closed on different destination bytes', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'zhihui-rsc-test-'));
  const nested = path.join(root, '__next.group/page');
  fs.mkdirSync(nested, {recursive: true});
  fs.writeFileSync(path.join(nested, '__PAGE__.txt'), 'original');
  fs.writeFileSync(path.join(root, '__next.group.page.__PAGE__.txt'), 'collision');
  assert.throws(() => normalizeStaticExport(root), /collision/);
  assert.equal(fs.readFileSync(path.join(nested, '__PAGE__.txt'), 'utf8'), 'original');
});
