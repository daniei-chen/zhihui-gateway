/**
 * K38 扫描器自测：`design/web/scripts/a11y-scan.mjs`。
 *
 * 跑法（仓库根）：
 *   node --test design/web/scripts/*.test.mjs
 *
 * 为什么要测扫描器本身：一个「什么都报」的扫描器会被无视，一个「什么都放过」的
 * 扫描器会给人虚假安全感。这里用两个小夹具钉住「该报什么、不该报什么」。
 */
import {scanSource} from './a11y-scan.mjs';

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

const reasons = (src) => scanSource(src, 'fixture.tsx').map((f) => f.reason);

console.log('应当报出的：');
check(
  'icon-only button without name',
  reasons(`<Button onClick={x}><RefreshCw /></Button>`),
  ['icon-only-no-label'],
);
check(
  'self-closing trigger without name',
  reasons(`<SelectTrigger className="x" />`),
  ['self-closing-no-label'],
);
check(
  'multi-line icon-only button',
  reasons(`<Button\n  variant="outline"\n  onClick={go}\n>\n  <Trash2 />\n</Button>`),
  ['icon-only-no-label'],
);
check('anchor with only icon', reasons(`<a href="/x"><Home /></a>`), ['icon-only-no-label']);

console.log('不应误报的：');
check('button with text', reasons(`<Button>保存</Button>`), []);
check('button with aria-label', reasons(`<Button aria-label="刷新"><RefreshCw /></Button>`), []);
check('button with title', reasons(`<button title="关闭"><X /></button>`), []);
check('button with aria-labelledby', reasons(`<Button aria-labelledby="h1"><X /></Button>`), []);
check('button with expression text child', reasons(`<Button>{t('save')}</Button>`), []);
check(
  'switch named by <label htmlFor> is not a finding',
  reasons(
    `<label htmlFor="key-realm-unlimited"><Switch id="key-realm-unlimited" checked={on} />{t('x')}</label>`,
  ),
  [],
);
check(
  'switch with id but no label is still a finding',
  reasons(`<Switch id="orphan" checked={on} />`),
  ['self-closing-no-label'],
);
check('non-interactive custom component', reasons(`<CopyButton value={x} />`), []);
check('icon-only div is out of scope', reasons(`<div><RefreshCw /></div>`), []);

console.log('扫描真实组件树（只统计数量，不当作通过标准）：');
import {scanDir} from './a11y-scan.mjs';
import {resolve} from 'node:path';
const webRoot = resolve(process.cwd(), 'src/three-platform/web');
const findings = scanDir(webRoot);
console.log(`  web 树命中 ${findings.length} 处（详见 a11y-scan.mjs 的输出）`);
check('scanner returns array', Array.isArray(findings), true);

console.log(failed ? `\n${failed} failed / ${passed} passed` : `\nall passed（${passed} 条）`);
process.exit(failed ? 1 : 0);
