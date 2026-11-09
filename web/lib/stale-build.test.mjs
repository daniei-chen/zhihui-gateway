/**
 * K33（前端侧）测试：旧构建/旧资源错误的识别与自动刷新防环（lib/stale-build.ts）。
 *
 * 跑法：`cd src/three-platform/web && node --test lib/*.test.mjs`
 *
 * 两条都不能错：
 *   · 误判 → 普通渲染异常也去刷新，用户的表单/对话被清掉，问题还在；
 *   · 漏判 → 跨发布后用户卡在「点了没反应」，只能自己摸索刷新。
 * 自动刷新本身还必须是**有界**的：存储不可写时坚决不自动刷新。
 */
import {
  createReloadGuard,
  isStaleBundleError,
} from './stale-build.ts';

let failed = 0;
let passed = 0;

function check(name, got, want) {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g === w) {
    passed += 1;
    return;
  }
  failed += 1;
  console.log(`  FAIL ${name}\n       got  ${g}\n       want ${w}`);
}

function fakeStorage({readOnly = false} = {}) {
  const data = new Map();
  return {
    data,
    getItem: (k) => (data.has(k) ? data.get(k) : null),
    setItem: (k, v) => {
      if (readOnly) throw new Error('read-only');
      data.set(k, v);
    },
    removeItem: (k) => {
      if (readOnly) throw new Error('read-only');
      data.delete(k);
    },
  };
}

console.log('错误识别：');
const staleMessages = [
  {name: 'ChunkLoadError', message: 'Loading chunk 1234 failed.'},
  {name: 'Error', message: 'Loading chunk app/dashboard/page failed.'},
  {name: 'TypeError', message: 'Failed to fetch dynamically imported module: /_next/static/chunks/x.js'},
  {name: 'TypeError', message: 'Importing a module script failed.'},
  {name: 'Error', message: 'Failed to fetch RSC payload for https://example.test/dashboard.'},
  {name: 'SyntaxError', message: "Unexpected token '<', \"<!DOCTYPE\"... is not valid JSON"},
  {name: 'Error', message: 'Unable to load chunk'},
];
for (const err of staleMessages) {
  check(`stale: ${err.message.slice(0, 48)}`, isStaleBundleError(err), true);
}
{
  // 混在多层 cause 里也要能认出来
  check('stale: nested cause', isStaleBundleError({message: 'x', cause: 'ChunkLoadError'}), true);
  // 字符串形态（有的实现只给 message）
  check('stale: plain string', isStaleBundleError('Loading chunk main failed'), true);
}

const normalMessages = [
  {name: 'TypeError', message: "Cannot read properties of undefined (reading 'map')"},
  {name: 'AxiosError', message: 'Request failed with status code 500'},
  {name: 'Error', message: 'Failed to fetch'},
  {name: 'AbortError', message: 'The operation was aborted.'},
  null,
  undefined,
  {},
];
for (const err of normalMessages) {
  check(`not stale: ${JSON.stringify(err)?.slice(0, 40)}`, isStaleBundleError(err), false);
}

console.log('自动刷新防环：');
{
  const storage = fakeStorage();
  const guard = createReloadGuard(storage, {windowMs: 10_000, max: 1});
  const t0 = 1_000_000;
  check('first failure may auto reload', guard.shouldAutoReload(t0), true);
  guard.markAutoReload(t0);
  check('second failure within window must not auto reload', guard.shouldAutoReload(t0 + 5_000), false);
  check('record written', guard.record()?.count, 1);
  check('after window expires auto reload is allowed again', guard.shouldAutoReload(t0 + 20_000), true);
}
{
  // 真·死循环防线：存储只读（写入必然失败）时**一次都不自动刷新**
  const storage = fakeStorage({readOnly: true});
  const guard = createReloadGuard(storage);
  check('read-only storage: never auto reload', guard.shouldAutoReload(1), false);
  guard.markAutoReload(1); // 不能抛
  check('read-only storage: still refuses afterwards', guard.shouldAutoReload(2), false);
}
{
  // 完全没有 storage（SSR/隐私模式把 sessionStorage 拿掉）
  const guard = createReloadGuard(null);
  check('no storage: never auto reload', guard.shouldAutoReload(1), false);
}
{
  // 损坏的记录：当成没有记录（可以刷一次），但不能抛
  const storage = fakeStorage();
  storage.setItem('wb.stale-build-reload', '{not json');
  const guard = createReloadGuard(storage);
  check('corrupt record treated as absent', guard.shouldAutoReload(1), true);
  guard.markAutoReload(1);
  check('corrupt record replaced with valid one', guardsRecord(guard), {ts: 1, count: 1});
}
{
  // clear 之后又可以自动刷新（用户手动刷新/确认新构建可用时调用）
  const storage = fakeStorage();
  const guard = createReloadGuard(storage);
  guard.markAutoReload(100);
  check('blocked before clear', guard.shouldAutoReload(200), false);
  guard.clear();
  check('allowed after clear', guard.shouldAutoReload(300), true);
}

function guardsRecord(guard) {
  const rec = guard.record();
  return rec && {ts: rec.ts, count: rec.count};
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
