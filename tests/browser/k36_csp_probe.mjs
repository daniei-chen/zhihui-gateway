// K36 浏览器侧实测：CSP 是否真的生效。
//
// 方法学说明（重要，别再踩）：
//   `page.evaluate` 走 CDP 的 Runtime.evaluate，**不受页面 CSP 约束**。
//   用它测 "eval 是否被拦" 会永远得到"没被拦"的假结论（实测 eval('1+1') 返回 2，
//   而页面 CSP 明明没有 unsafe-eval）。这与此前 N25（用 route.abort 测取消路径）
//   是同一类错误：用测试工具的旁路去验证被测对象的约束。
//
// 正确做法：让**页面自己**加载脚本，在脚本内部触发受 CSP 约束的行为。
//   - 同源脚本：route fulfill 提供内容 → CSP 的 'self' 允许 → 必须能跑
//   - 跨源脚本：localhost（同机不同 host）→ 不在 'self' → 必须被拦
//   - 脚本内部 eval / 内联脚本：在页面脚本里执行，受 CSP 约束
//
// 只在本地 fixture 上跑（127.0.0.1:18443），绝不连生产。
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

const run = path.resolve('evidence/browser/k36-csp-' + crypto.randomUUID().slice(0, 8));
fs.mkdirSync(run, {recursive: true});
fs.copyFileSync(new URL(import.meta.url), path.join(run, 'verifier.mjs'));

const report = {
  schema: 'zhihui-csp-probe/v2',
  source_sha256: fixture.source_sha256,
  production_connected: false,
  method_note: 'page.evaluate 绕过 CSP，所有 CSP 断言都在页面脚本内部执行',
  status: 'running',
};
const persist = () => fs.writeFileSync(path.join(run, 'report.json'), JSON.stringify(report, null, 2));
persist();

const browser = await chromium.launch({headless: true});
const ctx = await browser.newContext({ignoreHTTPSErrors: true, locale: 'zh-CN', viewport: {width: 1440, height: 1000}});

const PROBE = `
  window.__K36 = window.__K36 || {probe_loaded: true};
  // 1) 脚本内部 eval —— 受 CSP 约束（无 unsafe-eval 必须抛）
  try { window.__K36.eval_result = String(eval('1+1')); }
  catch (e) { window.__K36.eval_error = String(e).slice(0, 120); }
  // 2) 脚本内部插入内联脚本 —— 受 CSP 约束
  try {
    const s = document.createElement('script');
    s.textContent = 'window.__K36.inline_ran = true';
    document.head.appendChild(s);
  } catch (e) { window.__K36.inline_error = String(e).slice(0, 120); }
`;
const CROSS = 'window.__K36 = window.__K36 || {}; window.__K36.cross_ran = true;';

await ctx.route('**/*', async (route) => {
  const url = new URL(route.request().url());
  const samePortLocalhost = url.hostname === 'localhost' && String(url.port) === String(base.port);
  // 同源探针：必须与 base 完全同 origin，否则不在 CSP 'self' 内
  if (url.origin === base.origin && url.pathname === '/k36-probe.js') {
    return route.fulfill({status: 200, contentType: 'application/javascript', body: PROBE});
  }
  if (samePortLocalhost && url.pathname === '/k36-cross.js') {
    return route.fulfill({status: 200, contentType: 'application/javascript', body: CROSS});
  }
  if (['http:', 'https:'].includes(url.protocol) && url.origin !== base.origin && !samePortLocalhost) {
    return route.abort();
  }
  return route.continue();
});

await ctx.addInitScript(() => {
  localStorage.setItem('qtg.platform', 'workbuddy');
  localStorage.setItem('workbuddy-manager:locale', 'zh-CN');
});

const page = await ctx.newPage();
const violations = [];
page.on('console', (m) => {
  const t = m.text();
  if (/Content Security Policy|Refused to/i.test(t)) violations.push(t.slice(0, 300));
});

