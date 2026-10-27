#!/usr/bin/env node
// K38 补测与修复验证（R2-B）：对话框焦点陷阱/ESC、表格语义、对比度数值、CPU 节流。
//
// 两种模式：
//   1) 静态模式（不需要浏览器 fixture，现在就能跑）：
//        node tests/browser/k38_a11y_final.mjs --static-only
//      做三件事：
//        · 从 app/globals.css 解析 oklch/rgb 设计令牌，按 CSS Color 4 的
//          OKLab→linear sRGB 公式算 WCAG 2.x 相对亮度与对比度，逐对判 4.5/3；
//        · 静态核对表格语义（真实 <table>、TableHead 是否带 scope、是否有排序头）；
//        · 静态核对对话框组件是否依赖 Radix 的默认焦点管理（有没有被覆盖掉）。
//      产出 evidence/r2-k38/{contrast.json,contrast-table.md,static-a11y.json}。
//
//   2) 浏览器模式（真实 Chromium + 本地 disposable fixture 127.0.0.1:18443）：
//        node tests/browser/k38_a11y_final.mjs <fixture.json>
//      fixture JSON 由 tests/browser/run_local_fixture.ps1 写出
//      （evidence/browser-ready-*.json）。`makeHarness` 会强制校验
//      production_connected===false 且 host 必须是 127.0.0.1:18443，非本地直接抛错。
//      浏览器部分：
//        · 对话框：Tab 正向/反向循环不逃逸、ESC 关闭、关闭后焦点回到触发按钮、
//          打开时焦点落在对话框内（非 body）；
//        · 表格：真实 <table>、th scope、表头与数据列对齐、按钮可访问名称、
//          排序头 aria-sort（无排序头则记 N/A 并给出证据）；
//        · 对比度：从运行中的页面读 computed style（证明构建产物与源令牌一致），
//          与静态表交叉核对；
//        · CPU 节流：CDP Emulation.setCPUThrottlingRate 1x/4x/6x 各测一次首屏
//          load 与网络静默时间，并统计 >1s 的长任务。
//
// 明确不做（不是"通过"）：真实屏幕阅读器实播（NVDA/VoiceOver）无法自动化，
// 报告里标为 uncovered，只做可自动验证的语义断言。
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';

const REPO = path.resolve('.');
const WEB = path.join(REPO, 'src/three-platform/web');
const GLOBALS = path.join(WEB, 'app/globals.css');
const OUT_DIR = path.join(REPO, 'evidence/r2-k38');
const STATIC_ONLY = process.argv.includes('--static-only');
const argvFixture = process.argv.slice(2).find((a) => !a.startsWith('--'));
const compareIdx = process.argv.findIndex((a) => a.startsWith('--compare='));
const COMPARE = compareIdx >= 0 ? path.resolve(process.argv[compareIdx].split('=')[1]) : null;

fs.mkdirSync(OUT_DIR, {recursive: true});

// ─────────────────────────────── 颜色数学（CSS Color 4 + WCAG 2.x）─────────────
// OKLab → LMS → linear sRGB 用 CSS Color 4 的官方矩阵常量；WCAG 相对亮度直接
// 用 linear sRGB 加权（两者定义一致的线性空间，不再做 gamma 往返）。
function oklchToLinearSrgb(L, C, Hdeg, alpha = 1) {
  const h = (Hdeg * Math.PI) / 180;
  const a = C * Math.cos(h);
  const b = C * Math.sin(h);
  const l_ = L + 0.3963377774 * a + 0.2158037573 * b;
  const m_ = L - 0.1055613458 * a - 0.0638541728 * b;
  const s_ = L - 0.0894841775 * a - 1.2914855480 * b;
  const l = l_ ** 3;
  const m = m_ ** 3;
  const s = s_ ** 3;
  return {
    r: 4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    g: -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    b: -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s,
    a: alpha,
  };
}

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const linToSrgb = (v) => (v <= 0.0031308 ? 12.92 * v : 1.055 * v ** (1 / 2.4) - 0.055);
const srgbToLin = (v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4);

function toSrgbTuple(c) {
  return {
    r: clamp01(linToSrgb(clamp01(c.r))),
    g: clamp01(linToSrgb(clamp01(c.g))),
    b: clamp01(linToSrgb(clamp01(c.b))),
    a: c.a === undefined ? 1 : c.a,
  };
}

