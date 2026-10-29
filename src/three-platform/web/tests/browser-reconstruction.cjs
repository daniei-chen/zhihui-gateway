const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const operations = require('./operational-fixtures.cjs');

const root = path.resolve(__dirname, '..');
const output = path.resolve(process.env.QA_OUTPUT || path.join(root, '../evidence/browser'));
fs.mkdirSync(output, {recursive: true});
const built = path.join(root, 'out');
const trace = [];
const missingStatic = new Set();
let authenticated = true;
let logoutMode = 'failure';
let fixtureRole = 'admin';
let managedFixture = true;
const product = {name: '智汇网关', version: '0.0.2', repository: 'daniei-chen/zhihui-gateway',
  repository_url: 'https://github.com/daniei-chen/zhihui-gateway', branch: 'main',
  releases_url: 'https://github.com/daniei-chen/zhihui-gateway/releases', source_commit: 'a'.repeat(40),
  manager_baseline: 'v10.7.22+audit.20261029.2', release_channel: 'controlled-signed',
  managed: true, web_update_enabled: false, update_policy: 'trusted-publisher-required',
  provider_endpoints_changed: false};
const unknown = new Set();
const today = new Date().toISOString().slice(0, 10);

function fixture(url, request) {
  const operation = operations.fixture(url);
  if (operation !== undefined) return operation;
  const p = url.pathname;
  const platform = request.headers['x-qtg-platform'] || url.searchParams.get('platform') || 'workbuddy';
  const realm = url.searchParams.get('realm') || 'cn';
  const amount = platform === 'qoder' ? 202 : platform === 'trae' ? 303 : 101;
  const accounts = ['cn', 'global'].map(r => ({file: `${platform}-${r}.json`, uid: `${platform}-${r}`, nickname: `${platform}-${r}-fixture`,
    enterprise_id: '', expires_at: 1900000000, is_expired: false, remain_seconds: 9999999, credits: amount,
    healthy: true, disabled: false, cooling: false, in_pool: true, success_count: 8, source: 'file', realm: r}));
  const version = {current: 'fixture', latest: 'fixture', has_update: false, error: '', repo: '', assets_ready: true};
  if (p === '/api/me') return authenticated ? {username: 'reconstruction-fixture', role: fixtureRole} : [401, {detail: 'No session'}];
  if (p === '/api/logout') {
    if (logoutMode === 'failure') return [500, {detail: 'Simulated logout failure; session remains active'}];
    authenticated = false; return {ok: true};
  }
  if (p === '/api/upstreams') return {items: [{id: null, name: 'Fixture', base_url: 'http://fixture.invalid', enabled: true, is_default: true, has_key: false, api_key_masked: '', auth_dir: '/fixture', note: '', container: '', bound_keys: 0}]};
  if (p === '/api/accounts/refresh-credits') return {credits: {}, meta: {}};
  if (p === '/api/accounts') return {accounts, total: accounts.length, manageable: false, pool_available: true, upstream: {id: null, name: 'Fixture', is_default: true}};
  if (p === '/api/status') return {connected: true, total: 2, healthy: 2, cooling: 0, disabled: 0, in_flight_full: 0, accounts, realm_totals: {cn: {total: 1, healthy: 1, cooling: 0, disabled: 0, in_flight_full: 0}, global: {total: 1, healthy: 1, cooling: 0, disabled: 0, in_flight_full: 0}}};
  if (p === '/api/stats/summary') return {today_requests: amount, today_tokens: amount * 100, today_credit: 3.5, week_credit: 9.5, total_credit: 19.5, week_requests: amount, week_tokens: amount * 100, total_requests: amount, total_tokens: amount * 100, active_keys: 0, top_model: 'fixture-model', failures: {today_4xx: 1, today_5xx: 0, week_4xx: 1, week_5xx: 0}, usage_health: {ok: true}};
  if (p === '/api/stats/daily' && process.env.QA_HOME_PREVIEW) return [18, 24, 37, 29, 52, 46, 61, 56, 73, 63, 82, 71, 101, 85].map((requests, index) => ({
    day: new Date(Date.now() - (13 - index) * 86400000).toISOString().slice(0, 10), requests,
    prompt_tokens: requests * 740, completion_tokens: requests * 180, failed: index % 5 === 0 ? 2 : 0,
  }));
  if (p === '/api/stats/daily') return [{day: today, requests: amount, prompt_tokens: 1000, completion_tokens: 2000, failed: 1}];
  if (p === '/api/stats/hourly') return [{day: today, hour: 10, requests: amount, prompt_tokens: 1000, completion_tokens: 2000, failed: 1}];
  if (p === '/api/stats/credits') return {days: 7, from: today, to: today, today: {income: 19710, expense: 17014.094, net: 2695.906}, week: {income: 19710, expense: 17014.094, net: 2695.906}, total: {income: 19710, expense: 17014.094, net: 2695.906}, daily: [{day: today, income: 19710, expense: 17014.094, net: 2695.906}]};
  if (['/api/stats/by-model', '/api/stats/by-key'].includes(p)) return [
    {name: p.endsWith('by-key') ? 'creative-studio' : 'fixture-model', key_id: 7, requests: 12,
      prompt_tokens: 10000, completion_tokens: 2000, credit: 1.25, cache_hit_tokens: 9400,
      cache_miss_tokens: 600, cache_hit_rate: 0.94, tokens_per_sec: 37.5, throughput_samples: 12},
    {name: 'no-measurement', key_id: 8, requests: 2, prompt_tokens: 300, completion_tokens: 100,
      credit: 0, cache_hit_rate: null, tokens_per_sec: null, throughput_samples: 0},
  ];
  if (['/api/keys', '/api/tokens', '/api/red-packets', '/api/security/rules', '/api/security/logs'].includes(p)) return [];
  if (p === '/api/stats/upstream') return {available: false, detail: 'Fixture has no live upstream'};
  if (p === '/api/logs') return {items: [], total: 0, page: 1, size: 20};
  if (p === '/api/proxies') return {routes: [], default: ''};
  if (p === '/api/models') return {models: [], source: 'static', count: 0};
  if (p === '/api/model-catalog') return {models: [], source: 'none', source_label: 'Fixture', via: '', errors: [], cached: false, cache_age: 0, summary: {total: 0, reasoning: 0, large_context: 0, max_context: 0, series: [], unique_ids: 0}};
  if (p === '/api/playground/models') return {models: [], default: '', default_model: '', realm};
  if (p === '/api/security/config') return {enabled: true, mode: 'blacklist'};
  if (p === '/api/audit-logs' || p === '/api/checkin-logs') return {items: [], total: 0};
  if (p === '/api/task-logs') return {logs: [], total: 0, stats: {by_kind: {}, total: 0, total_credits: 0}, kinds: {}, collector: {}};
  if (p === '/api/task-run') return {running: false, mode: '', target: '', started_at: 0, finished_at: 0, exit_code: null, timed_out: false, error: '', lines: [], tasks: [], available: false, unavailable_reason: 'Local fixture', schedule: {enabled: false, hours: []}};
  if (p === '/api/upstream/logs') return {available: true, lines: [], total: 0};
  if (p === '/api/task-claim-schedule') return {enabled: false, hours: []};
  if (p === '/api/accounts/refresh-plan') return {enabled: false, interval_days: 7, hour: 0, last_run: 0, next_run: null};
  if (p === '/api/accounts/daily-plan') return Object.fromEntries(['checkin', 'compensation', 'claim', 'full'].map(k => [k, {enabled: false, hour: 0, last_run: ''}]));
  if (p === '/api/settings/upstream') return {available: false, error: 'Local fixture; no production configuration loaded'};
  if (p === '/api/settings/model-map') return {};
  if (p === '/api/settings/pg-sync') return {config: {enabled: false, host: '', port: 5432, dbname: '', user: '', password: '', sslmode: 'prefer', interval_minutes: 0, keep_local_backup: true, last_export_at: 0, last_import_at: 0}, status: {running: false, kind: '', ok: null, step: '', percent: 0, tables_total: 0, tables_done: 0, rows: 0, logs: [], started_at: 0, finished_at: 0}};
  if (p === '/api/users') return [{username: 'reconstruction-fixture', role: 'viewer'}];
  if (p === '/api/settings/github-token') return {configured: false, masked: ''};
  if (p === '/api/settings/github-mirror') return {mirror: '', stored: '', env_mirror: '', source: '', presets: []};
  if (p === '/api/system/versions') return {manager: 'v0.0.2', upstream_connected: false, upstream_accounts: 0, upstream_dir: '', product: {...product, managed: managedFixture, web_update_enabled: !managedFixture}};
  if (p === '/api/system/update-status') return {available: true, running: false, ok: null, step: '', logs: [], version: 'v0.0.2', updater_found: true, upstream_dir: '', product: {...product, managed: managedFixture, web_update_enabled: !managedFixture}};
  if (p === '/api/system/check-update') return {checked_at: 0, cached: true, manager: version, upstream: {...version, changes: []}};
  if (p === '/api/system/update-package') return {present: false};
  if (p === '/api/system/changelog') return {available: true, versions: [], total: 0};
  if (p === '/api/proxy-routes') return {routes: [], pool: {enabled: false, strategy: 'round_robin', strategies: ['priority', 'round_robin', 'random', 'least_connections'], interval_seconds: 300, routes_total: 0, routes_enabled: 0}, default: '', schemes: ['http', 'https', 'socks5'], bound_total: 0};
  if (p === '/api/settings/proxy') return {global_proxy_enabled: false, http_proxy: '', update_proxy: '', http_configured: false, update_configured: false};
  if (p === '/api/proxy-routes/bindings') return {accounts: [], routes: []};
  if (p === '/api/upstream/reload-state') return {pending: false, can_reload: false, groups: []};
  if (p === '/api/keys/import-local/status') return {enabled: false, available: false, clients: []};
  if (p === '/api/claim/local-fixture') return {title: 'Local claim fixture', quota_kind: 'credit', shares: 1, left: 1, models: [], expires_at: 1900000000, expired: false, claimed: false, my_amount: null, my_key: null};
  unknown.add(p);
  return [503, {detail: `No local fixture for ${p}`}];
}

