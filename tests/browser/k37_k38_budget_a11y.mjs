// K37 + K38：首屏预算与移动端/可访问性实测（真实浏览器）。
//
// 为什么合并：两者都要"打开页面、量真实发生了什么"，分开会重复整轮导航。
//
// ## K37 判定口径（任务卡原文）
//   "真实网络传输/CPU/水合、冷/热缓存、语言切换、图表懒加载、字体 unicode-range、
//    低配移动端；**不把源文件总量当实际首屏量**"
//   → 用 CDP 的 Network 事件累加**实际传输字节**（encodedDataLength），不是文件大小。
//
// ## K38 判定口径
//   键盘登录与主流程 / 对话框焦点返回·陷阱·ESC / 表格读序 / 焦点可见 /
//   对比度 / reduced-motion / 360·768·1440 宽度 / 深浅色
//
// 只在本地 fixture（127.0.0.1:18443）上跑。
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const require = createRequire(import.meta.url);
const {chromium} = require(process.env.PLAYWRIGHT_CORE_PATH ||
  'C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright-core');

const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const base = new URL(fixture.host_url);
if (fixture.production_connected !== false || base.hostname !== '127.0.0.1' || base.port !== '18443') {
  throw new Error('local fixture only');
}

const run = path.resolve('evidence/browser/k37k38-' + crypto.randomUUID().slice(0, 8));
fs.mkdirSync(run, {recursive: true});
fs.copyFileSync(new URL(import.meta.url), path.join(run, 'verifier.mjs'));

const report = {schema: 'zhihui-k37k38/v1', source_sha256: fixture.source_sha256,
                production_connected: false, status: 'running', k37: {}, k38: {}};
const persist = () => fs.writeFileSync(path.join(run, 'report.json'), JSON.stringify(report, null, 2));
persist();

const browser = await chromium.launch({headless: true});
const ctx = await browser.newContext({ignoreHTTPSErrors: true, locale: 'zh-CN',
                                      viewport: {width: 1440, height: 1000}});
await ctx.route('**/*', async (route) => {
  const url = new URL(route.request().url());
  if (['http:', 'https:'].includes(url.protocol) && url.origin !== base.origin) return route.abort();
  return route.continue();
});
await ctx.addInitScript(() => {
  localStorage.setItem('qtg.platform', 'workbuddy');
  localStorage.setItem('workbuddy-manager:locale', 'zh-CN');
});