// 颜色解析：支持 oklch(L C H [/ A])、rgb(r,g,b)、#hex、transparent。
function parseColor(input) {
  if (input === null || input === undefined) throw new Error(`无法解析颜色: ${input}`);
  const text = String(input).trim();
  if (/^transparent$/i.test(text)) return {r: 0, g: 0, b: 0, a: 0};
  let m = text.match(/^oklch\(\s*([0-9.]+%?)\s+([0-9.]+%?)\s+([0-9.]+)(?:deg)?\s*(?:\/\s*([0-9.]+%?))?\s*\)$/i);
  if (m) {
    const pct = (v) => (v.endsWith('%') ? parseFloat(v) / 100 : parseFloat(v));
    const L = pct(m[1]);
    const C = pct(m[2]);
    const alpha = m[4] === undefined ? 1 : pct(m[4]);
    return toSrgbTuple(oklchToLinearSrgb(L, C, parseFloat(m[3]), alpha));
  }
  m = text.match(/^rgba?\(\s*([0-9.]+)\s*,\s*([0-9.]+)\s*,\s*([0-9.]+)(?:\s*,\s*([0-9.]+))?\s*\)$/i);
  if (m) {
    return {r: +m[1] / 255, g: +m[2] / 255, b: +m[3] / 255,
            a: m[4] === undefined ? 1 : parseFloat(m[4])};
  }
  m = text.match(/^#([0-9a-f]{6})$/i);
  if (m) {
    return {r: parseInt(m[1].slice(0, 2), 16) / 255, g: parseInt(m[1].slice(2, 4), 16) / 255,
            b: parseInt(m[1].slice(4, 6), 16) / 255, a: 1};
  }
  m = text.match(/^#([0-9a-f]{3})$/i);
  if (m) {
    const eh = (c) => parseInt(c + c, 16) / 255;
    return {r: eh(m[1][0]), g: eh(m[1][1]), b: eh(m[1][2]), a: 1};
  }
  throw new Error(`无法解析颜色: ${text}`);
}

// sRGB 空间里按 alpha 合成（CSS 普通 alpha 合成发生在设备空间）。
function composite(fg, bg) {
  const a = fg.a === undefined ? 1 : fg.a;
  return {
    r: fg.r * a + bg.r * (1 - a),
    g: fg.g * a + bg.g * (1 - a),
    b: fg.b * a + bg.b * (1 - a),
    a: 1,
  };
}

function relativeLuminance(rgb) {
  const lin = (v) => srgbToLin(clamp01(v));
  return 0.2126 * lin(rgb.r) + 0.7152 * lin(rgb.g) + 0.0722 * lin(rgb.b);
}

function contrastRatio(fgRgb, bgRgb) {
  const l1 = relativeLuminance(fgRgb);
  const l2 = relativeLuminance(bgRgb);
  const hi = Math.max(l1, l2);
  const lo = Math.min(l1, l2);
  return (hi + 0.05) / (lo + 0.05);
}

const fmt = (v) => (Math.round(v * 100) / 100).toFixed(2);

// ─────────────────────────────── 令牌解析 ─────────────────────────────────────
function parseThemeBlocks(css) {
  const block = (selector) => {
    const i = css.indexOf(selector);
    if (i < 0) throw new Error(`globals.css 缺少 ${selector} 块`);
    const open = css.indexOf('{', i);
    let depth = 0;
    for (let j = open; j < css.length; j += 1) {
      if (css[j] === '{') depth += 1;
      else if (css[j] === '}') {
        depth -= 1;
        if (depth === 0) return css.slice(open + 1, j);
      }
    }
    throw new Error(`globals.css 的 ${selector} 块括号不闭合`);
  };
  const tokens = (text) => {
    const map = {};
    for (const line of text.split('\n')) {
      const m = line.match(/^\s*(--[a-z0-9-]+)\s*:\s*([^;]+);/i);
      if (m) map[m[1]] = m[2].trim();
    }
    return map;
  };
  return {light: tokens(block(':root {')), dark: tokens(block('.dark {'))};
}

function makeThemeResolver(tokens) {
  const cache = new Map();
  const resolve = (name, stack = []) => {
    if (cache.has(name)) return cache.get(name);
    const raw = tokens[name];
    if (raw === undefined) throw new Error(`令牌不存在: ${name}`);
    if (stack.includes(name)) throw new Error(`令牌循环引用: ${[...stack, name].join(' -> ')}`);
    let value;
    if (/^var\(/.test(raw)) {
      const m = raw.match(/^var\(\s*(--[a-z0-9-]+)\s*\)$/i);
      value = m ? resolve(m[1], [...stack, name]) : parseColor(raw);
    } else {
      value = parseColor(raw);
    }
    cache.set(name, value);
    return value;
  };
  return resolve;
}

// 需要合成的派生色（与组件里的写法一一对应）
function resolveColor(theme, spec) {
  const {resolve} = theme;
  if (typeof spec === 'string') {
    // '--token' 走令牌解析；'#ffffff' / 'rgb(...)' 这类字面量直接解析
    return spec.startsWith('--') ? resolve(spec) : parseColor(spec);
  }
  // {token, alpha, over} → token 以 alpha 叠在 over 上；alpha 缺省取 token 自身 alpha
  const base = spec.over ? resolveColor(theme, spec.over) : resolve('--background');
  const color = resolve(spec.token);
  const alpha = spec.alpha === undefined ? (color.a === undefined ? 1 : color.a) : spec.alpha;
  return composite({...color, a: alpha}, base);
}

// ─────────────────────────────── 对比度用例表 ─────────────────────────────────
// criterion: 4.5 正文 / 3 大文本或非文本 / 'decorative' 仅记录（WCAG 1.4.11 装饰豁免）
const PAIRS = [
  // ── 正文与次要文本（WCAG 1.4.3，正常字号 4.5:1）
  {id: 'body', label: '正文 body', fg: '--foreground', bg: '--background', criterion: 4.5,
   note: 'body { bg-background text-foreground }'},
  {id: 'card-text', label: '卡片正文', fg: '--card-foreground', bg: '--card', criterion: 4.5},
  {id: 'muted-on-bg', label: '次要文本（页面底色上）', fg: '--muted-foreground',
   bg: '--background', criterion: 4.5, note: '表头 11px、说明文字都用它，属正常字号'},
  {id: 'muted-on-muted', label: '次要文本（muted 底色上）', fg: '--muted-foreground',
   bg: '--muted', criterion: 4.5, note: 'Tabs/Badge/工具条上的次要文字'},
  {id: 'primary-btn', label: '主按钮文字', fg: '--primary-foreground', bg: '--primary',
   criterion: 4.5},
  {id: 'secondary-btn', label: '次要按钮文字', fg: '--secondary-foreground',
   bg: '--secondary', criterion: 4.5},
  {id: 'accent-text', label: 'accent 前景（悬停项）', fg: '--accent-foreground', bg: '--accent',
   criterion: 4.5},
  {id: 'sidebar-text', label: '侧栏文字', fg: '--sidebar-foreground', bg: '--sidebar',
   criterion: 4.5},
  {id: 'sidebar-accent', label: '侧栏选中项文字', fg: '--sidebar-accent-foreground',
   bg: '--sidebar-accent', criterion: 4.5},
  // ── 错误/成功/警示（文字判据）
  {id: 'destructive-text', label: '错误文字（text-destructive）', fg: '--destructive',
   bg: '--background', criterion: 4.5, note: '错误提示、破坏性操作文案'},
  {id: 'destructive-btn-light', label: '破坏性按钮文字（light：destructive 底 + 白字）',
   fg: '#ffffff', bg: '--destructive', criterion: 4.5, themes: ['light'],
   note: 'button.tsx / badge.tsx / ConfirmDialog 在 light 下都是完整 destructive 底'},
  {id: 'destructive-btn-dark', label: '破坏性按钮文字（dark：destructive/60 底 + 白字）',
   fg: '#ffffff', bg: {token: '--destructive', alpha: 0.6, over: '--background'},
   criterion: 4.5, themes: ['dark'], note: 'dark 下必须用 /60；用完整 destructive 底会掉到 2.9'},
  {id: 'destructive-hover-light', label: '破坏性按钮悬停态文字（light：/90 底）',
   fg: '#ffffff', bg: {token: '--destructive', alpha: 0.9, over: '--background'},
   criterion: 4.5, themes: ['light'], note: 'hover:bg-destructive/90'},
  {id: 'destructive-hover-dark', label: '破坏性按钮悬停态文字（dark：/50 底）',
   fg: '#ffffff', bg: {token: '--destructive', alpha: 0.5, over: '--background'},
   criterion: 4.5, themes: ['dark'],
   note: 'dark:hover:bg-destructive/50；若沿用 /90 会掉到 3.5（已实测）'},
  {id: 'chart-legend', label: '图表图例文字（次要文本）', fg: '--muted-foreground',
   bg: '--background', criterion: 4.5},
  // ── 大文本（WCAG 1.4.3 大字号 3:1）
  {id: 'heading-large', label: '大标题（≥24px 或 ≥18.66px 粗体）', fg: '--foreground',
   bg: '--background', criterion: 3, kind: 'large'},
  // ── 非文本：焦点指示器（WCAG 1.4.11 / 2.4.13，3:1）
  {id: 'ring-on-bg', label: '焦点环 vs 页面底色', fg: '--ring', bg: '--background',
   criterion: 3, kind: 'nontext'},
  {id: 'ring-on-card', label: '焦点环 vs 卡片底色', fg: '--ring', bg: '--card',
   criterion: 3, kind: 'nontext'},
  // ── 非文本：控件边界（输入框/描边按钮的可见边界，3:1）
  {id: 'control-border', label: '控件边界（--input）vs 页面底色', fg: '--input',
   bg: '--background', criterion: 3, kind: 'nontext',
   note: 'input.tsx 与 outline 按钮的可见边界'},
  {id: 'control-border-card', label: '控件边界（--input）vs 卡片底色', fg: '--input',
   bg: '--card', criterion: 3, kind: 'nontext'},
  {id: 'input-fill', label: '输入框填充 vs 页面底色（muted/55）', fg: {token: '--muted',
   alpha: 0.55, over: '--background'}, bg: '--background', criterion: 'info', kind: 'info',
   note: '仅记录：控件可辨识边界改由 --input 边框承担（1.4.11 的判据在 control-border）'},
  // ── 非文本：状态与图标（3:1）
  {id: 'destructive-icon', label: '破坏性图标（text-red-500/600 近似用 destructive）',
   fg: '--destructive', bg: '--background', criterion: 3, kind: 'nontext'},
  {id: 'switch-off', label: '开关未选中轨道 vs 底色', fg: '--input', bg: '--background',
   criterion: 3, kind: 'nontext', note: 'switch.tsx 未选中用 --input'},
  // ── 图表数据系列（图形对象，3:1）
  {id: 'chart-1', label: '图表色 1 vs 底色', fg: '--chart-1', bg: '--card',
   criterion: 3, kind: 'nontext'},
  {id: 'chart-2', label: '图表色 2 vs 底色', fg: '--chart-2', bg: '--card',
   criterion: 3, kind: 'nontext'},
  {id: 'chart-3', label: '图表色 3 vs 底色', fg: '--chart-3', bg: '--card',
   criterion: 3, kind: 'nontext'},
  {id: 'chart-4', label: '图表色 4 vs 底色', fg: '--chart-4', bg: '--card',
   criterion: 3, kind: 'nontext'},
  {id: 'chart-5', label: '图表色 5 vs 底色', fg: '--chart-5', bg: '--card',
   criterion: 3, kind: 'nontext'},
  // ── 装饰性（仅记录，不算失败：WCAG 1.4.11 对纯装饰/分隔线豁免）
  {id: 'border-decoration', label: '分隔线/卡片描边 vs 页面底色', fg: '--border',
   bg: '--background', criterion: 'decorative', kind: 'decorative',
   note: '表格行线、卡片描边属装饰；控件边界用 --input 判定'},
];

// 主题专属用例：dark/light 各自只算适用项
function pairsFor(themeName) {
  return PAIRS.filter((p) => !p.themes || p.themes.includes(themeName));
}

function computeContrast(themeName, tokens) {
  const theme = {tokens, resolve: makeThemeResolver(tokens)};
  const rows = [];
  for (const pair of pairsFor(themeName)) {
    const fg = resolveColor(theme, pair.fg);
    const bg = resolveColor(theme, pair.bg);
    const solidBg = composite(bg, theme.resolve('--background'));
    // 半透明前景（如 dark 的 --input = white/15%）必须先叠到底色上再算亮度，
    // 否则会把"几乎不可见的半透明白"当成纯白，得到假的 19:1。
    const solidFg = composite(fg, solidBg);
    const ratio = contrastRatio(solidFg, solidBg);
    const checkable = typeof pair.criterion === 'number';
    const pass = checkable ? ratio >= pair.criterion : null;
    rows.push({id: pair.id, label: pair.label, kind: pair.kind || 'text',
               criterion: pair.criterion, ratio: Math.round(ratio * 100) / 100, pass,
               note: pair.note || ''});
  }
  return rows;
}

function staticContrast() {
  const css = fs.readFileSync(GLOBALS, 'utf8');
  const {light, dark} = parseThemeBlocks(css);
  return {
    source: path.relative(REPO, GLOBALS).replaceAll('\\', '/'),
    light: computeContrast('light', light),
    dark: computeContrast('dark', dark),
  };
}

// ─────────────────────────────── 静态表格/对话框审计 ──────────────────────────
function walkFiles(dir, suffix, out = []) {
  for (const entry of fs.readdirSync(dir, {withFileTypes: true})) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules' || entry.name === '.next') continue;
      walkFiles(p, suffix, out);
    } else if (entry.name.endsWith(suffix)) out.push(p);
  }
  return out;
}

function staticA11yAudit() {
  const tableSrc = fs.readFileSync(path.join(WEB, 'components/ui/table.tsx'), 'utf8');
  const tableHeadScopeDefault = /scope=/.test(tableSrc);
  const pages = ['logs', 'keys', 'accounts', 'stats', 'models', 'security', 'tasks',
                 'red-packets'];
  const pageRows = [];
  for (const page of pages) {
    const file = path.join(WEB, 'app/(main)', page, 'page.tsx');
    if (!fs.existsSync(file)) continue;
    const src = fs.readFileSync(file, 'utf8');
    pageRows.push({
      page,
      nativeTable: /<Table[\s>]/.test(src),
      tableHeadUses: (src.match(/<TableHead[\s>]/g) || []).length,
      ariaSort: (src.match(/aria-sort/g) || []).length,
      roleTable: (src.match(/role=["']table["']/g) || []).length,
      gridCols: (src.match(/grid-cols/g) || []).length,
    });
  }
  const dialogFiles = [
    'components/animate-ui/radix/dialog.tsx',
    'components/ui/alert-dialog.tsx',
    'components/common/layout/ConfirmDialog.tsx',
  ];
  const dialogs = dialogFiles.map((rel) => {
    const src = fs.readFileSync(path.join(WEB, rel), 'utf8');
    return {
      file: rel,
      primitive: /@radix-ui\/react-(alert-)?dialog|from 'radix-ui'/.test(src)
        ? 'radix' : (/@\/components\/ui\/alert-dialog/.test(src)
          ? 'radix（经 components/ui/alert-dialog 包装）' : 'unknown'),
      modalFalse: /modal=\{false\}/.test(src),
      overridesEscape: /onEscapeKeyDown=/.test(src),
      overridesInitialFocus: /onOpenAutoFocus=/.test(src),
      overridesRestoreFocus: /onCloseAutoFocus=/.test(src),
      usesForceMount: /forceMount/.test(src),
    };
  });
  // 破坏性实心底在 dark 下必须改用 /60（完整底 + 白字只有 2.9:1）。
  // 这些组件的 className 都写在单行里，所以按行检查即可；命中且同行没有
  // dark:bg-destructive/N 或 dark:hover:bg-destructive/N 的记为违规。
  const destructiveSolid = [];
  const scanRoots = [path.join(WEB, 'app'), path.join(WEB, 'components')];
  for (const root of scanRoots) {
    for (const file of walkFiles(root, '.tsx')) {
      const rel = path.relative(WEB, file).replaceAll('\\', '/');
      fs.readFileSync(file, 'utf8').split('\n').forEach((line, idx) => {
        if (!/\bbg-destructive(?!\/)/.test(line)) return;
        if (/dark:(hover:)?bg-destructive\/\d+/.test(line)) return;
        destructiveSolid.push({file: rel, line: idx + 1,
                               snippet: line.trim().slice(0, 140)});
      });
    }
  }
  return {tableHeadScopeDefault, pages: pageRows, dialogs, destructiveSolid};
}

function writeContrastArtifacts(current, before) {
  const beforeMap = {light: {}, dark: {}};
  if (before) {
    for (const theme of ['light', 'dark']) {
      for (const row of before[theme] || []) beforeMap[theme][row.id] = row;
    }
  }
  const jsonPath = path.join(OUT_DIR, 'contrast.json');
  fs.writeFileSync(jsonPath, JSON.stringify(current, null, 2));

  const lines = [];
  lines.push('# K38 对比度数值表（oklch → sRGB → WCAG 相对亮度）');
  lines.push('');
  lines.push('> 生成方式：`node tests/browser/k38_a11y_final.mjs --static-only` 直接解析');
  lines.push('> `src/three-platform/web/app/globals.css` 的设计令牌，按 CSS Color 4 的');
  lines.push('> OKLab→linear sRGB 矩阵求线性 RGB，再用 WCAG 相对亮度 `(L1+0.05)/(L2+0.05)`');
  lines.push('> 计算；带 alpha 的令牌（`/ 10%`、`muted/55`）先在 sRGB 空间合成到底色再算。');
  lines.push('> 阈值：正文 4.5:1、大文本 3:1、非文本 UI（焦点环/控件边界/图标/图表系列）3:1。');
  lines.push('> 装饰性分隔线（表格行线、卡片描边）按 WCAG 1.4.11 的装饰豁免**只记录数值**，');
  lines.push('> 控件边界一律用 `--input` 判定。');
  lines.push('');
  for (const theme of ['light', 'dark']) {
    lines.push(`## ${theme === 'light' ? '浅色主题（:root）' : '深色主题（.dark）'}`);
    lines.push('');
    lines.push('| 组合 | 用途/判据 | 阈值 | 修复前 | 现值 | 判定 |');
    lines.push('|---|---|---|---|---|---|');
    for (const row of current[theme]) {
      const beforeRow = beforeMap[theme][row.id];
      const beforeText = beforeRow ? fmt(beforeRow.ratio) : '—';
      const verdict = row.pass === null
        ? (row.criterion === 'info' ? '记录（信息项）' : '记录（装饰豁免）')
        : (row.pass ? 'PASS' : '**FAIL**');
      const currentText = fmt(row.ratio);
      lines.push(`| ${row.label} | ${row.kind === 'text' ? '文本' : row.kind === 'large'
        ? '大文本' : row.kind === 'nontext' ? '非文本'
          : row.kind === 'info' ? '信息（只记录）' : '装饰（1.4.11 豁免）'}`
        + `${row.note ? '：' + row.note : ''}`
        + ` | ${row.criterion === 'decorative' || row.criterion === 'info'
          ? '—' : row.criterion + ':1'} | ${beforeText}`
        + ` | ${currentText} | ${verdict} |`);
    }
    lines.push('');
  }
  fs.writeFileSync(path.join(OUT_DIR, 'contrast-table.md'), lines.join('\n') + '\n');
  return jsonPath;
}

// ─────────────────────────────── 浏览器部分 ───────────────────────────────────
async function browserRun(fixturePath) {
  // 先自己校验一遍 fixture，再交给 makeHarness（双重守卫）：
  //   · 必须是 https://127.0.0.1:18443 的本地 disposable fixture；
  //   · production_connected 必须显式为 false；
  //   · source_sha256 必须是 64 位 hex（否则证据无法绑定源码快照，直接拒绝）。
  const raw = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
  let url;
  try {
    url = new URL(raw.host_url);
  } catch {
    throw new Error(`fixture.host_url 不是合法 URL: ${raw.host_url}`);
  }
  if (raw.production_connected !== false) {
    throw new Error('拒绝：fixture 未声明 production_connected=false');
  }
  if (url.protocol !== 'https:') {
    throw new Error(`拒绝：fixture 必须是 https（实际 ${url.protocol}）`);
  }
  if (url.hostname !== '127.0.0.1' || url.port !== '18443') {
    throw new Error(`拒绝非本地目标：${url.host}（只允许 127.0.0.1:18443）`);
  }
  if (!/^[0-9a-f]{64}$/.test(String(raw.source_sha256 || ''))) {
    throw new Error('拒绝：source_sha256 缺失或不是 64 位 hex，证据无法绑定源码快照');
  }
  const {makeHarness} = await import('./fixture_lib.mjs');
  const h = makeHarness(fixturePath, 'zhihui-k38-a11y/v1', 'k38-a11y-final');
  const shots = path.join(OUT_DIR, 'shots');
  fs.mkdirSync(shots, {recursive: true});
  const out = {schema: 'zhihui-k38-a11y/v1', source_sha256: null, production_connected: false,
               parts: {}, uncovered: [
                 '真实屏幕阅读器实播（NVDA / VoiceOver）无法自动化：只做可自动验证的语义断言',
                 '移动端真实设备（iOS Safari / Android TalkBack）未覆盖：用 360px 视口近似',
               ]};
  try {
    out.source_sha256 = h.report.source_sha256;
    const ctx = await h.newCtx();
    const page = await ctx.newPage();
    await h.login(page);

    // ── 1) 对话框：焦点陷阱 / ESC / 焦点恢复 ──────────────────────────────
    out.parts.dialog = await dialogChecks(page, shots, h);
    out.parts.table = await tableChecks(page, h);
    out.parts.contrast = await liveContrast(page);
    out.parts.cpu = await cpuThrottle(page);
    out.status = Object.values(out.parts).every((p) => p.status !== 'FAIL')
      ? 'completed' : 'completed_with_failures';
  } catch (err) {
    out.status = 'error';
    out.error = String(err && err.stack ? err.stack : err).slice(0, 2000);
    h.note('k38 error: ' + out.error);
  }
  fs.writeFileSync(path.join(OUT_DIR, 'report.json'),
                   JSON.stringify({...out, generated_at: new Date().toISOString()}, null, 2));
  await h.finish();
  return out;
}

async function dialogChecks(page, shots, h) {
  const result = {status: 'PASS', cases: {}, opened: null, trigger: null, failures: []};
  const candidates = ['/keys/', '/accounts/', '/red-packets/', '/tasks/', '/settings/'];
  let trigger = null;
  for (const route of candidates) {
    await page.goto(h.base.origin + route, {waitUntil: 'domcontentloaded'});
    await page.waitForTimeout(1500);
    const btn = page.locator('[aria-haspopup="dialog"]:visible').first();
    if (await btn.count()) {
      trigger = btn;
      result.opened = route;
      break;
    }
  }
  if (!trigger) {
    result.status = 'BLOCKED';
    result.failures.push('在候选页面里没有找到 aria-haspopup="dialog" 的触发按钮；'
      + '对话框未被真实触发，本位记为阻塞（待 fixture 数据补齐后重跑）');
    return result;
  }
  // 给触发按钮打标记，便于关闭后核对焦点是否回到它
  await trigger.evaluate((el) => {
    el.setAttribute('data-k38-trigger', '1');
  });
  const label = await trigger.evaluate((el) => ({
    tag: el.tagName,
    text: (el.textContent || '').trim().slice(0, 40),
    aria: el.getAttribute('aria-label') || el.getAttribute('title') || '',
  }));
  result.trigger = label;
  await trigger.click();
  const dialog = page.locator('[role="dialog"], [role="alertdialog"]').first();
  await dialog.waitFor({state: 'visible', timeout: 10000});
  await page.waitForTimeout(400);
  await page.screenshot({path: path.join(shots, 'dialog-open.png')});

  const activeInfo = async () => page.evaluate(() => {
    const el = document.activeElement;
    const dlg = document.querySelector('[role="dialog"], [role="alertdialog"]');
    return {
      tag: el ? el.tagName : null,
      inDialog: !!(el && dlg && (dlg === el || dlg.contains(el))),
      isBody: !el || el === document.body,
      text: el ? (el.textContent || '').trim().slice(0, 30) : '',
      name: el ? (el.getAttribute('aria-label') || el.getAttribute('title') || ''
                   || (el.textContent || '').trim().slice(0, 30)) : '',
    };
  });
  const set = (id, pass, detail) => {
    result.cases[id] = {pass, ...detail};
    if (!pass) { result.failures.push(id + ': ' + JSON.stringify(detail).slice(0, 300)); }
  };

  // ⑤ 打开时焦点在对话框内
  const openFocus = await activeInfo();
  set('focus_on_open_inside_dialog', openFocus.inDialog && !openFocus.isBody, openFocus);

  const focusableCount = await page.evaluate(() => {
    const dlg = document.querySelector('[role="dialog"], [role="alertdialog"]');
    if (!dlg) return 0;
    const sel = 'a[href],button:not([disabled]),input:not([disabled]),select,textarea,'
      + '[tabindex]:not([tabindex="-1"])';
    return [...dlg.querySelectorAll(sel)].filter((el) => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0;
    }).length;
  });
  result.focusable_count = focusableCount;

  // ① Tab 正向循环：按 focusable+2 次，全程不得逃出对话框，且至少回到过起点
  const seen = [];
  let escaped = false;
  let wrapped = false;
  for (let i = 0; i < focusableCount + 2; i += 1) {
    await page.keyboard.press('Tab');
    const info = await activeInfo();
    if (!info.inDialog) { escaped = true; seen.push({escaped_to: info.tag, text: info.text}); break; }
    if (seen.some((s) => s.name && s.name === info.name) && info.name) wrapped = true;
    seen.push({name: info.name});
  }
  set('tab_forward_trapped', !escaped, {focusableCount, escaped, seen: seen.slice(0, 8)});
  set('tab_forward_wraps', wrapped || focusableCount <= 1,
      {wrapped, focusableCount});

  // ② Shift+Tab 反向：3 次，不得逃出
  let reverseEscaped = false;
  for (let i = 0; i < 3; i += 1) {
    await page.keyboard.press('Shift+Tab');
    const info = await activeInfo();
    if (!info.inDialog) { reverseEscaped = true; break; }
  }
  set('shift_tab_trapped', !reverseEscaped, {reverseEscaped});

  // ③ ESC 关闭
  await page.keyboard.press('Escape');
  let closed = false;
  try {
    await dialog.waitFor({state: 'hidden', timeout: 8000});
    closed = true;
  } catch { /* 没关掉就是失败 */ }
  set('escape_closes', closed, {});
  await page.waitForTimeout(300);

  // ④ 关闭后焦点回到触发按钮
  const restored = await page.evaluate(() => {
    const el = document.activeElement;
    if (!el) return {restored: false, tag: null};
    return {restored: el.getAttribute('data-k38-trigger') === '1',
            tag: el.tagName,
            text: (el.textContent || '').trim().slice(0, 30)};
  });
  set('focus_restored_to_trigger', restored.restored, restored);

  // 再打开一次验证焦点仍正常（防止 forceMount/动画卸载把焦点留在 body）
  await trigger.click();
  await dialog.waitFor({state: 'visible', timeout: 10000});
  await page.waitForTimeout(300);
  const secondOpen = await activeInfo();
  set('reopen_focus_inside_dialog', secondOpen.inDialog && !secondOpen.isBody, secondOpen);
  await page.keyboard.press('Escape');
  await dialog.waitFor({state: 'hidden', timeout: 8000}).catch(() => {});

  result.status = result.failures.length ? 'FAIL' : 'PASS';
  return result;
}

async function tableChecks(page, h) {
  const result = {status: 'PASS', pages: {}, failures: []};
  const routes = [['logs', '/logs/'], ['keys', '/keys/'], ['accounts', '/accounts/'],
                  ['stats', '/stats/']];
  for (const [name, route] of routes) {
    await page.goto(h.base.origin + route, {waitUntil: 'domcontentloaded'});
    await page.waitForTimeout(1800);
    const info = await page.evaluate(() => {
      const tables = [...document.querySelectorAll('table')];
      const detail = tables.map((t) => {
        const heads = [...t.querySelectorAll('thead th')];
        const firstRow = t.querySelector('tbody tr');
        const cells = firstRow ? [...firstRow.querySelectorAll('td')] : [];
        return {
          headerCount: heads.length,
          headerTexts: heads.map((th) => (th.textContent || '').trim().slice(0, 20)),
          headerScopes: heads.map((th) => th.getAttribute('scope')),
          allScope: heads.length > 0 && heads.every((th) => th.getAttribute('scope')),
          firstRowCells: cells.length,
          columnsAligned: heads.length > 0 && cells.length === heads.length,
          ariaSort: heads.map((th) => th.getAttribute('aria-sort')).filter(Boolean),
          sortableHeaderButtons: heads.filter((th) => th.querySelector('button')).length,
          caption: !!t.querySelector('caption'),
          ariaLabel: t.getAttribute('aria-label') || '',
        };
      });
      // 表格内按钮的可访问名称（文本 / aria-label / title 至少一个）
      const buttons = [...document.querySelectorAll('table button')];
      const unnamed = buttons.filter((b) => !((b.textContent || '').trim()
        || b.getAttribute('aria-label') || b.getAttribute('title'))).length;
      return {tableCount: tables.length, detail, buttons: buttons.length, unnamed,
              roleTableDivs: document.querySelectorAll('[role="table"]').length};
    });
    const detail = info.detail;
    const problems = [];
    if (info.tableCount === 0) problems.push('没有真实 <table>');
    if (info.roleTableDivs > 0) problems.push('存在 div 伪表格（role=table）');
    if (info.unnamed > 0) problems.push(`${info.unnamed} 个表格内按钮无可访问名称`);
    for (const [i, t] of detail.entries()) {
      if (!t.allScope) problems.push(`表 ${i + 1}: 有 th 缺 scope`);
      if (!t.columnsAligned) problems.push(`表 ${i + 1}: 表头 ${t.headerCount} 列 vs 首行 ${t.firstRowCells} 单元格`);
      if (t.headerTexts.some((x) => !x)) problems.push(`表 ${i + 1}: 存在空表头`);
      if (t.sortableHeaderButtons > 0 && t.ariaSort.length === 0) {
        problems.push(`表 ${i + 1}: 有可点击排序表头但缺 aria-sort`);
      }
    }
    result.pages[name] = {route, ...info, problems,
                          sortableHeaders: detail.some((t) => t.sortableHeaderButtons > 0)};
    if (problems.length) result.failures.push(`${name}: ${problems.join('; ')}`);
  }
  // 移动端列表语义：accounts 在 <768px 用卡片列表替代表格（不是表格，所以不用
  // role=table；但必须是 list/listitem，否则屏幕阅读器只当成一串 div）。
  await page.setViewportSize({width: 360, height: 740});
  await page.goto(h.base.origin + '/accounts/', {waitUntil: 'domcontentloaded'});
  await page.waitForTimeout(1500);
  const mobile = await page.evaluate(() => {
    const list = document.querySelector('[role="list"]');
    const items = list ? [...list.querySelectorAll('[role="listitem"]')] : [];
    const visible = items.filter((el) => el.getBoundingClientRect().width > 0);
    return {
      listPresent: !!list,
      listitems: items.length,
      visibleListitems: visible.length,
      emptyText: visible.filter((el) => !(el.textContent || '').trim()).length,
    };
  });
  result.mobile_list = mobile;
  if (mobile.listitems === 0) {
    result.mobile_list_note = 'fixture 无账号数据，列表语义无法实测（不记 PASS/FAIL）';
  } else {
    if (!mobile.listPresent) result.failures.push('移动端 accounts 卡片列表缺 role="list"');
    if (mobile.visibleListitems === 0) result.failures.push('移动端列表项不可见');
    if (mobile.emptyText > 0) result.failures.push(`移动端 ${mobile.emptyText} 个列表项无可读文本`);
  }
  await page.setViewportSize({width: 1440, height: 1000});

  // 排序走的是 <Select> 而不是列头按钮 → aria-sort 不适用，留证据
  result.aria_sort_note = '排序控件是 <Select>（accounts/models 页），不是可点击列头，'
    + '因此 aria-sort 不适用；若将来改成列头排序，本项应改为断言 aria-sort。';
  result.status = result.failures.length ? 'FAIL' : 'PASS';
  return result;
}

async function liveContrast(page) {
  const result = {status: 'PASS', themes: {}, failures: []};
  for (const scheme of ['light', 'dark']) {
    await page.emulateMedia({colorScheme: scheme});
    await page.goto(await page.evaluate(() => location.origin) + '/dashboard/',
                    {waitUntil: 'domcontentloaded'});
    await page.waitForTimeout(1200);
    const measured = await page.evaluate(() => {
      const css = getComputedStyle(document.documentElement);
      const read = (name) => css.getPropertyValue(name).trim();
      const pick = (sel) => {
        const el = document.querySelector(sel);
        if (!el) return null;
        const cs = getComputedStyle(el);
        return {color: cs.color, background: cs.backgroundColor,
                borderColor: cs.borderTopColor};
      };
      return {
        tokens: Object.fromEntries(['--background', '--foreground', '--muted-foreground',
          '--destructive', '--ring', '--input', '--border', '--card', '--primary',
          '--primary-foreground'].map((n) => [n, read(n)])),
        body: pick('body'),
        muted: pick('[class*="text-muted-foreground"]'),
        tableHead: pick('[data-slot="table-head"]'),
        primaryButton: pick('[data-slot="button"].bg-primary'),
        isDark: document.documentElement.classList.contains('dark'),
      };
    });
    result.themes[scheme] = measured;
    if (!measured.body) result.failures.push(`${scheme}: 取不到 body 计算样式`);
    if (measured.body && measured.body.background === 'rgba(0, 0, 0, 0)') {
      result.failures.push(`${scheme}: body 背景透明，无法计算对比度`);
    }
  }
  await page.emulateMedia({colorScheme: null});
  result.status = result.failures.length ? 'FAIL' : 'PASS';
  result.note = '本项只证明"构建产物里的令牌值可读取、深浅色均生效"；'
    + '数值判定以 contrast.json（源令牌静态计算）为准，两者应一致。';
  return result;
}

async function cpuThrottle(page) {
  const result = {status: 'PASS', runs: [], long_tasks: [], failures: []};
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Performance.enable').catch(() => {});
  await page.addInitScript(() => {
    window.__k38Long = [];
    try {
      new PerformanceObserver((list) => {
        for (const e of list.getEntries()) {
          if (e.duration > 50) window.__k38Long.push(Math.round(e.duration));
        }
      }).observe({entryTypes: ['longtask']});
    } catch { /* 不支持 longtask 时忽略 */ }
  });
  for (const rate of [1, 4, 6]) {
    await cdp.send('Emulation.setCPUThrottlingRate', {rate});
    await cdp.send('Network.clearBrowserCache').catch(() => {});
    const t0 = Date.now();
    await page.goto(await page.evaluate(() => location.origin) + '/dashboard/',
                    {waitUntil: 'load', timeout: 120000});
    const loadMs = Date.now() - t0;
    let idleMs = loadMs;
    try {
      await page.waitForLoadState('networkidle', {timeout: 60000});
      idleMs = Date.now() - t0;
    } catch { /* networkidle 超时如实记录 */ }
    const long = await page.evaluate(() => window.__k38Long || []);
    const over1s = long.filter((d) => d >= 1000);
    result.runs.push({cpu_throttle: rate, load_ms: loadMs, idle_ms: idleMs,
                      long_tasks_over_50ms: long.length,
                      long_tasks_over_1s: over1s.length,
                      max_long_task_ms: long.length ? Math.max(...long) : 0});
    if (over1s.length) result.long_tasks.push({rate, over1s});
  }
  await cdp.send('Emulation.setCPUThrottlingRate', {rate: 1});
  result.note = '不做性能断言（任务卡只要求基线数字）；>1s 长任务如实列出。';
  return result;
}

// ─────────────────────────────── main ────────────────────────────────────────
if (STATIC_ONLY) {
  const current = staticContrast();
  const before = COMPARE && fs.existsSync(COMPARE)
    ? JSON.parse(fs.readFileSync(COMPARE, 'utf8')) : null;
  const contrastPath = writeContrastArtifacts(current, before);
  const audit = staticA11yAudit();
  fs.writeFileSync(path.join(OUT_DIR, 'static-a11y.json'),
                   JSON.stringify({schema: 'zhihui-k38-static/v1', contrast: {
                     source: current.source,
                     failing: {light: current.light.filter((r) => r.pass === false).map((r) => r.id),
                               dark: current.dark.filter((r) => r.pass === false).map((r) => r.id)},
                   }, audit}, null, 2));
  const failing = {
    light: current.light.filter((r) => r.pass === false),
    dark: current.dark.filter((r) => r.pass === false),
  };
  console.log(JSON.stringify({
    contrast: contrastPath,
    failing_light: failing.light.map((r) => `${r.id}=${fmt(r.ratio)}`),
    failing_dark: failing.dark.map((r) => `${r.id}=${fmt(r.ratio)}`),
    table_head_scope_default: audit.tableHeadScopeDefault,
    pages_without_native_table: audit.pages.filter((p) => !p.nativeTable).map((p) => p.page),
    dialogs: audit.dialogs.map((d) => `${d.file}:${d.primitive}`),
    destructive_solid_violations: audit.destructiveSolid,
  }, null, 2));
  process.exit(failing.light.length + failing.dark.length ? 2 : 0);
}

if (!argvFixture) {
  console.error('用法：\n'
    + '  静态（无需 fixture）：node tests/browser/k38_a11y_final.mjs --static-only\n'
    + '  浏览器（需 fixture）：node tests/browser/k38_a11y_final.mjs <fixture.json>');
  process.exit(64);
}
const browseResult = await browserRun(argvFixture);
console.log('K38_REPORT ' + path.join(OUT_DIR, 'report.json'));
console.log(JSON.stringify(browseResult.parts, null, 2).slice(0, 4000));
process.exit(browseResult.status === 'completed' ? 0 : 1);