await page.goto(base.origin + '/login/', {waitUntil: 'domcontentloaded'});
await page.waitForTimeout(1200);
await page.locator('#username').fill('gateway-admin');
await page.locator('#password').fill('synthetic-password');
await Promise.all([
  page.waitForResponse(r => new URL(r.url()).pathname === '/api/login' && r.request().method() === 'POST', {timeout: 45000}),
  page.locator('button[type="submit"]').click(),
]);

await page.goto(base.origin + '/', {waitUntil: 'domcontentloaded'});
await page.waitForTimeout(1500);

const resp = await page.goto(base.origin + '/dashboard/', {waitUntil: 'domcontentloaded'});
await page.waitForTimeout(1500);
const headers = resp.headers();

// 1) 同源脚本（CSP 'self' 允许）必须能加载并执行
const sameOrigin = await page.evaluate((src) => new Promise((resolve) => {
  const s = document.createElement('script');
  s.src = src;
  s.onload = () => resolve({loaded: true});
  s.onerror = () => resolve({loaded: false});
  document.head.appendChild(s);
  setTimeout(() => resolve({loaded: 'timeout'}), 5000);
}), base.origin + '/k36-probe.js');
await page.waitForTimeout(600);
const afterSameOrigin = await page.evaluate(() => window.__K36 || null);

// 2) 跨源脚本（localhost ≠ 127.0.0.1）必须被拦
const crossOrigin = await page.evaluate((src) => new Promise((resolve) => {
  window.__K36 = window.__K36 || {};
  const s = document.createElement('script');
  s.src = src;
  s.onload = () => resolve({loaded: true});
  s.onerror = () => resolve({loaded: false});
  document.head.appendChild(s);
  setTimeout(() => resolve({loaded: 'timeout'}), 4000);
}), `https://localhost:${base.port}/k36-cross.js`);
await page.waitForTimeout(600);
const afterCross = await page.evaluate(() => window.__K36 || null);

// 3) React 水合痕迹（合法应用脚本确实跑了）
const hydrated = await page.evaluate(() =>
  !!(document.querySelector('#__next, [data-reactroot], main') && document.body.innerText.length > 50));

const results = {
  headers: Object.fromEntries(Object.entries(headers).filter(([k]) =>
    /^(x-|content-security|referrer|permissions|strict)/i.test(k))),
  same_origin_url: base.origin + '/k36-probe.js',
  same_origin_script: sameOrigin,
  same_origin_effects: afterSameOrigin,
  cross_origin_script: crossOrigin,
  cross_origin_effect: afterCross?.cross_ran === true,
  hydrated,
};
report.results = results;
report.csp_violations = violations.slice(0, 20);

const csp = headers['content-security-policy'] || '';
const noUnsafeEval = !/\bunsafe-eval\b/.test(csp);
report.verdict = {
  has_csp: !!csp,
  has_all_headers: ['x-content-type-options', 'x-frame-options', 'referrer-policy',
                    'permissions-policy'].every(h => h in headers),
  csp_declares_no_unsafe_eval: noUnsafeEval,
  same_origin_script_ran: sameOrigin.loaded === true && afterSameOrigin?.probe_loaded === true,
  // eval 被拦 ⇔ 脚本内部 eval 抛错且没有返回值
  eval_blocked: noUnsafeEval ? !!afterSameOrigin?.eval_error && afterSameOrigin?.eval_result === undefined : false,
  inline_blocked: afterSameOrigin?.inline_ran !== true,
  cross_origin_blocked: crossOrigin.loaded !== true && afterCross?.cross_ran !== true,
  app_hydrated: hydrated,
};
report.status = 'completed';
persist();
await page.screenshot({path: path.join(run, 'after.png')});
console.log(JSON.stringify({run, csp, verdict: report.verdict}, null, 2));
console.log('VIOLATIONS', JSON.stringify(report.csp_violations.slice(0, 5), null, 1));
console.log('REPORT ' + path.join(run, 'report.json'));
await browser.close();
