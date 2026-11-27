/* Local static-export acceptance with synthetic APIs; never opens production. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const zh = require('../lib/i18n/locales/zh-CN.json');

const root = path.resolve(__dirname, '..');
const output = process.env.QA_OUTPUT || fs.mkdtempSync(path.join(os.tmpdir(), 'zhihui-browser-'));
fs.mkdirSync(output, {recursive: true});
const deferred = () => {
  let resolve;
  const promise = new Promise(yes => {resolve = yes;});
  return {promise, resolve};
};
async function observed(signal) {
  let timer;
  try {
    return await Promise.race([signal.promise, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('expected synthetic request was not observed')), 15000);
    })]);
  } finally {clearTimeout(timer);}
}

// Route.fulfill only hands bytes to Playwright. Observe the browser's terminal
// event as well, before releasing any held response, so late replies cannot
// silently land after the assertion has already passed.
let heldSequence = 0;
function heldRequest(page, request, scenario) {
  const terminal = deferred(), fulfilled = deferred();
  const cleanup = () => {
    page.off('requestfinished', finished);
    page.off('requestfailed', failed);
  };
  const finished = candidate => {
    if (candidate !== request) return;
    cleanup(); terminal.resolve({state: 'received'});
  };
  const failed = candidate => {
    if (candidate !== request) return;
    cleanup(); terminal.resolve({state: 'failed', error: request.failure()?.errorText || ''});
  };
  page.on('requestfinished', finished);
  page.on('requestfailed', failed);
  return {id: 'held-' + (++heldSequence), scenario, request, terminal, fulfilled, cleanup};
}

async function fulfillHeld(route, held, response) {
  try {
    await route.fulfill({...response, headers: {'x-zhihui-acceptance': held.id}});
  } catch (error) {
    // Only a separately observed browser cancellation can justify this failure.
    held.fulfillError = error.message;
  } finally {held.fulfilled.resolve();}
}

async function rendered(page) {
  await observed({promise: page.evaluate(() => new Promise(resolve => {
    requestAnimationFrame(() => requestAnimationFrame(resolve));
  }))});
}

async function assertEnabled(locator) {
  await locator.waitFor();
  await locator.page().waitForFunction(element => !element.disabled, await locator.elementHandle());
  assert.equal(await locator.isEnabled(), true);
}

async function consumedHeld(page, held, result) {
  await observed(held.fulfilled);
  const terminal = await observed(held.terminal);
  const evidence = {scenario: held.scenario, url: held.request.url()};
  if (terminal.state === 'failed') {
    assert.match(terminal.error, /ERR_ABORTED|NS_BINDING_ABORTED|cancelled/i,
      'a failed request must be an observed client cancellation');
    await rendered(page);
    result.heldRequests.push({...evidence, terminal: 'cancelled', error: terminal.error});
    return 'cancelled';
  }
  assert.equal(held.fulfillError, undefined, 'a completed response must have been fulfilled successfully');
  const response = await held.request.response();
  assert.ok(response, 'requestfinished must have a response');
  assert.equal(await observed({promise: response.finished()}), null);
  // The injected observer records only after XHR loadend / fetch body reading
  // and two render frames. Network completion alone is not a DOM assertion.
  await page.waitForFunction(id => window.__zhihuiAcceptance.some(item => item.id === id), held.id);
  const receipt = await page.evaluate(id => window.__zhihuiAcceptance.find(item => item.id === id), held.id);
  assert.equal(receipt.status, response.status());
  result.heldRequests.push({...evidence, terminal: 'response_consumed', ...receipt});
  return 'response_consumed';
}

async function readyResponses(page, origin, paths, action) {
  const replies = paths.map(pathname => page.waitForResponse(response => {
    const url = new URL(response.url());
    return url.origin === origin && url.pathname === pathname;
  }));
  const [responses] = await Promise.all([Promise.all(replies), action()]);
  for (const response of responses) {
    assert.equal(response.status(), 200, 'required synthetic API failed: ' + response.url());
    assert.equal(await observed({promise: response.finished()}), null);
  }
  await rendered(page);
  return responses;
}
const server = spawn(process.execPath, [path.join(__dirname, 'browser-reconstruction.cjs')], {
  cwd: root, env: {...process.env, QA_SERVE_ONLY: '0', QA_OUTPUT: output}, stdio: ['ignore', 'pipe', 'pipe'],
});
const originReady = new Promise((resolve, reject) => {
  const timer = setTimeout(() => reject(new Error('synthetic server did not start')), 10000);
  let text = '';
  server.stdout.on('data', chunk => {
    text += chunk;
    const match = text.match(/Synthetic console: (http:\/\/127\.0\.0\.1:\d+)/);
    if (match) {clearTimeout(timer); resolve(match[1]);}
  });
  server.once('error', reject);
  server.once('exit', code => {clearTimeout(timer); reject(new Error('synthetic server exited: ' + code));});
});

(async () => {
  let browser;
  const held = [];
  const result = {scope: 'loopback static export with synthetic API responses', scenarios: [],
    heldRequests: [], httpFailures: [], blockedRequests: [], errors: []};
  try {
    const origin = await originReady;
    const executablePath = process.env.PLAYWRIGHT_CHROMIUM || undefined;
    browser = await chromium.launch({headless: true, executablePath});
    const context = await browser.newContext({locale: 'zh-CN', viewport: {width: 1440, height: 1000}, reducedMotion: 'reduce'});
    await context.addInitScript(() => {
      window.__zhihuiAcceptance = [];
      const receipt = (id, status, transport) => {
        if (!id) return;
        requestAnimationFrame(() => requestAnimationFrame(() => {
          window.__zhihuiAcceptance.push({id, status, transport});
        }));
      };
      // Axios currently uses XHR. Preserve native behavior and only observe the
      // explicitly tagged fixture responses after the app's loadend handlers.
      const nativeOpen = XMLHttpRequest.prototype.open;
      XMLHttpRequest.prototype.open = function(...args) {
        this.addEventListener('loadend', () => {
          receipt(this.getResponseHeader('x-zhihui-acceptance'), this.status, 'xhr');
        }, {once: true});
        return nativeOpen.apply(this, args);
      };
      // Retain the same body-consumption boundary if the API switches adapters.
      const nativeFetch = window.fetch;
      window.fetch = async function(...args) {
        const response = await nativeFetch.apply(this, args);
        const id = response.headers.get('x-zhihui-acceptance');
        if (id) for (const method of ['json', 'text']) {
          const read = response[method].bind(response);
          response[method] = async (...readArgs) => {
            const body = await read(...readArgs);
            receipt(id, response.status, 'fetch');
            return body;
          };
        }
        return response;
      };
    });
    const sameOrigin = route => {
      if (new URL(route.request().url()).origin === origin) return true;
      result.blockedRequests.push(route.request().url());
      return false;
    };
    await context.route('**/*', route => {
      if (!sameOrigin(route)) return route.abort();
      if (new URL(route.request().url()).pathname === '/api/platforms') {
        return route.fulfill({json: {platforms: [{id: 'workbuddy', name: 'WorkBuddy', upstream_id: 0,
          readonly: false, enabled: true, capabilities: {}, note: 'Synthetic active platform'}], total: 1}});
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.setDefaultTimeout(15000);
    page.on('pageerror', error => result.errors.push(error.message));
    const expectedHttpFailures = new WeakMap();
    page.on('response', response => {
      if (response.status() >= 400 && expectedHttpFailures.get(response.request()) !== response.status()) {
        result.httpFailures.push({url: response.url(), status: response.status()});
      }
    });
    const assertHealthy = () => {
      assert.deepEqual(result.errors, [], 'uncaught browser errors');
      assert.deepEqual(result.httpFailures, [], 'unexpected HTTP errors or missing static/API fixtures');
      assert.deepEqual(result.blockedRequests, [], 'browser attempted a non-loopback request');
    };
    const cfgEntered = deferred(), cfgRelease = deferred(), cfgRequests = [], apiCalls = [];
    await page.route('**/api/**', async route => {
      if (!sameOrigin(route)) return route.abort();
      const url = new URL(route.request().url());
      apiCalls.push(url.pathname);
      if (url.pathname === '/api/settings/upstream') {
        const request = heldRequest(page, route.request(), 'obsolete upstream configuration');
        held.push(request); cfgRequests.push(request);
        expectedHttpFailures.set(route.request(), 503);
        cfgEntered.resolve();
        await cfgRelease.promise;
        await fulfillHeld(route, request, {status: 503, json: {detail: 'Synthetic upstream unavailable'}});
      } else if (url.pathname === '/api/budgets/review') {
        await route.fulfill({json: {items: [], next_cursor: null, scanned_rows: 0,
          usage_unknown_is_zero: false, scope: 'main-supplier-usage-not-customer-wallet'}});
      } else if (url.pathname === '/api/logs/retention/status') {
        await route.fulfill({json: {enabled: false, sampled: false, ok: null,
          fresh: false, age_seconds: null, deleted_rows: 0}});
      } else await route.fallback();
    });
    await page.goto(origin + '/settings/upstream/');
    await observed(cfgEntered);
    await page.locator('a[href="/settings/users/"], a[href="/settings/users"]').first().click();
    await page.getByRole('table').waitFor();
    cfgRelease.resolve();
    await Promise.all(cfgRequests.map(request => consumedHeld(page, request, result)));
    assert.equal(await page.getByRole('table').isVisible(), true);
    assertHealthy();
    result.scenarios.push('users remain usable after obsolete upstream requests settle; terminals recorded separately');
    await readyResponses(page, origin, ['/api/settings/model-map'], () => page.goto(origin + '/settings/models/'));
    await page.getByText(zh.settings.mapNewAlias, {exact: true}).waitFor();
    await assertEnabled(page.getByPlaceholder('gpt-4o-mini', {exact: true}));
    assertHealthy();
    result.scenarios.push('model-map settings load independently of upstream configuration');
    const cfgCalls = apiCalls.filter(name => name === '/api/settings/upstream').length;
    const tabApis = {
      tokens: ['/api/tokens'], backup: ['/api/settings/pg-sync'],
      system: ['/api/system/versions', '/api/system/update-status', '/api/system/check-update',
        '/api/system/configuration', '/api/system/config', '/api/budgets/review', '/api/logs/retention/status'],
      about: [],
    };
    for (const tab of ['tokens', 'backup', 'system', 'about']) {
      const responses = await readyResponses(page, origin, tabApis[tab], () => page.goto(origin + '/settings/' + tab + '/'));
      if (tab === 'tokens') {
        await page.getByText(zh.settings.tokensNew, {exact: true}).waitFor();
        await page.getByText(zh.settings.tokensEmpty, {exact: true}).waitFor();
        await assertEnabled(page.locator('main').getByRole('textbox'));
      } else if (tab === 'backup') {
        await page.getByText(zh.pgSync.title, {exact: true}).waitFor();
        await assertEnabled(page.locator('#pg-host'));
        assert.equal(await page.locator('#pg-port').inputValue(), '5432');
      } else if (tab === 'system') {
        const releaseCenter = page.locator('[data-slot="zhihui-release-center"]');
        await releaseCenter.getByRole('heading', {name: zh.releaseCenter.title, exact: true}).waitFor();
        const versions = await responses.find(response => new URL(response.url()).pathname === '/api/system/versions').json();
        assert.equal(typeof versions.product.version, 'string');
        assert.ok(versions.product.version.length > 0);
        await releaseCenter.getByText(versions.product.version, {exact: true}).waitFor();
        await page.getByText(zh.budgetReview.empty, {exact: true}).waitFor();
        await page.getByText(zh.budgetReview.archiveDisabled, {exact: true}).waitFor();
      } else await page.getByText(zh.settings.aboutDesc, {exact: true}).waitFor();
      await page.waitForLoadState('networkidle');
      assertHealthy();
      assert.equal(apiCalls.filter(name => name === '/api/settings/upstream').length, cfgCalls);
    }
    await page.screenshot({path: path.join(output, 'independent-settings.png'), fullPage: false});
    result.scenarios.push('tokens, backup, system and about do not depend on the upstream endpoint');
    await page.unroute('**/api/**');

    const detailEntered = deferred(), delayedDetail = deferred(), detailRequests = [];
    const filterEntered = deferred(), delayedFilter = deferred(), filterRequests = [];
    const record = (id, model = 'model-' + id) => ({request_id: id, ts: 1793200000, status: 200, model,
      latency_ms: 10, size: 1, truncated: false});
    await page.route('**/api/traffic**', async route => {
      if (!sameOrigin(route)) return route.abort();
      const url = new URL(route.request().url());
      if (url.pathname === '/api/traffic/status') {
        return route.fulfill({json: {enabled: true, desired: true, effective: true,
          retention_hours: 24, max_records: 5000, limit_bytes: 1000, counter: {}, queued: 0, dir: 'synthetic'}});
      }
      if (url.pathname === '/api/traffic') {
        if (url.searchParams.get('status') === 'error') {
          const request = heldRequest(page, route.request(), 'obsolete traffic filter');
          held.push(request); filterRequests.push(request);
          filterEntered.resolve();
          await delayedFilter.promise;
          await fulfillHeld(route, request, {json: {enabled: true, records: [record('stale', 'stale-filter')]}});
          return;
        }
        const records = url.searchParams.get('status') === 'ok' ? [record('current')] : [record('A'), record('B')];
        return route.fulfill({json: {enabled: true, records}});
      }
      const id = url.pathname.split('/').at(-1);
      const payload = {json: {...record(id), path: '/v1/chat/completions',
        client: {marker: 'detail-' + id}, gateway: {}, response: {}, diff: []}};
      if (id === 'A') {
        const request = heldRequest(page, route.request(), 'closed traffic detail');
        held.push(request); detailRequests.push(request);
        detailEntered.resolve(); await delayedDetail.promise;
        await fulfillHeld(route, request, payload);
      } else await route.fulfill(payload);
    });
    await page.goto(origin + '/traffic/');
    await page.getByRole('row').filter({hasText: 'model-A'}).click();
    await page.getByRole('dialog').waitFor();
    await observed(detailEntered);
    await page.getByRole('button', {name: '关闭', exact: true}).click();
    await page.getByRole('dialog').waitFor({state: 'hidden'});
    await page.getByRole('row').filter({hasText: 'model-B'}).click();
    await page.getByRole('dialog').getByText(/detail-B/).waitFor();
    delayedDetail.resolve();
    const detailTerminals = await Promise.all(detailRequests.map(request => consumedHeld(page, request, result)));
    assert.ok(detailTerminals.every(terminal => terminal === 'cancelled'), 'closing detail must cancel its owned request');
    assert.doesNotMatch(await page.getByRole('dialog').innerText(), /detail-A/);
    assert.match(await page.getByRole('dialog').innerText(), /detail-B/);
    await page.screenshot({path: path.join(output, 'traffic-owned-detail.png'), fullPage: false});
    await page.getByRole('button', {name: '关闭', exact: true}).click();
    await page.getByRole('dialog').waitFor({state: 'hidden'});
    result.scenarios.push('closing a slow detail cancels its transport and preserves the next selection; non-cancellable races are covered by component tests');
    await page.getByRole('button', {name: '只看失败', exact: true}).click();
    await observed(filterEntered);
    await page.getByRole('button', {name: '只看成功', exact: true}).click();
    await page.getByRole('row').filter({hasText: 'model-current'}).waitFor();
    delayedFilter.resolve();
    await Promise.all(filterRequests.map(request => consumedHeld(page, request, result)));
    assert.doesNotMatch(await page.getByRole('table').innerText(), /stale-filter/);
    assert.match(await page.getByRole('table').innerText(), /model-current/);
    result.scenarios.push('obsolete filter requests settle before current rows are asserted; terminals recorded separately');
    assertHealthy();
    result.ok = true;
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify(result, null, 2));
    console.log(JSON.stringify(result));
  } catch (error) {
    result.ok = false;
    result.failure = error.message;
    const failedPage = browser?.contexts()[0]?.pages()[0];
    if (failedPage) {
      result.failedPage = failedPage.url();
      try {await failedPage.screenshot({path: path.join(output, 'failure.png')});}
      catch { /* Keep the original failure if the browser itself has exited. */ }
    }
    fs.writeFileSync(path.join(output, 'acceptance.json'), JSON.stringify(result, null, 2));
    throw error;
  } finally {
    for (const request of held) request.cleanup();
    try {if (browser) await browser.close();}
    finally {server.kill('SIGTERM');}
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
