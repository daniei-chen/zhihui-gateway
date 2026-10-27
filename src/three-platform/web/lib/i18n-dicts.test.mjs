/**
 * K37 测试：语言字典注册表（lib/i18n/dicts.ts）。
 *
 * 跑法：`cd src/three-platform/web && node --test lib/*.test.mjs`
 *
 * 为什么单独测：语言包从「五种静态 import」改成「源语言静态 + 其余按需」以后，
 * 界面在字典到达前必须**回退到源语言**而不是空白/键名；而回退规则写错时界面
 * 不会报错，只会显示一片中文键值或空白。这里把「查什么字典」这一层钉死。
 */
import {createDictRegistry, pickDict} from './i18n/dicts.ts';

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

console.log('注册与查找：');
{
  const reg = createDictRegistry();
  check('empty registry has nothing', [reg.size(), reg.has('en'), reg.get('en')], [0, false, null]);
  reg.register('zh-CN', {title: '标题'});
  reg.register('en', {title: 'Title'});
  check('registered lookups', [reg.has('zh-CN'), reg.has('en'), reg.has('ja')], [true, true, false]);
  check('get returns dict object', reg.get('en')?.title, 'Title');
  check('size counts registered locales', reg.size(), 2);
  reg.register('en', {title: 'Title v2'});
  check('re-register overwrites', [reg.get('en')?.title, reg.size()], ['Title v2', 2]);
  reg.register('ja', null); // 脏输入不应注册
  check('invalid dict ignored', reg.has('ja'), false);
  reg.clear();
  check('clear empties registry', reg.size(), 0);
}

console.log('回退规则（源语言兜底）：');
{
  const reg = createDictRegistry();
  reg.register('zh-CN', {title: '标题'});
  check('loaded locale wins', pickDict(reg, 'en', 'zh-CN')?.title, '标题');
  reg.register('en', {title: 'Title'});
  check('target locale used when loaded', pickDict(reg, 'en', 'zh-CN')?.title, 'Title');
  check('missing target falls back to source', pickDict(reg, 'ko', 'zh-CN')?.title, '标题');
  check('missing both returns undefined', pickDict(createDictRegistry(), 'ko', 'zh-CN'), undefined);
}

console.log('加载前后同键不同值（模拟「先回退、加载后切换」）：');
{
  const reg = createDictRegistry();
  reg.register('zh-CN', {playground: {stopped: '（已停止）'}});
  const before = pickDict(reg, 'en', 'zh-CN');
  check('before load: source language shown', before?.playground?.stopped, '（已停止）');
  reg.register('en', {playground: {stopped: '(stopped)'}});
  const after = pickDict(reg, 'en', 'zh-CN');
  check('after load: target language shown', after?.playground?.stopped, '(stopped)');
  check('source dict untouched', pickDict(reg, 'zh-CN', 'zh-CN')?.playground?.stopped, '（已停止）');
}

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
