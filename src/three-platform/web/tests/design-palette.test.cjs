const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const jsx = (type, props) => ({type, props});

test('console carries the homepage mint and apricot side atmosphere without tinting semantic chart colors',()=>{
  const css=read('app/brand.css');
  assert.match(css,/ellipse 34% 700px at 0 58%, #eaf7f3/);
  assert.match(css,/ellipse 34% 700px at 100% 53%, #fff2e5/);
  assert.match(css,/html:not\(\.dark\):has\(#main-content\) body/);
  assert.doesNotMatch(css,/filter:\s*(hue-rotate|sepia)/);
});

test('console logo retains the homepage exact gradient and unique SVG ids', () => {
  let id = 0;
  const mocks = {'react': {useId: () => `brand-${++id}`}, 'next/link': {default: 'link'},
    '@/lib/product': {PRODUCT_NAME: 'fixture'}, 'react/jsx-runtime': {jsx, jsxs: jsx}};
  const code = ts.transpileModule(read('components/common/layout/BrandHome.tsx'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const module = {exports: {}};
  vm.runInNewContext(code, {module, exports: module.exports, require: name => mocks[name]});
  const first = JSON.stringify(module.exports.BrandHome()), second = JSON.stringify(module.exports.BrandHome());
  assert.match(first, /#7391FA/); assert.match(first, /#4D6CEB/);
  assert.match(first, /url\(#brand-1\)/); assert.match(second, /url\(#brand-2\)/);
});

test('chart palette separates traffic, income, expense and failures without recoloring SVGs', () => {
  const css = read('app/brand.css');
  for (const value of ['--chart-1: #3683fc', '--chart-2: #159d8d', '--chart-expense: #cf8b32']) assert.ok(css.includes(value));
  const charts = read('components/dashboard/DashboardCharts.tsx');
  assert.match(charts, /name=\{expenseLabel\} stroke="var\(--chart-expense\)"/);
  assert.match(charts, /name=\{failedLabel\}[\s\S]*?stroke="var\(--destructive\)"/);
  assert.doesNotMatch(css, /\.recharts[^{}]*path[^{}]*\{[^}]*stroke:/);
});

test('Trae presents the official HTTPS link to copy, not a fabricated working callback', () => {
  const source = read('components/common/accounts/AddAccountDialog.tsx');
  assert.match(source, /CopyButton value=\{authUrl\}/);
  assert.match(source, /addAccount.copyOfficialLink/);
  assert.doesNotMatch(source, /trae-callback-test|placeholder="http:\/\/127/);
  for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
    const copy = JSON.parse(read(`lib/i18n/locales/${locale}.json`)).addAccount;
    for (const key of ['copyOfficialLink', 'callbackPlaceholder', 'callbackHint']) assert.ok(copy[key]?.trim(), locale + ':' + key);
  }
  assert.match(JSON.parse(read('lib/i18n/locales/zh-CN.json')).addAccount.callbackHint, /不是原始入口链接/);
});
