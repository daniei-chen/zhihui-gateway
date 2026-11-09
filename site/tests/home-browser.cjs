const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const crypto = require('node:crypto');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '../home');
const output = process.env.HOME_QA_OUTPUT ? path.resolve(process.env.HOME_QA_OUTPUT) : fs.mkdtempSync(path.join(os.tmpdir(), 'zhihui-home-qa-'));
fs.mkdirSync(output, {recursive: true});
const testedHomeSha256 = crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'index.html'))).digest('hex');
const report = {productionConnected: false, testedHomeSha256, errors: [], blockedExternal: [], responsive: []};
const server = http.createServer((req, res) => {
  const name = decodeURIComponent(new URL(req.url, 'http://localhost').pathname);
  const file = path.join(root, name === '/' ? 'index.html' : name);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) {res.writeHead(404).end(); return;}
  res.setHeader('Content-Type', file.endsWith('.html') ? 'text/html; charset=utf-8' : file.endsWith('.svg') ? 'image/svg+xml' : file.endsWith('.jpg') ? 'image/jpeg' : 'application/octet-stream');
  fs.createReadStream(file).pipe(res);
});
(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
    await context.route('**/*', route => {
      if (new URL(route.request().url()).origin !== origin) {report.blockedExternal.push(route.request().url()); return route.abort();}
      return route.continue();
    });
    const page = await context.newPage();
    page.on('pageerror', error => report.errors.push(error.message));
    await page.goto(origin, {waitUntil: 'networkidle'});
    await page.waitForTimeout(1100);
    const title = page.locator('#hero-title .muted');
    assert.equal(await title.evaluate(el => getComputedStyle(el).color), 'rgb(32, 42, 61)');
    assert.equal(await title.evaluate(el => getComputedStyle(el).backgroundImage), 'none');
    assert.doesNotMatch(await page.locator('body').innerText(), /3\.0|仅供本人及获授权用户使用|交互式概念演示/);
    assert.ok(await title.evaluate(el => parseFloat(getComputedStyle(el.parentElement).fontSize) <= 48));
    assert.equal(await page.locator('.hero-copy .btn-dark').getAttribute('href'), '/dashboard/');
    assert.match(await page.locator('.hero-copy .btn-dark').innerText(), /进入控制台/);
    assert.equal(await page.locator('.nav-actions a[href="/dashboard/"]').count(), 0);
    assert.ok(await page.locator('.prism-float').isVisible());
    assert.ok(await page.locator('.demo-browser').isVisible());
    await page.locator('.console-capture img').evaluate(el => el.decode());
    assert.equal(await page.locator('.console-capture img').evaluate(el => el.naturalWidth), 1440);
    const chapters = ['product', 'platforms', 'routing', 'ledger', 'about', 'console', 'setup', 'engineering', 'boundary', 'faq'];
    assert.deepEqual(await page.locator('section[id]').evaluateAll(items => items.map(el => el.id)), chapters);
    await page.waitForFunction(() => document.querySelector('canvas').dataset.flowState === 'running');
    report.particles = await page.locator('canvas').evaluate(el => ({count: Number(el.dataset.particleCount), state: el.dataset.flowState, width: el.width, height: el.height}));
    assert.ok(report.particles.count >= 150 && report.particles.count <= 380);
    await page.mouse.move(210, 350);
    await page.waitForTimeout(80);
    report.pointer = await page.locator('.pointer-aura').evaluate(el => ({transform: el.style.transform, active: document.documentElement.hasAttribute('data-blue-pointer')}));
    assert.ok(report.pointer.active);
    assert.match(report.pointer.transform, /translate3d/);
    await page.screenshot({path: path.join(output, 'home-desktop.png'), fullPage: true});
    await page.screenshot({path: path.join(output, 'home-hero.png')});
    for (const platform of ['qoder', 'trae', 'workbuddy']) {
      await page.locator(`[data-hero-platform="${platform}"]`).click();
      assert.equal(await page.locator(`[data-hero-platform="${platform}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator(`[data-platform="${platform}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator(`[data-route="${platform}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator(`[data-code="${platform}"]`).getAttribute('aria-pressed'), 'true');
      assert.equal(await page.locator(`[data-signal-route="${platform}"]`).getAttribute('class'), 'is-selected');
    }
    await page.locator('#zh-motion-control').click();
    assert.equal(await page.locator('canvas').getAttribute('data-flow-state'), 'paused');
    await page.locator('#zh-motion-control').click();
    await page.waitForFunction(() => document.querySelector('canvas').dataset.flowState === 'running');
    await page.emulateMedia({reducedMotion: 'reduce'});
    await page.waitForTimeout(80);
    assert.equal(await page.locator('canvas').getAttribute('data-flow-state'), 'paused');
    assert.ok(await page.locator('#zh-motion-control').isHidden());
    assert.equal(await title.evaluate(el => getComputedStyle(el).color), 'rgb(32, 42, 61)');
    await page.emulateMedia({reducedMotion: 'no-preference'});
    await page.waitForFunction(() => document.querySelector('canvas').dataset.flowState === 'running');
    const geometry = await page.locator('.routing-fabric').evaluate(el => {
      const core = el.querySelector('.signal-core').getBoundingClientRect();
      return {width: el.getBoundingClientRect().width, sources: Array.from(el.querySelectorAll('.signal-source'), button => {
        const r = button.getBoundingClientRect();
        return {x: r.x, right: r.right, y: r.y, bottom: r.bottom, coreRight: core.right};
      })};
    });
    assert.ok(geometry.width >= 400, 'Routing fabric must not shrink to its intrinsic text width');
    assert.ok(geometry.sources.every(button => button.x > button.coreRight), 'Platform buttons must stay clear of the routing core');
    report.routingGeometry = geometry;
    const quota = page.locator('.quota-fill');
    const fixed = await quota.evaluate(el => getComputedStyle(el).width);
    await page.locator('#product [data-blue-card]').last().hover();
    assert.equal(await quota.evaluate(el => getComputedStyle(el).width), fixed);
    assert.match(await page.locator('.quota-ribbon').innerText(), /非实时额度/);
    await page.locator('[data-ledger="unknown"]').click();
    assert.equal(await page.locator('#ledgerBadge').innerText(), 'UNKNOWN');
    assert.equal(await page.locator('.ledger-wrap').getAttribute('data-ledger-state'), 'unknown');
    assert.match(await page.locator('#ledgerDetail').innerText(), /不等于免费/);
    await page.locator('[data-ledger="settled"]').click();
    for (const panel of ['overview', 'accounts', 'keys', 'models', 'logs', 'tasks', 'security']) {
      await page.locator(`[data-console="${panel}"]`).click();
      assert.equal(await page.locator(`[data-panel="${panel}"]`).isVisible(), true);
      await page.locator(`[data-panel="${panel}"] .console-native-preview img`).evaluate(el => el.decode());
      assert.equal(await page.locator(`[data-panel="${panel}"] .console-native-preview img`).evaluate(el => el.naturalWidth), 1440);
    }
    await page.locator('[data-console="accounts"]').click();
    await page.locator('[data-zh3-detail="WorkBuddy"]').click();
    assert.equal(await page.locator('#zh3Inspector').getAttribute('aria-hidden'), 'false');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#zh3Inspector').getAttribute('aria-hidden'), 'true');
    await page.locator('[data-console="overview"]').click();
    await page.locator('[data-format="python"]').click();
    assert.match(await page.locator('#codeBlock').innerText(), /client\.chat\.completions/);
    await page.locator('[data-format="curl"]').click();
    await page.locator('.faq-question').last().click();
    assert.equal(await page.locator('.faq-question').last().getAttribute('aria-expanded'), 'true');
    await page.locator('.faq-question').last().click();
    await page.locator('[data-scenario="fallback"]').click();
    await page.waitForFunction(() => !document.getElementById('zh23Run').disabled, {timeout: 8000});
    assert.match(await page.locator('#zh23RouteStatus').innerText(), /示意|模拟|路径|上游/);
    await page.locator('[data-scenario="fresh"]').click();
    await page.waitForFunction(() => !document.getElementById('zh23Run').disabled, {timeout: 8000});
    const anchorIssues = await page.locator('a[href^="#"]').evaluateAll(links => links.filter(a => a.hash.length > 1 && !document.getElementById(decodeURIComponent(a.hash.slice(1)))).map(a => a.hash));
    assert.deepEqual(anchorIssues, []);
    const duplicateIds = await page.evaluate(() => {
      const ids = Array.from(document.querySelectorAll('[id]'), el => el.id);
      return ids.filter((id, index) => ids.indexOf(id) !== index);
    });
    assert.deepEqual(duplicateIds, []);
    report.interactions = ['hero-platforms', 'route-and-code-sync', 'ledger-unknown', 'seven-console-panels', 'account-inspector', 'python-curl', 'faq', 'route-scenario', 'anchor-closure', 'unique-ids'];
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 950});
      await page.goto(origin, {waitUntil: 'networkidle'});
      await page.waitForTimeout(300);
      const facts = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth, titleColor: getComputedStyle(document.querySelector('#hero-title .muted')).color}));
      assert.ok(facts.scrollWidth <= width, `Horizontal overflow at ${width}`);
      assert.equal(facts.titleColor, 'rgb(32, 42, 61)');
      report.responsive.push(facts);
      if (width === 390) await page.screenshot({path: path.join(output, 'home-mobile-hero.png')});
      if ([390, 1440].includes(width)) {
        for (const section of await page.locator('section').all()) {
          await section.scrollIntoViewIfNeeded();
          await page.waitForTimeout(180);
        }
        await page.waitForTimeout(900);
        await page.evaluate(() => window.scrollTo({top: 0, behavior: 'instant'}));
        await page.screenshot({path: path.join(output, width === 390 ? 'home-mobile.png' : 'home-desktop.png'), fullPage: true});
        for (const id of ['product', 'platforms', 'routing', 'ledger', 'about', 'console', 'setup', 'engineering', 'boundary', 'faq']) {
          const section = page.locator('#' + id);
          await section.scrollIntoViewIfNeeded();
          await page.waitForTimeout(450);
          if (width === 1440) await section.screenshot({path: path.join(output, `chapter-${id}.png`)});
          assert.equal(await section.evaluate(el => el.classList.contains('chapter-inview')), true);
        }
      }
    }
    const touch = await browser.newContext({hasTouch: true, isMobile: true, viewport: {width: 390, height: 844}});
    const mobile = await touch.newPage();
    mobile.on('pageerror', error => report.errors.push(error.message));
    await mobile.goto(origin, {waitUntil: 'networkidle'});
    assert.equal(await mobile.locator('canvas').getAttribute('data-flow-state'), 'paused');
    assert.ok(await mobile.locator('#zh-motion-control').isHidden());
    await mobile.locator('#mobileTrigger').click();
    assert.equal(await mobile.locator('#mobileTrigger').getAttribute('aria-expanded'), 'true');
    await mobile.locator('#mobileMenu a[href="#product"]').click();
    assert.equal(await mobile.locator('#mobileTrigger').getAttribute('aria-expanded'), 'false');
    report.touchStaticFallback = true;
    await mobile.locator('.console-capture img').evaluate(el => el.decode());
    assert.equal(await mobile.locator('.console-capture img').evaluate(el => el.naturalWidth), 390);
    const noJsContext = await browser.newContext({javaScriptEnabled: false, viewport: {width: 390, height: 844}});
    const noJs = await noJsContext.newPage();
    await noJs.goto(origin);
    assert.ok(await noJs.locator('.hero-copy .btn-dark').isVisible());
    assert.equal(await noJs.locator('.hero-copy .btn-dark').getAttribute('href'), '/dashboard/');
    assert.equal(await noJs.locator('#zh-motion-control').isHidden(), true);
    assert.equal(await noJs.locator('#product .feature').first().evaluate(el => getComputedStyle(el).opacity), '1');
    report.noJsEntryReadable = true;
    assert.equal(crypto.createHash('sha256').update(fs.readFileSync(path.join(root, 'index.html'))).digest('hex'), testedHomeSha256);
    assert.deepEqual(report.errors, []);
    report.status = 'passed';
  } catch (error) {report.status = 'failed'; report.failure = error.stack; throw error;}
  finally {fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2)); await browser.close(); await new Promise(resolve => server.close(resolve));}
  console.log(JSON.stringify(report));
})().catch(error => {console.error(error); server.close(); process.exitCode = 1;});
