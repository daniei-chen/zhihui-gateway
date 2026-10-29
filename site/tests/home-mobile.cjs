const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const engines = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '../home');
const output = path.resolve(process.env.HOME_QA_OUTPUT || 'evidence/home-mobile');
fs.mkdirSync(output, {recursive: true});
const report = {
  testedHomeSha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'index.html'))).digest('hex'),
  productionConnected: Boolean(process.env.HOME_QA_ORIGIN),
  productionAuthenticated: false,
  businessRequestsAllowed: false,
  checks: [], errors: [], blockedRequests: [],
};
const server = http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const file = path.resolve(root, '.' + (name === '/' ? '/index.html' : name));
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file) || !fs.statSync(file).isFile()) {
    res.writeHead(404).end(); return;
  }
  res.setHeader('Content-Type', file.endsWith('.html') ? 'text/html; charset=utf-8' : file.endsWith('.jpg') ? 'image/jpeg' : 'application/octet-stream');
  fs.createReadStream(file).pipe(res);
});

async function entryState(page) {
  return page.evaluate(() => {
    const title = document.querySelector('#hero-title');
    const link = document.querySelector('.hero-copy a[href="/dashboard/"]');
    const painted = el => {
      for (let node = el; node instanceof Element; node = node.parentElement) {
        const css = getComputedStyle(node);
        if (Number(css.opacity) < .99 || css.visibility !== 'visible' || css.display === 'none') return false;
      }
      return true;
    };
    const box = link.getBoundingClientRect();
    const target = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
    return {
      titlePainted: painted(title), entryPainted: painted(link),
      entryInViewport: box.top >= 0 && box.bottom <= innerHeight,
      entryHitTarget: target === link || link.contains(target),
      entryHref: link.getAttribute('href'),
      entryOpacity: getComputedStyle(link.parentElement).opacity,
      width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
      motionPaused: document.documentElement.hasAttribute('data-home-flow-paused'),
    };
  });
}

async function assertEntry(page, label) {
  const state = await entryState(page);
  report.checks.push({label, ...state});
  assert.equal(state.titlePainted, true, label + ': title must actually be painted, not merely in layout');
  assert.equal(state.entryPainted, true, label + ': entry must not be transparent');
  assert.equal(state.entryInViewport, true, label + ': entry must fit above the fold');
  assert.equal(state.entryHitTarget, true, label + ': entry must receive a tap');
  assert.equal(state.entryHref, '/dashboard/');
  assert.ok(state.scrollWidth <= state.width, label + ': horizontal overflow');
}

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = process.env.HOME_QA_ORIGIN || `http://127.0.0.1:${server.address().port}`;
  try {
    for (const name of (process.env.HOME_QA_ENGINES || 'chromium,webkit').split(',')) {
      const browser = await engines[name].launch({headless: true});
      try {
        for (const width of [320, 390, 430]) {
          const context = await browser.newContext({viewport: {width, height: 844}, hasTouch: true, isMobile: true, reducedMotion: 'no-preference'});
          await context.route('**/*', route => {
            const url = new URL(route.request().url());
            if (url.origin !== origin || /^\/(api|v1|qoder|trae|wb)\//.test(url.pathname)) {
              report.blockedRequests.push(url.pathname); return route.abort();
            }
            if (url.pathname === '/dashboard/') return route.fulfill({contentType: 'text/html', body: '<!doctype html><h1 id="entry-destination">Console destination fixture</h1>'});
            return route.continue();
          });
          const page = await context.newPage();
          page.on('pageerror', error => report.errors.push(name + ': ' + error.message));
          const response = await page.goto(origin, {waitUntil: 'load'});
          assert.equal(response.status(), 200);
          assert.equal(crypto.createHash('sha256').update(await response.body()).digest('hex'), report.testedHomeSha256);
          await page.waitForTimeout(150);
          await page.screenshot({path: path.join(output, `${name}-${width}-first-paint.png`)});
          await assertEntry(page, `${name}-${width}-first-paint`);
          await page.waitForTimeout(1200);
          await assertEntry(page, `${name}-${width}-settled`);
          await page.locator('.hero-copy a[href="/dashboard/"]').tap();
          await page.waitForURL('**/dashboard/');
          assert.equal(new URL(page.url()).pathname, '/dashboard/');
          assert.equal(await page.locator('#entry-destination').count(), 1);
          await page.goBack({waitUntil: 'load'});
          await page.waitForTimeout(150);
          await assertEntry(page, `${name}-${width}-history-return`);
          await page.locator('#mobileTrigger').tap();
          assert.equal(await page.locator('#mobileTrigger').getAttribute('aria-expanded'), 'true');
          await page.locator('#mobileMenu a[href="/dashboard/"]').tap();
          await page.waitForURL('**/dashboard/');
          assert.equal(new URL(page.url()).pathname, '/dashboard/');
          await page.goto(origin, {waitUntil: 'load'});
          await page.emulateMedia({reducedMotion: 'reduce'});
          await assertEntry(page, `${name}-${width}-reduced-motion`);
          await context.close();
        }
        const noJs = await browser.newContext({viewport: {width: 390, height: 844}, hasTouch: true, isMobile: true, javaScriptEnabled: false});
        const page = await noJs.newPage();
        await page.goto(origin, {waitUntil: 'load'});
        await assertEntry(page, `${name}-no-javascript`);
        await noJs.close();
      } finally {await browser.close();}
    }
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.blockedRequests, []);
    report.status = 'passed';
  } catch (error) {report.status = 'failed'; report.failure = error.stack; throw error;}
  finally {
    fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
    await new Promise(resolve => server.close(resolve));
  }
  console.log(JSON.stringify({status: report.status, checks: report.checks.length, errors: report.errors.length}));
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