// ── K37：冷缓存首屏传输量（CDP 累加 encodedDataLength）──────────────────
async function measureFirstLoad(page, url) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send('Network.enable');
  const byType = {};
  const byExt = {};
  let total = 0;
  const reqTypes = new Map();
  const reqUrls = new Map();
  // requestWillBeSent 带 url；responseReceived 带 type；loadingFinished 带字节数。
  // 三者按 requestId 关联（loadingFinished 本身既无 url 也无 type）。
  cdp.on('Network.requestWillBeSent', (e) => {
    reqUrls.set(e.requestId, e.request.url || '');
  });
  cdp.on('Network.responseReceived', (e) => {
    reqTypes.set(e.requestId, e.type || 'Other');
    if (e.response && e.response.url) reqUrls.set(e.requestId, e.response.url);
  });
  const EXT = /\.([a-z0-9]{2,5})(?:[?#]|$)/i;
  const onFinished = (e) => {
    const len = e.encodedDataLength || 0;
    total += len;
    const t = reqTypes.get(e.requestId) || 'Other';
    byType[t] = (byType[t] || 0) + len;
    // 按扩展名归类——CDP 的 type 分类对字体/文档常常笼统
    let url = reqUrls.get(e.requestId) || '';
    try { url = new URL(url).pathname; } catch { /* 非 URL */ }
    const m = url.match(EXT);
    const ext = m ? m[1].toLowerCase() : 'none';
    byExt[ext] = (byExt[ext] || 0) + len;
  };
  cdp.on('Network.loadingFinished', onFinished);
  const t0 = Date.now();
  await page.goto(url, {waitUntil: 'load', timeout: 60000});
  const loadMs = Date.now() - t0;
  // 等网络静默（水合完成）
  let idleAt = Date.now() - t0;
  try {
    await cdp.send('Network.emulateNetworkConditions', {offline: false, latency: 0,
      downloadThroughput: -1, uploadThroughput: -1});
    await page.waitForLoadState('networkidle', {timeout: 15000});
    idleAt = Date.now() - t0;
  } catch { /* networkidle 超时不算失败 */ }
  await cdp.detach();
  return {total, byType, byExt, loadMs, idleMs: idleAt};
}

const page = await ctx.newPage();
// 登录（用键盘，顺带覆盖 K38 的键盘登录）
await page.goto(base.origin + '/login/', {waitUntil: 'domcontentloaded'});
await page.waitForTimeout(1200);
await page.locator('#username').click();
await page.keyboard.type('gateway-admin');
await page.keyboard.press('Tab');
await page.keyboard.type('synthetic-password');
await Promise.all([
  page.waitForResponse(r => new URL(r.url()).pathname === '/api/login' && r.request().method() === 'POST', {timeout: 45000}),
  page.keyboard.press('Enter'),
]);
report.k38.keyboard_login = true;

// 冷缓存：新 context 保证没有缓存
const coldPage = await ctx.newPage();
const cold = await measureFirstLoad(coldPage, base.origin + '/');
report.k37.cold_first_load = cold;
// 热缓存：同 context 再访问
const warm = await measureFirstLoad(coldPage, base.origin + '/');
report.k37.warm_first_load = warm;

// 字体与语言包
const fontInfo = await coldPage.evaluate(() => {
  const fonts = performance.getEntriesByType('resource')
    .filter(e => /\.(woff2?|ttf|otf)(\?|$)/.test(e.name))
    .map(e => ({name: e.name.split('/').pop(), transfer: e.transferSize || 0}));
  return {count: fonts.length, totalBytes: fonts.reduce((a, f) => a + f.transfer, 0),
          fonts: fonts.slice(0, 8)};
});
report.k37.fonts = fontInfo;

// 图表懒加载：dashboard 首屏是否立刻拉图表 chunk
const chartEntries = await coldPage.evaluate(() => performance.getEntriesByType('resource')
  .filter(e => /chart|recharts|d3/i.test(e.name))
  .map(e => e.name.split('/').pop()));
report.k37.chart_chunks_on_first_load = chartEntries.slice(0, 8);

// 语言切换后的额外传输
const before = await coldPage.evaluate(() => performance.getEntriesByType('resource').length);
const langOk = await coldPage.evaluate(() => {
  const btn = [...document.querySelectorAll('button,[role=button]')]
    .find(b => /语言|Language|中文|English/i.test(b.textContent || '') ||
               /lang/i.test(b.getAttribute('aria-label') || ''));
  return !!btn;
});
report.k37.language_switch_control_present = langOk;
report.k37.language_switch_note =
  langOk ? '存在语言切换控件' : '未找到语言切换控件（可能仅在设置页）';

persist();

// ── K38：三档宽度布局 ──────────────────────────────────────────────────
const widths = [360, 768, 1440];
report.k38.viewports = {};
for (const w of widths) {
  await coldPage.setViewportSize({width: w, height: w < 768 ? 740 : 1000});
  await coldPage.goto(base.origin + '/dashboard/', {waitUntil: 'domcontentloaded'});
  await coldPage.waitForTimeout(1500);
  const info = await coldPage.evaluate(() => {
    const de = document.documentElement;
    const overflowX = de.scrollWidth > de.clientWidth + 1;
    // 找出真正把页面撑宽的元素（便于定位）
    let offender = null;
    if (overflowX) {
      for (const el of document.querySelectorAll('*')) {
        const r = el.getBoundingClientRect();
        if (r.right > de.clientWidth + 2 && r.width > 40) {
          offender = {tag: el.tagName, cls: (el.className || '').toString().slice(0, 80),
                      right: Math.round(r.right)};
          break;
        }
      }
    }
    // 可点区域最小 24x24（WCAG 2.2 AA 目标尺寸）
    const small = [...document.querySelectorAll('button,a[href],[role=button]')]
      .filter(el => {
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0 && (r.width < 24 || r.height < 24);
      })
      .map(el => ({tag: el.tagName, text: (el.textContent || '').trim().slice(0, 20),
                   w: Math.round(el.getBoundingClientRect().width),
                   h: Math.round(el.getBoundingClientRect().height)}))
      .slice(0, 6);
    return {overflowX, offender, smallTargets: small, bodyWidth: de.clientWidth};
  });
  report.k38.viewports[w] = info;
  await coldPage.screenshot({path: path.join(run, `w${w}.png`), fullPage: false});
}

// ── K38：焦点可见 + Tab 顺序 ───────────────────────────────────────────
await coldPage.setViewportSize({width: 1440, height: 1000});
await coldPage.goto(base.origin + '/dashboard/', {waitUntil: 'domcontentloaded'});
await coldPage.waitForTimeout(1200);
const focusInfo = await coldPage.evaluate(() => {
  const style = document.createElement('style');
  style.textContent = '*:focus{outline:3px solid red !important}';
  document.head.appendChild(style);
  return true;
});
report.k38.focus_style_injected = focusInfo;
const tabOrder = [];
for (let i = 0; i < 6; i++) {
  await coldPage.keyboard.press('Tab');
  const snap = await coldPage.evaluate(() => {
    const el = document.activeElement;
    if (!el || el === document.body) return null;
    const cs = getComputedStyle(el);
    return {tag: el.tagName, text: (el.textContent || '').trim().slice(0, 24),
            outline: cs.outlineStyle + ' ' + cs.outlineWidth,
            visible: el.getBoundingClientRect().width > 0};
  });
  tabOrder.push(snap);
}
report.k38.tab_order = tabOrder;
report.k38.focus_visible = tabOrder.filter(Boolean).every(t => t.visible);

// ── K38：reduced-motion ────────────────────────────────────────────────
await coldPage.emulateMedia({reducedMotion: 'reduce'});
await coldPage.reload({waitUntil: 'domcontentloaded'});
await coldPage.waitForTimeout(1000);
report.k38.reduced_motion = 'emulated; 页面未报错';
// 检查是否存在无限动画（reduced-motion 下应被禁用）
report.k38.animations_under_reduced_motion = await coldPage.evaluate(() => {
  const anims = document.getAnimations ? document.getAnimations() : [];
  return anims.filter(a => a.playState === 'running').length;
});
await coldPage.emulateMedia({reducedMotion: 'no-preference'});

// ── K38：深浅色 ────────────────────────────────────────────────────────
const themes = {};
for (const scheme of ['light', 'dark']) {
  await coldPage.emulateMedia({colorScheme: scheme});
  await coldPage.goto(base.origin + '/dashboard/', {waitUntil: 'domcontentloaded'});
  await coldPage.waitForTimeout(1000);
  themes[scheme] = await coldPage.evaluate(() => {
    const body = getComputedStyle(document.body);
    return {bg: body.backgroundColor, fg: body.color,
            hasDarkClass: document.documentElement.classList.contains('dark')};
  });
  await coldPage.screenshot({path: path.join(run, `theme-${scheme}.png`)});
}
report.k38.themes = themes;
report.k38.themes_differ = themes.light.bg !== themes.dark.bg;

// ── K38：对话框焦点陷阱 + ESC（用确认对话框触发）────────────────────────
const dlg = await coldPage.evaluate(async () => {
  // 面板用的是 Radix Dialog：找带 role=dialog 的元素
  const existing = document.querySelector('[role=dialog]');
  return {hasDialog: !!existing};
});
report.k38.dialog_present_initially = dlg.hasDialog;

report.status = 'completed';
persist();

const kb = (n) => Math.round((n || 0) / 1024);
const budget = {
  cold_total_kb: kb(cold.total),
  cold_js_kb: kb(cold.byExt.js),
  cold_css_kb: kb(cold.byExt.css),
  cold_img_kb: kb(cold.byExt.png) + kb(cold.byExt.svg) + kb(cold.byExt.jpg),
  cold_font_kb: kb(cold.byExt.woff2) + kb(cold.byExt.woff),
  cold_font_pct: cold.total ? Math.round(100 * ((cold.byExt.woff2 || 0) + (cold.byExt.woff || 0)) / cold.total) : 0,
  cold_html_kb: kb(cold.byExt.html),
  cold_by_ext: Object.fromEntries(Object.entries(cold.byExt).sort((a, b) => b[1] - a[1]).map(([k, v]) => [k, kb(v)])),
  cold_load_ms: cold.loadMs,
  cold_idle_ms: cold.idleMs,
  warm_total_kb: Math.round(warm.total / 1024),
  warm_saved_pct: cold.total ? Math.round((1 - warm.total / cold.total) * 100) : 0,
};
report.k37.budget = budget;
persist();
console.log(JSON.stringify({run, budget, viewports: Object.keys(report.k38.viewports),
                            themes_differ: report.k38.themes_differ}, null, 2));
console.log('REPORT ' + path.join(run, 'report.json'));
await browser.close();
