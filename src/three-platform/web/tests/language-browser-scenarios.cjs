'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const labels = {'zh-CN': '\u7b80\u4f53\u4e2d\u6587', 'zh-TW': '\u7e41\u9ad4\u4e2d\u6587',
  en: 'English', ja: '\u65e5\u672c\u8a9e', ko: '\ud55c\uad6d\uc5b4'};
const languageKey = 'workbuddy-manager:locale';

async function selected(page, locale) {
  await page.waitForFunction(value => document.documentElement.lang === value, locale);
  const trigger = page.locator('[data-slot=language-toggle]');
  await trigger.waitFor({state: 'visible'});
  assert.equal(await trigger.getAttribute('data-locale'), locale);
  assert.ok((await trigger.innerText()).includes(labels[locale]));
  assert.equal(await page.locator('[data-slot=console-brand]').isVisible(), true);
  assert.equal(await page.locator('.zh-console-brand-name').textContent(), '智汇网关');
}

async function choose(page, locale) {
  await page.locator('[data-slot=language-toggle]').click();
  await page.getByRole('option', {name: labels[locale], exact: true}).click();
  await selected(page, locale);
}

async function run(context, page, origin, output, built) {
  const results = [];
  await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
  await choose(page, 'zh-CN');
  const chineseTitle = await page.locator('h1').innerText();
  for (const locale of ['en', 'ja', 'ko', 'zh-TW', 'zh-CN']) {
    await choose(page, locale);
    assert.equal(await page.evaluate(key => localStorage.getItem(key), languageKey), locale);
    await page.waitForTimeout(150);
    const title = await page.locator('h1').innerText();
    if (locale === 'en') assert.notEqual(title, chineseTitle);
    await page.reload({waitUntil: 'networkidle'});
    await selected(page, locale);
    await page.goto(origin + '/stats', {waitUntil: 'networkidle'});
    await selected(page, locale);
    results.push({kind: 'select-reload-navigation', locale, title, passed: true});
  }
  const trigger = page.locator('[data-slot=language-toggle]');
  await trigger.focus();
  await page.keyboard.press('Space');
  await page.getByRole('listbox').waitFor({state: 'visible'});
  assert.equal(await page.getByRole('option').count(), 5);
  await page.keyboard.press('Escape');
  await page.waitForFunction(() => document.activeElement?.getAttribute('data-slot') === 'language-toggle');
  assert.equal(await trigger.evaluate(node => node === document.activeElement), true);
  await page.keyboard.press('ArrowDown');
  await page.getByRole('listbox').waitFor({state: 'visible'});
  await page.keyboard.press('Home');
  await page.waitForFunction(value => document.activeElement?.textContent?.includes(value), labels['zh-CN']);
  await page.keyboard.press('ArrowDown');
  await page.waitForFunction(value => document.activeElement?.textContent?.includes(value), labels['zh-TW']);
  await page.keyboard.press('ArrowDown');
  await page.waitForFunction(value => document.activeElement?.textContent?.includes(value), labels.en);
  await page.keyboard.press('Enter');
  await selected(page, 'en');
  await page.waitForFunction(() => document.activeElement?.getAttribute('data-slot') === 'language-toggle');
  assert.equal(await trigger.evaluate(node => node === document.activeElement), true);
  results.push({kind: 'keyboard-space-escape-arrow-home-enter-focus', passed: true});

  for (const theme of ['light', 'dark']) {
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({width, height: 960});
      await page.evaluate(value => localStorage.setItem('theme', value), theme);
      await page.reload({waitUntil: 'networkidle'});
      await choose(page, 'en');
      await trigger.click();
      const box = await page.getByRole('listbox').boundingBox();
      const layout = await page.evaluate(() => ({width: innerWidth,
        scrollWidth: document.documentElement.scrollWidth, dark: document.documentElement.classList.contains('dark')}));
      assert.equal(layout.width, layout.scrollWidth);
      assert.equal(layout.dark, theme === 'dark');
      assert.ok(box.x >= -1 && box.x + box.width <= width + 1);
      await page.screenshot({path: path.join(output, `language-menu-${theme}-${width}.png`), fullPage: false});
      await page.keyboard.press('Escape');
      results.push({kind: 'responsive-menu', theme, width, layout, menu: box, passed: true});
    }
  }
  for (const platform of ['workbuddy', 'qoder', 'trae']) {
    for (const realm of ['cn', 'global']) {
      await page.evaluate(({platform, realm}) => {
        localStorage.setItem('qtg.platform', platform);
        localStorage.setItem('wb.realm', realm);
      }, {platform, realm});
      await page.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
      await choose(page, 'ja');
      assert.equal(await page.evaluate(() => localStorage.getItem('qtg.platform')), platform);
      assert.equal(await page.evaluate(() => localStorage.getItem('wb.realm')), realm);
      results.push({kind: 'platform-realm-preserved', platform, realm, passed: true});
    }
  }

  // Delay only the lazy English dictionary in a fresh browser context.
  const chunks = path.join(built, '_next/static/chunks');
  const english = fs.readdirSync(chunks).filter(name => name.endsWith('.js') &&
    fs.readFileSync(path.join(chunks, name), 'utf8').includes('"switch":"Switch language"'));
  assert.equal(english.length, 1, 'Exactly one dictionary chunk is required for the controlled race');
  const race = await context.browser().newContext({locale: 'en', viewport: {width: 390, height: 960}});
  let release, delayed = false;
  const pause = new Promise(done => { release = done; });
  await race.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (url.pathname.endsWith('/' + english[0])) { delayed = true; await pause; }
    return route.continue();
  });
  await race.addInitScript(() => localStorage.setItem('workbuddy-manager:dock-tip-dismissed', 'true'));
  const racePage = await race.newPage();
  const raceErrors = [];
  racePage.on('pageerror', error => raceErrors.push(error.message));
  try {
    await racePage.goto(origin + '/dashboard', {waitUntil: 'domcontentloaded'});
    await racePage.locator('[data-slot=language-toggle]').waitFor({state: 'visible'});
    await racePage.waitForFunction(() => !!document.querySelector('h1'));
    for (let i = 0; i < 100 && !delayed; i++) await racePage.waitForTimeout(20);
    assert.equal(delayed, true);
    await choose(racePage, 'ja');
    release();
    await racePage.waitForLoadState('networkidle');
    await racePage.waitForTimeout(200);
    await selected(racePage, 'ja');
    assert.equal(await racePage.evaluate(key => localStorage.getItem(key), languageKey), 'ja');
    assert.deepEqual(raceErrors, []);
    results.push({kind: 'real-browser-delayed-startup-does-not-override-selection', passed: true});
  } finally { release(); await race.close(); }

  const blocked = await context.browser().newContext({locale: 'zh-CN'});
  await blocked.route('**/*', route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  await blocked.addInitScript(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function(key, value) {
      if (key === 'workbuddy-manager:locale') throw new DOMException('Denied', 'SecurityError');
      return original.call(this, key, value);
    };
  });
  const blockedPage = await blocked.newPage();
  try {
    await blockedPage.goto(origin + '/dashboard', {waitUntil: 'networkidle'});
    await choose(blockedPage, 'ko');
    await blockedPage.goto(origin + '/stats', {waitUntil: 'networkidle'});
    // Full navigation cannot persist a deliberately denied write; current-page switching remains usable.
    await choose(blockedPage, 'en');
    results.push({kind: 'storage-denied-switch-remains-usable', passed: true});
  } finally { await blocked.close(); }
  return results;
}

module.exports = {run};