const server = http.createServer((request, response) => {
  const url = new URL(request.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) {
    trace.push({path: url.pathname, method: request.method, platform: request.headers['x-qtg-platform'] || null, queryPlatform: url.searchParams.get('platform'), realm: url.searchParams.get('realm'), upstreamPlatform: url.searchParams.get('upstream_platform'), executionId: url.searchParams.get('request_id')});
    const value = fixture(url, request);
    const [status, body] = Array.isArray(value) && typeof value[0] === 'number' ? value : [200, value];
    response.writeHead(status, {'content-type': 'application/json'}); response.end(JSON.stringify(body)); return;
  }
  if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); response.end(); return; }
  if (process.env.QA_DEV) {
    const proxy = http.request({hostname: '127.0.0.1', port: Number(process.env.QA_DEV), path: request.url, method: request.method, headers: {...request.headers, host: `127.0.0.1:${process.env.QA_DEV}`}}, upstream => {
      response.writeHead(upstream.statusCode, upstream.headers); upstream.pipe(response);
    });
    proxy.on('error', error => { response.writeHead(502); response.end(error.message); });
    proxy.end(); return;
  }
  let relative = decodeURIComponent(url.pathname).replace(/^\/+/, '');
  if (!path.extname(relative)) relative = path.join(relative, 'index.html');
  const filename = path.resolve(built, relative);
  if (!filename.startsWith(built + path.sep) || !fs.existsSync(filename) || !fs.statSync(filename).isFile()) {
    if (!missingStatic.has(url.pathname)) console.log('MISSING_STATIC', url.pathname);
    missingStatic.add(url.pathname);
    response.writeHead(404); response.end(); return;
  }
  const types = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2', '.json': 'application/json', '.txt': 'text/plain', '.png': 'image/png', '.ico': 'image/x-icon', '.svg': 'image/svg+xml'};
  const send = () => { response.writeHead(200, {'content-type': types[path.extname(filename)] || 'application/octet-stream'}); response.end(fs.readFileSync(filename)); };
  if (process.env.QA_DELAY && url.pathname.includes('/page-') && url.pathname.endsWith('.js')) setTimeout(send, 500);
  else send();
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  const result = {scope: 'Local reconstructed static export with synthetic API fixtures; no production/provider access. Not destructive-action/backend acceptance.', routes: [], hydration: [], contextSwitches: [], responsive: [], guards: [], errors: []};
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    await context.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    await context.addInitScript(() => { localStorage.setItem('workbuddy-manager:dock-tip-dismissed', 'true'); });
    const page = await context.newPage();
    let errors = [];
    const allErrors = [];
    const runtimeConsoleErrors = [];
    const hydrationFrames = [];
    if (process.env.QA_DEBUG_ONLY) {
      const cdp = await context.newCDPSession(page);
      await cdp.send('Debugger.enable');
      console.log('DEBUGGER_ENABLED: no breakpoints, unchanged production files');
    }
    if (process.env.QA_DEBUG_CAPTURE) {
      const cdp = await context.newCDPSession(page);
      await cdp.send('Debugger.enable');
      const chunks = path.join(built, '_next/static/chunks');
      for (const file of fs.readdirSync(chunks)) {
        if (!file.startsWith('4bd1b696-') || !file.endsWith('.js')) continue;
        const source = fs.readFileSync(path.join(chunks, file), 'utf8');
        const columnNumber = source.indexOf('var n=Error(i(418,');
        if (columnNumber >= 0) {
          const expression = `console.error('HYDRATION_FIBER',JSON.stringify({actual:rN?.outerHTML||rN?.nodeValue,stack:(()=>{const a=[];let f=e;for(let i=0;f&&i<12;i++,f=f.return)a.push({tag:f.tag,type:typeof f.type==='string'?f.type:f.type?.name,props:{className:f.pendingProps?.className,children:typeof f.pendingProps?.children==='string'?f.pendingProps.children:undefined}});return a})()})),false`;
          const bp = await cdp.send('Debugger.setBreakpointByUrl', {lineNumber: 0, columnNumber, ...(process.env.QA_PAUSE ? {} : {condition: expression}), urlRegex: file.replaceAll('.', '\\.')});
          console.log('HYDRATION_BREAKPOINT', bp.breakpointId);
        }
      }
      cdp.on('Debugger.paused', async event => {
        try {
          const frame = event.callFrames[0];
          const value = await cdp.send('Debugger.evaluateOnCallFrame', {callFrameId: frame.callFrameId, returnByValue: true, expression: `JSON.stringify({actual:rN?.outerHTML||rN?.nodeValue,stack:(()=>{const a=[];let f=e;for(let i=0;f&&i<12;i++,f=f.return)a.push({tag:f.tag,type:typeof f.type==='string'?f.type:f.type?.name,props:{className:f.pendingProps?.className,children:typeof f.pendingProps?.children==='string'?f.pendingProps.children:undefined},memo:{className:f.memoizedProps?.className}});return a})()})`});
          hydrationFrames.push({url: page.url(), data: value.result.value, stack: event.callFrames.slice(0, 6).map(f => f.functionName)});
          fs.writeFileSync(path.join(output, 'hydration-debug-frames.json'), JSON.stringify(hydrationFrames, null, 2));
        } finally { await cdp.send('Debugger.resume'); }
      });
    }
    page.on('pageerror', error => { errors.push(error.message); allErrors.push({url: page.url(), message: error.message}); console.log('PAGEERROR', page.url(), error.message); });
    page.on('console', message => {
      if (message.type() === 'error' || (process.env.QA_HEADER_CAPTURE && message.text().startsWith('HYDRATION_'))) console.log('CONSOLE', message.text());
      if (message.type() === 'error' && !message.text().startsWith('Failed to load resource:')) runtimeConsoleErrors.push({url: page.url(), message: message.text()});
    });
    if (process.env.QA_HOME_PREVIEW) {
      const crypto = require('node:crypto');
      const before = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'app/brand.css'))).digest('hex');
      const previews = [];
      for (const route of ['/dashboard', '/accounts', '/keys', '/models', '/logs', '/tasks', '/security', '/stats']) {
        for (const viewport of [{width: 1440, height: 930}, {width: 390, height: 650}]) {
          await page.setViewportSize(viewport);
          await page.goto(origin + route, {waitUntil: 'networkidle'});
          await page.waitForTimeout(2000);
          assert.equal(await page.locator('[data-slot=console-brand]').isVisible(), true);
          const image = route.slice(1) + (viewport.width === 1440 ? '-desktop.jpg' : '-mobile.jpg');
          await page.screenshot({path: path.join(output, image), type: 'jpeg', quality: 87, animations: 'disabled'});
          previews.push({route, image, viewport, sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(output, image))).digest('hex')});
        }
      }
      assert.deepEqual([...unknown], []);
      assert.deepEqual([...missingStatic], []);
      assert.deepEqual(allErrors, []);
      assert.deepEqual(runtimeConsoleErrors, []);
      fs.writeFileSync(path.join(output, 'home-preview.json'), JSON.stringify({
        scope: 'Actual exported console rendering with synthetic local fixtures. No production data or provider calls.',
        buildId: fs.readFileSync(path.join(root, '.next/BUILD_ID'), 'utf8').trim(),
        brandCssSha256: before, previews, errors: allErrors, unknownFixtures: [...unknown], status: 'passed',
      }, null, 2));
      console.log('HOME_PREVIEW_COMPLETE');
      return;
    }
    if (process.env.QA_VISUAL_ONLY) {
      await page.emulateMedia({reducedMotion: 'no-preference'});
      await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
      const card = page.locator('[data-slot=stat-card]').first();
      await card.hover();
      await page.waitForTimeout(100);
      assert.equal(await page.locator('.zh-console-aura').getAttribute('data-active'), '');
      assert.equal(await card.evaluate(el => el.style.getPropertyValue('--surface-light')), '1');
      assert.equal(await page.locator('html').evaluate(el => getComputedStyle(el).getPropertyValue('--chart-1').trim()), '#7399e8');
      await page.emulateMedia({reducedMotion: 'reduce'});
      await page.waitForTimeout(100);
      assert.equal(await page.locator('.zh-console-aura').getAttribute('data-active'), null);
      assert.equal(await page.locator('.zh-console-aura').evaluate(el => getComputedStyle(el).display), 'none');
      await page.emulateMedia({reducedMotion: 'no-preference'});
      await card.hover();
      await page.waitForTimeout(100);
      await page.mouse.move(2, 2);
      await page.waitForTimeout(50);
      assert.equal(await card.evaluate(el => el.style.getPropertyValue('--surface-light')), '');
      await page.waitForTimeout(1800);
      for (const wrapper of await page.locator('.recharts-wrapper').all()) {
        await wrapper.hover();
        await page.waitForTimeout(100);
      }
      await page.mouse.move(2, 2);
      for (const width of [320, 390, 768, 1440]) {
        await page.setViewportSize({width, height: 950});
        await page.waitForTimeout(400);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false,
          'Hidden chart tooltips must not keep desktop coordinates at width ' + width);
      }
      const mobileContext = await browser.newContext({hasTouch: true, isMobile: true, viewport: {width: 390, height: 844}});
      await mobileContext.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
      const mobile = await mobileContext.newPage();
      await mobile.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
      assert.equal(await mobile.locator('.zh-console-aura').evaluate(el => getComputedStyle(el).display), 'none');
      assert.equal(await mobile.locator('[data-slot=console-brand]').isVisible(), true);
      await mobileContext.close();
      assert.deepEqual(allErrors, []);
      assert.deepEqual(runtimeConsoleErrors, []);
      assert.deepEqual([...unknown], []);
      fs.writeFileSync(path.join(output, 'visual-results.json'), JSON.stringify({status: 'passed', pointer: true, reducedMotion: true, touchStatic: true, hiddenTooltipResize: true, semanticColorsPreserved: true, errors: allErrors}, null, 2));
      console.log('VISUAL_COMPLETE');
      return;
    }
    if (process.env.QA_PRODUCT_ONLY) {
      result.product = [];
      for (const locale of ['zh-CN', 'zh-TW', 'en', 'ja', 'ko']) {
        await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
        await page.evaluate(value => localStorage.setItem('workbuddy-manager:locale', value), locale);
        await page.goto(origin + '/settings/system', {waitUntil: 'networkidle'});
        const originCard = page.locator('[data-slot=product-origin]');
        await originCard.waitFor();
        assert.equal(await originCard.getByRole('link', {name: product.repository, exact: true}).getAttribute('href'), product.repository_url);
        assert.ok((await originCard.innerText()).includes(product.source_commit.slice(0, 12)));
        const copy = JSON.parse(fs.readFileSync(path.join(root, 'lib/i18n/locales', locale + '.json')));
        await page.getByText(copy.product.controlledTitle, {exact: true}).waitFor();
        const buttons = page.locator('[data-slot=update-target] button');
        assert.equal(await buttons.count(), 2);
        for (const button of await buttons.all()) assert.ok(await button.isDisabled());
        for (const width of [320, 1440]) {
          await page.setViewportSize({width, height: 1000});
          await page.waitForTimeout(300);
          const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
          if (overflow) {
            const offenders = await page.evaluate(() => [...document.querySelectorAll('*')].filter(el => el.getBoundingClientRect().right > innerWidth + 1).slice(0, 30).map(el => ({tag: el.tagName, cls: el.className, text: el.innerText?.slice(0, 120), right: el.getBoundingClientRect().right})));
            fs.writeFileSync(path.join(output, 'overflow-' + locale + '.json'), JSON.stringify(offenders, null, 2));
            await page.screenshot({path: path.join(output, 'overflow-' + locale + '.png'), fullPage: true});
          }
          assert.equal(overflow, false, locale + ' product settings overflow');
          await page.screenshot({path: path.join(output, `product-${locale}-${width}.png`), fullPage: true, animations: 'disabled'});
        }
        result.product.push({locale, managed: true, overflow: false, repository: product.repository, version: '0.0.2'});
      }
      result.errors = allErrors;
      result.runtimeConsoleErrors = runtimeConsoleErrors;
      result.missingStatic = [...missingStatic];
      fs.writeFileSync(path.join(output, 'product-results.json'), JSON.stringify(result, null, 2));
      assert.equal(allErrors.length, 0);
      assert.equal(runtimeConsoleErrors.length, 0);
      assert.deepEqual([...missingStatic], []);
      assert.ok(!trace.some(row => row.method === 'POST' && row.path.includes('/update')));
      console.log('PRODUCT_COMPLETE', JSON.stringify({checks: result.product.length, errors: allErrors.length}));
      return;
    }
    if (process.env.QA_LANGUAGE_ONLY) {
      const language = require('./language-browser-scenarios.cjs');
      result.language = await language.run(context, page, origin, output, built);
      result.errors = allErrors;
      result.runtimeConsoleErrors = runtimeConsoleErrors;
      result.missingStatic = [...missingStatic];
      fs.writeFileSync(path.join(output, 'language-results.json'), JSON.stringify(result, null, 2));
      assert.equal(allErrors.length, 0);
      assert.equal(runtimeConsoleErrors.length, 0);
      assert.deepEqual([...missingStatic], []);
      console.log('LANGUAGE_COMPLETE', JSON.stringify({checks: result.language.length, errors: allErrors.length}));
      return;
    }
    if (process.env.QA_HYDRATION) {
      for (let i = 0; i < (Number(process.env.QA_HYDRATION_COUNT) || (process.env.QA_DEBUG_CAPTURE ? 60 : 15)); i++) {
        await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
        await page.evaluate(value => {
          localStorage.setItem('qtg.platform', value % 2 ? 'qoder' : 'trae');
          localStorage.setItem('wb.realm', 'global');
          localStorage.setItem('workbuddy-manager:locale', ['en', 'ko', 'zh-CN'][value % 3]);
          localStorage.setItem('theme', 'dark');
        }, i);
        errors = [];
        const route = ['/stats', '/tasks', '/red-packets', '/settings/changelog'][i % 4];
        await page.goto(origin + route, {waitUntil: 'networkidle'});
        result.hydration.push({iteration: i + 1, route, errors: [...errors]});
      }
      result.allErrors = allErrors;
      result.runtimeConsoleErrors = runtimeConsoleErrors;
      fs.writeFileSync(path.join(output, process.env.QA_DEV ? 'hydration-dev.json' : 'hydration-prod.json'), JSON.stringify(result, null, 2));
      assert.equal(allErrors.length, 0, 'Hydration stress must not report page errors');
      assert.equal(runtimeConsoleErrors.length, 0, 'Hydration stress must not log rendering errors');
      return;
    }
    const routes = process.env.QA_QUICK ? ['/settings/changelog', '/dashboard'] : ['/', '/dashboard', '/accounts', '/keys', '/models', '/playground', '/logs', '/stats', '/tasks', '/red-packets', '/security', '/settings', '/settings/upstream', '/settings/proxy', '/settings/models', '/settings/users', '/settings/tokens', '/settings/system', '/settings/backup', '/settings/changelog', '/settings/about', '/claim', '/login'];
    for (const route of routes) {
      errors = [];
      const response = await page.goto(origin + route, {waitUntil: 'networkidle'});
      await page.waitForTimeout(120);
      const facts = await page.evaluate(() => ({path: location.pathname, title: document.title, h1: document.querySelector('h1')?.textContent, main: !!document.querySelector('main'), text: document.body.innerText.slice(-240)}));
      result.routes.push({route, status: response.status(), ...facts, errors: [...errors]});
      if (await page.locator('#main-content').count()) {
        const brand = page.locator('[data-slot=console-brand]');
        assert.equal(await brand.count(), 1, route + ' has one shared product brand');
        assert.equal(await brand.isVisible(), true, route + ' product brand is visible');
        assert.equal(await brand.locator('.zh-console-brand-name').textContent(), product.name);
        assert.equal(await brand.getAttribute('href'), '/dashboard/');
        assert.equal(await brand.locator('svg').count(), 1);
      }
      if (process.env.QA_DOC_SHOTS && ['/dashboard', '/accounts', '/keys', '/tasks', '/logs', '/stats', '/models', '/playground', '/security', '/settings'].includes(route)) {
        await page.screenshot({path: path.join(output, 'doc-' + route.slice(1) + '.png'), fullPage: true, animations: 'disabled'});
      }
      console.log(route, response.status(), errors.length);
    }
    for (let i = 0; i < (process.env.QA_QUICK ? 0 : 20); i++) {
      errors = [];
      const route = i % 2 ? '/stats' : '/dashboard';
      await page.goto(origin + route, {waitUntil: 'networkidle'});
      result.hydration.push({iteration: i + 1, route, errors: [...errors]});
    }
    result.operations = [];
    await page.goto(origin + '/logs', {waitUntil: 'networkidle'});
    let evidence = page.locator('section').filter({has: page.getByRole('heading', {name: '网关终态证据台账', exact: true})});
    await evidence.getByRole('button', {name: 'fixture-execution-001', exact: true}).click();
    await page.getByRole('heading', {name: '请求终态与上游尝试', exact: true}).waitFor();
    const executionRequest = trace.findLast(item => item.path === '/api/logs/attempts/explain');
    assert.equal(executionRequest.executionId, 'fixture-execution-001');
    result.operations.push({case: 'attempt-execution-id', request: executionRequest});
    operations.state.mode = 'stale';
    await evidence.getByRole('button', {name: '刷新证据', exact: true}).first().click(); await page.waitForTimeout(150);
    assert.equal(await evidence.getByRole('button', {name: 'fixture-execution-001', exact: true}).count(), 1);
    assert.match(await evidence.locator('[role=alert]').first().textContent(), /此前快照/);
    result.operations.push({case: 'stale-keeps-evidence-with-warning', passed: true});
    operations.state.mode = 'forbidden';
    await evidence.getByRole('button', {name: '刷新证据', exact: true}).first().click(); await page.waitForTimeout(150);
    assert.equal(await evidence.getByRole('button', {name: 'fixture-execution-001', exact: true}).count(), 0);
    assert.match(await evidence.locator('[role=alert]').textContent(), /没有读取权限/);
    result.operations.push({case: '403-hides-retained-evidence', passed: true});
    operations.state.mode = 'unavailable';
    await evidence.getByRole('button', {name: '刷新证据', exact: true}).first().click(); await page.waitForTimeout(150);
    assert.match(await evidence.locator('[role=alert]').textContent(), /当前不可用/);
    result.operations.push({case: '501-explicitly-unavailable', passed: true});
    operations.state.mode = 'success';
    await page.goto(origin + '/stats', {waitUntil: 'networkidle'});
    assert.equal(await page.getByRole('heading', {name: '运行结果证据', exact: true}).count(), 0);
    assert.equal(await page.getByText('上游官方统计', {exact: true}).count(), 0);
    assert.equal(await page.getByRole('columnheader', {name: '缓存命中率', exact: true}).count(), 2);
    assert.equal(await page.getByRole('columnheader', {name: '输出 tok/s', exact: true}).count(), 2);
    assert.equal(await page.getByRole('cell', {name: '94%', exact: true}).count(), 2);
    assert.equal(await page.getByRole('cell', {name: '37.5', exact: true}).count(), 2);
    assert.match(await page.locator('main').innerText(), /已删除密钥的历史用量仍计入总量/);
    assert.equal(trace.some(item => item.path === '/api/stats/upstream' || item.path === '/api/stats/outcomes'), false);
    result.operations.push({case: 'usage-page-current-keys-cache-output-no-diagnostics', passed: true});
    await page.getByRole('tab', {name: 'Qoder', exact: true}).click(); await page.waitForTimeout(250);
    await page.goto(origin + '/logs', {waitUntil: 'networkidle'});
    const outcomeRequest = trace.findLast(item => item.path === '/api/logs/outcomes');
    assert.equal(outcomeRequest.platform, 'workbuddy'); assert.equal(outcomeRequest.queryPlatform, null); assert.equal(outcomeRequest.upstreamPlatform, 'qoder');
    result.operations.push({case: 'qtg-ledger-not-rerouted', request: outcomeRequest});
    await page.getByRole('tab', {name: 'WorkBuddy', exact: true}).click(); await page.waitForTimeout(250);
    await page.goto(origin + '/settings/system', {waitUntil: 'networkidle'});
    await page.getByText('启动时配置快照', {exact: true}).click();
    const configurationText = await page.locator('main').textContent();
    assert.doesNotMatch(configurationText, /synthetic-secret-must-not-render/);
    assert.match(configurationText, /\[redacted\]/); assert.match(configurationText, /process_only/);
    result.operations.push({case: 'configuration-four-sources-secret-redaction', passed: true});
    for (const route of ['/logs', '/stats', '/settings/system']) {
      for (const width of [320, 390, 768, 1440]) {
        await page.setViewportSize({width, height: 900}); await page.goto(origin + route, {waitUntil: 'networkidle'});
        const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
        assert.equal(scrollWidth, width, `${route} overflow at ${width}`);
        result.operations.push({case: 'responsive', route, width, scrollWidth});
        if (width === 320 || width === 1440) await page.screenshot({path: path.join(output, `operations-${route.replaceAll('/', '-')}-${width}.png`), fullPage: true});
      }
    }
    fixtureRole = 'viewer';
    await page.evaluate(() => sessionStorage.removeItem('wb-me'));
    const beforeViewer = trace.length;
    await page.goto(origin + '/settings/system', {waitUntil: 'networkidle'});
    const viewerConfigurationReads = trace.slice(beforeViewer).filter(item => ['/api/system/configuration', '/api/system/config'].includes(item.path));
    assert.equal(viewerConfigurationReads.length, 0);
    result.operations.push({case: 'viewer-no-admin-configuration-reads', passed: true});
    fixtureRole = 'admin'; await page.evaluate(() => sessionStorage.removeItem('wb-me'));
    await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
    for (const platform of ['Qoder', 'Trae', 'WorkBuddy']) {
      await page.getByRole('tab', {name: platform, exact: true}).click();
      await page.waitForTimeout(250);
      for (const realm of ['国际版', '国内版']) {
        const start = trace.length;
        await page.getByRole('tab', {name: realm, exact: true}).click(); await page.waitForTimeout(180);
        const requests = trace.slice(start);
        const selected = await page.getByRole('tab', {name: platform, exact: true}).getAttribute('aria-selected');
        assert.equal(selected, 'true');
        const summary = requests.find(item => item.path === '/api/stats/summary');
        assert.equal(summary?.platform, platform.toLowerCase());
        assert.equal(summary?.queryPlatform, platform.toLowerCase());
        assert.equal(summary?.realm, realm === '国际版' ? 'global' : 'cn');
        result.contextSwitches.push({platform, realm, requests, selected});
      }
      if (platform !== 'WorkBuddy') {
        for (const route of ['/tasks', '/red-packets']) {
          await page.goto(origin + route, {waitUntil: 'networkidle'});
          const pathname = new URL(page.url()).pathname;
          assert.equal(pathname.replace(/\/$/, ''), '/dashboard');
          result.guards.push({platform, route, path: pathname});
        }
      }
    }
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 900});
      await page.reload({waitUntil: 'networkidle'});
      await page.waitForTimeout(250);
      const facts = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth, tables: document.querySelectorAll('details table').length, skipDisplay: getComputedStyle(document.querySelector('.skip-link')).display, nav: document.querySelectorAll('[role=navigation]').length}));
      result.responsive.push(facts);
      assert.equal(facts.scrollWidth, width, `Dashboard overflow at ${width}`);
      const brandBounds = await page.locator('[data-slot=console-brand]').boundingBox();
      const controlsBounds = await page.locator('.zh-console-controls').boundingBox();
      assert.ok(brandBounds && brandBounds.x >= 0 && brandBounds.x + brandBounds.width <= width);
      assert.ok(controlsBounds && controlsBounds.x >= 0 && controlsBounds.x + controlsBounds.width <= width);
      assert.ok(brandBounds.y + brandBounds.height <= controlsBounds.y + 1 || brandBounds.x + brandBounds.width <= controlsBounds.x,
        'Product brand must not overlap controls');
      await page.screenshot({path: path.join(output, `dashboard-${width}.png`), fullPage: true, animations: 'disabled'});
    }
    await page.keyboard.press('Tab');
    result.firstFocus = await page.evaluate(() => ({className: document.activeElement.className, text: document.activeElement.textContent}));
    await page.keyboard.press('Enter');
    result.skipTarget = await page.evaluate(() => ({id: document.activeElement.id, hash: location.hash}));
    assert.equal(result.firstFocus.className, 'skip-link');
    assert.equal(result.skipTarget.id, 'main-content');
    await page.locator('[data-slot=command-palette-trigger]').click();
    await page.locator('[data-slot=command-palette-input]').fill('用量');
    result.palette = {matches: await page.locator('[data-slot=command-palette-item]').count()};
    assert.ok(result.palette.matches > 0);
    await page.locator('[data-slot=command-palette-item]').first().click();
    await page.waitForURL(/\/stats\/?$/); await page.waitForLoadState('networkidle');
    assert.equal(await page.getByRole('heading', {level: 1}).count(), 1);
    await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
    await page.keyboard.press('Escape');
    const dock = page.locator('[data-slot=dock-root]');
    await page.mouse.move(10, 10); await page.waitForTimeout(300);
    const beforeHover = await dock.boundingBox();
    await dock.locator('a[href^="/accounts"]:visible').hover(); await page.waitForTimeout(500);
    const afterHover = await dock.boundingBox();
    assert.ok(afterHover.width > beforeHover.width + 20, 'Dock hover magnification is retained');
    await page.mouse.move(10, 10); await page.waitForTimeout(400);
    const beforeDrag = await dock.boundingBox();
    await page.evaluate(() => {
      window.__dockEvents = [];
      for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) window.addEventListener(type, event => {
        if (window.__dockEvents.length < 80) window.__dockEvents.push({type, x: event.clientX, y: event.clientY, target: event.target?.nodeName, style: document.querySelector('[data-slot=dock-root]')?.getAttribute('style')});
      });
    });
    await page.mouse.move(beforeDrag.x + 5, beforeDrag.y + 5); await page.mouse.down();
    await page.waitForTimeout(500); await page.mouse.move(beforeDrag.x + 5, beforeDrag.y - 145, {steps: 10}); await page.mouse.up();
    await page.waitForTimeout(200);
    const afterDrag = await dock.boundingBox();
    const dragEvents = await page.evaluate(() => window.__dockEvents);
    await page.reload({waitUntil: 'networkidle'}); await page.waitForTimeout(200);
    const restored = await dock.boundingBox();
    assert.ok(afterDrag.y < beforeDrag.y - 100, 'Custom Dock drag must follow the pointer upward');
    assert.ok(Math.abs(afterDrag.x - beforeDrag.x) < 5, 'Native drag cancellation must not jump the Dock left');
    assert.ok(Math.abs(restored.y - afterDrag.y) < 5, 'Dock drag position must survive reload');
    await page.locator('[data-slot=dock-profile-trigger]:visible').click();
    await page.locator('[data-slot=dock-mode-toggle]').click();
    const pinnedStorage = await page.evaluate(() => localStorage.getItem('workbuddy-manager:dock-mode'));
    await page.keyboard.press('Escape'); await page.waitForTimeout(200);
    result.dock = {beforeHover, afterHover, beforeDrag, afterDrag, restored, pinnedStorage, pinned: await dock.boundingBox(), dragEvents};
    await page.locator('[data-slot=dock-profile-trigger]:visible').click();
    await page.locator('[data-slot=theme-toggle]').click();
    await page.keyboard.press('Escape');
    await page.mouse.move(10, 10); await page.waitForTimeout(500);
    result.theme = await page.evaluate(() => ({dark: document.documentElement.classList.contains('dark'), preference: localStorage.getItem('theme')}));
    assert.equal(result.theme.dark, true);
    assert.equal(await page.locator('[data-slot=console-brand]').isVisible(), true);
    assert.equal(await page.locator('.zh-console-brand-mark').evaluate(el => getComputedStyle(el).color), 'rgb(145, 173, 255)');
    await page.screenshot({path: path.join(output, 'dashboard-dark-1440.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 900}); await page.reload({waitUntil: 'networkidle'});
    await page.locator('[data-slot=dock-mobile-toggle]').click(); await page.waitForTimeout(800);
    result.mobileDock = {expanded: await page.locator('[data-slot=dock-mobile-toggle]').getAttribute('aria-expanded'), visibleLinks: await dock.locator('a:visible').count()};
    assert.equal(result.mobileDock.expanded, 'true');
    assert.ok(result.mobileDock.visibleLinks > 0);
    await page.screenshot({path: path.join(output, 'dashboard-dark-mobile-menu-390.png'), fullPage: true});
    await page.locator('[data-slot=dock-mobile-toggle]').click();
    await page.setViewportSize({width: 1440, height: 900});
    await page.locator('[data-slot=dock-profile-trigger]:visible').click();
    await page.getByRole('button', {name: '退出登录', exact: true}).click();
    await page.getByRole('alertdialog').getByRole('button', {name: '退出登录', exact: true}).click();
    await page.waitForTimeout(250);
    result.logoutFailure = await page.evaluate(() => ({path: location.pathname, cachedIdentity: !!sessionStorage.getItem('wb-me'), dialog: !!document.querySelector('[role=alertdialog]'), error: document.querySelector('[role=alertdialog] [role=alert]')?.textContent}));
    assert.equal(result.logoutFailure.cachedIdentity, true);
    assert.equal(result.logoutFailure.dialog, true);
    assert.match(result.logoutFailure.error, /Simulated logout failure/);
    logoutMode = 'success';
    await page.getByRole('alertdialog').getByRole('button', {name: '退出登录', exact: true}).click();
    await page.waitForURL(/\/login/); await page.waitForLoadState('networkidle');
    result.logoutSuccess = await page.evaluate(() => ({path: location.pathname, cachedIdentity: !!sessionStorage.getItem('wb-me')}));
    assert.equal(result.logoutSuccess.cachedIdentity, false);
    result.loginLanguage = [];
    for (const locale of ['en', 'ja', 'ko', 'zh-TW', 'zh-CN']) {
      await page.locator('[data-slot=language-toggle]').click();
      const labels = {'en': 'English', 'ja': '日本語', 'ko': '한국어', 'zh-TW': '繁體中文', 'zh-CN': '简体中文'};
      await page.getByRole('option', {name: labels[locale], exact: true}).click();
      await page.waitForFunction(value => document.documentElement.lang === value, locale);
      const facts = await page.evaluate(() => ({lang: document.documentElement.lang,
        stored: localStorage.getItem('workbuddy-manager:locale'), overflow: document.documentElement.scrollWidth > innerWidth}));
      assert.equal(facts.stored, locale);
      assert.equal(facts.overflow, false);
      result.loginLanguage.push(facts);
    }
    await page.screenshot({path: path.join(output, 'login-zh-CN.png'), fullPage: true, animations: 'disabled'});
    await page.goto(origin + '/claim?code=local-fixture', {waitUntil: 'networkidle'});
    result.publicClaim = {url: page.url(), fixtureShown: await page.getByText('Local claim fixture').count()};
    assert.equal(result.publicClaim.fixtureShown, 1);
    authenticated = true;
    for (const locale of ['en', 'ja', 'ko', 'zh-TW', 'zh-CN']) {
      await page.evaluate(value => {localStorage.setItem('workbuddy-manager:locale', value); localStorage.setItem('qtg.platform', 'trae'); localStorage.setItem('wb.realm', 'global');}, locale);
      errors = [];
      await page.goto(origin + '/stats?range=7d', {waitUntil: 'networkidle'});
      result.hydration.push({locale, savedPlatform: 'trae', savedRealm: 'global', lang: await page.locator('html').getAttribute('lang'), errors: [...errors]});
    }
    result.unknownFixtures = [...unknown];
    result.missingStatic = [...missingStatic];
    result.requestTrace = trace;
    result.errors = allErrors;
    result.runtimeConsoleErrors = runtimeConsoleErrors;
    fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(result, null, 2));
    console.log('COMPLETE', JSON.stringify({errors: result.errors.length, responsive: result.responsive}));
    assert.equal(allErrors.length, 0, 'Browser acceptance must not report page errors');
    assert.equal(runtimeConsoleErrors.length, 0, 'Browser acceptance must not log rendering errors');
    assert.deepEqual([...unknown], [], 'Every requested API must have an explicit fixture');
    assert.deepEqual([...missingStatic], [], 'Exported navigation must not request missing static files');
  } catch (error) {
    result.testFailure = {message: error.message, stack: error.stack};
    fs.writeFileSync(path.join(output, 'results-partial.json'), JSON.stringify(result, null, 2));
    throw error;
  } finally {
    await browser.close(); await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
