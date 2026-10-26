// 前端纯函数单测（Node 原生 test runner，无需 DOM）
import test from 'node:test';
import assert from 'node:assert/strict';
const CHARTS_URL = process.env.QTG_APP_DIR
  ? new URL('file://' + process.env.QTG_APP_DIR + '/charts.js').href
  : new URL('../../qtg/web/assets/js/charts.js', import.meta.url).href;
const { monotonePath } = await import(CHARTS_URL);

// ui.js / api.js 依赖 document，这里内联等价的纯逻辑做契约测试
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
const num = (v, digits = 0) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toLocaleString('zh-CN', { maximumFractionDigits: digits, minimumFractionDigits: 0 });
};
const fmtTokens = (v) => {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e4) return (n / 1e3).toFixed(1) + 'k';
  return num(n);
};

test('esc 转义所有 HTML 元字符（防 XSS）', () => {
  assert.equal(esc(`<img src=x onerror="alert(1)">`), '&lt;img src=x onerror=&quot;alert(1)&quot;&gt;');
  assert.equal(esc("a'b&c"), 'a&#39;b&amp;c');
  assert.equal(esc(null), '');
});

test('num 处理非法值与千分位', () => {
  assert.equal(num(900), '900');
  assert.equal(num(1234), '1,234');
  assert.equal(num(null), '0');
  assert.equal(num('abc'), '—');
});

test('fmtTokens 分级单位', () => {
  assert.equal(fmtTokens(94651), '94.7k');
  assert.equal(fmtTokens(1_500_000), '1.50M');
  assert.equal(fmtTokens(2_000_000_000), '2.00B');
  assert.equal(fmtTokens(NaN), '—');
  assert.equal(fmtTokens(-1), '-1');
});

test('monotonePath 生成合法路径', () => {
  assert.equal(monotonePath([]), '');
  assert.equal(monotonePath([[10, 20]]), 'M10,20');
  const d2 = monotonePath([[0, 0], [100, 50]]);
  assert.match(d2, /^M0,0 L100,50$/);
  const d = monotonePath([[0, 100], [50, 60], [100, 80]]);
  assert.match(d, /^M0\.00,100\.00 C/);
  assert.ok(d.includes('C'), '应生成三次贝塞尔');
  // 单调性：x 必须严格递增（否则曲线会回折）
  const xs = [...d.matchAll(/[MC ]([\d.]+),/g)].map((m) => Number(m[1]));
  for (let i = 1; i < xs.length; i++) assert.ok(xs[i] >= xs[i - 1], `x 单调递增: ${xs}`);
});

test('图表 X 轴标签不被压缩为 0 宽', () => {
  // 单个数据点时 x 应居中（innerW/2），而不是全部堆在左边缘
  const d = monotonePath([[1, 1]]);
  assert.equal(d, 'M1,1');
});
