import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import {performance} from 'node:perf_hooks';

const require = createRequire(import.meta.url);
const ts = require('typescript');
const exports = {};
const compiled = ts.transpileModule(fs.readFileSync(new URL('./async-state.ts', import.meta.url), 'utf8'), {
  compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
}).outputText;
new Function('exports', 'require', compiled)(exports, require);
const {shareJson} = exports;

test('unchanged responses and rows retain identity without mutating either input', () => {
  const before = {rows: [{uid: 'a', credits: 0}, {uid: 'b', credits: null}], age: 10};
  assert.equal(shareJson(before, structuredClone(before)), before);
  const next = {...structuredClone(before), age: 11};
  const result = shareJson(before, next);
  assert.notEqual(result, before);
  assert.equal(result.rows, before.rows);
  assert.equal(result.rows[0].credits, 0);
  assert.equal(before.age, 10);
  assert.equal(next.age, 11);
});

test('changed values, removed fields and non-JSON objects are not hidden', () => {
  const before = {rows: [{id: 1}, {id: 2}], removed: true};
  assert.deepEqual(shareJson(before, {rows: [{id: 3}]}), {rows: [{id: 3}]});
  const date = new Date(0);
  assert.equal(shareJson(new Date(0), date), date);
  assert.deepEqual(shareJson({value: 0}, {value: null}), {value: null});
});

test('oversized arrays fall back to the new value instead of walking beyond the budget', () => {
  const before = Array.from({length: 100001}, () => 1);
  const next = before.slice();
  assert.equal(shareJson(before, next), next);
});

test('synthetic account scales preserve stable row references with a bounded traversal', () => {
  const measurements = [];
  for (const size of [100, 500, 2000]) {
    const before = {rows: Array.from({length: size}, (_, index) => ({uid: String(index), credits: index, healthy: true})), age: 1};
    const next = {...structuredClone(before), age: 2};
    const started = performance.now();
    const result = shareJson(before, next);
    measurements.push({size, milliseconds: +(performance.now() - started).toFixed(3)});
    assert.equal(result.rows, before.rows);
    assert.equal(result.age, 2);
  }
  console.log(JSON.stringify({synthetic_structural_sharing: measurements, inference_benchmark: false}));
});
