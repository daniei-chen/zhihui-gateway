'use strict';
const assert = require('node:assert/strict');
const path = require('node:path');

exports.run = async function(context, page, origin, output) {
  const starts = [], polls = [], finishes = [], reviews = [], cases = [];
  let holdStart, delayStart = false, failFinish = false, reviewMode = 'empty';
  let holdReview, delayReview = false;
  const urls = {
    workbuddy: {cn: 'https://copilot.tencent.com/auth', global: 'https://www.workbuddy.ai/auth'},
    qoder: {cn: 'https://qoder.com.cn/device/selectAccounts', global: 'https://qoder.com/device/selectAccounts'},
    trae: {cn: 'https://www.trae.cn/authorization'},
  };
  await page.route('**/api/auth/**', async route => {
    const req = route.request(), url = new URL(req.url());
    const body = req.method() === 'POST' ? req.postDataJSON() : {};
    const platform = url.searchParams.get('platform');
    const realm = body.realm || url.searchParams.get('realm');
    assert.equal(req.headers()['x-qtg-platform'], platform);
    if (url.pathname === '/api/auth/review') {
      assert.equal(req.method(), 'POST');
      assert.equal(url.searchParams.has('state'), false);
      reviews.push({platform, realm, state: body.state});
      if (delayReview) await new Promise(resolve => { holdReview = resolve; });
      return route.fulfill({json: {platform, realm, reviewed_at: 1,
        sessions: reviewMode === 'empty' ? [] : [{reference: 'synthetic-session'.slice(-8), platform, realm,
          status: reviewMode, engine_binding: 'current', expires_at: 2000000000,
          requires_reconciliation: reviewMode === 'unconfirmed', uid: 'fixture', nickname: 'Synthetic account'}]}});
    }
    if (url.pathname === '/api/auth/start') {
      starts.push({platform, realm, body});
      assert.equal(body.platform, platform);
      if (platform !== 'workbuddy') {
        assert.equal(body.region, undefined); assert.equal(body.proxy, undefined);
        assert.equal(url.searchParams.has('upstream_id'), false);
      }
      if (delayStart) await new Promise(resolve => { holdStart = resolve; });
      return route.fulfill({json: {state: 'synthetic-session', platform, realm,
        mode: platform === 'trae' ? 'callback' : 'poll', authUrl: urls[platform]?.[realm]}});
    }
    if (url.pathname === '/api/auth/poll') {
      polls.push({platform, realm});
      return route.fulfill({json: {status: 'waiting', platform, realm}});
    }
    if (url.pathname === '/api/auth/finish') {
      finishes.push({platform, realm});
      return route.fulfill(failFinish ? {status: 502, json: {detail: 'Synthetic unconfirmed insertion'}} :
        {json: {status: 'success', platform, realm, uid: 'fixture', nickname: 'Synthetic account'}});
    }
    if (url.pathname === '/api/auth/cancel') return route.fulfill({json: {ok: true}});
    throw new Error('Unknown authorization route ' + url.pathname);
  });
  const setScope = async (platform, realm) => {
    await page.goto(origin + '/accounts', {waitUntil: 'networkidle'});
    await page.evaluate(({platform, realm}) => {
      localStorage.setItem('qtg.platform', platform);
      localStorage.setItem('wb.realm', realm);
    }, {platform, realm});
    await page.reload({waitUntil: 'networkidle'});
  };
  for (const platform of ['workbuddy', 'qoder', 'trae']) for (const realm of ['cn', 'global']) {
    await setScope(platform, realm);
    const before = starts.length;
    await page.getByRole('button', {name: '添加账号', exact: true}).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.waitFor();
    await page.waitForTimeout(300);
    assert.match(await dialog.innerText(), new RegExp(platform === 'qoder' ? 'Qoder' : platform === 'trae' ? 'Trae' : 'WorkBuddy'));
    if (platform === 'trae' && realm === 'global') {
      assert.equal(starts.length, before, 'Unsupported Trae global must not contact any provider');
      assert.match(await dialog.innerText(), /尚未接入国际版/);
      assert.equal(await dialog.locator('a').count(), 0);
    } else {
      const link = dialog.getByRole('link', {name: /前往.*授权/});
      await link.waitFor();
      assert.equal(await link.getAttribute('href'), urls[platform][realm]);
      assert.equal(starts.at(-1).platform, platform);
      assert.equal(starts.at(-1).realm, realm);
      if (platform !== 'workbuddy') {
        assert.equal(await dialog.getByLabel('地区（用于国际版注册）').count(), 0);
        assert.doesNotMatch(await dialog.innerText(), /已自动完成签到|微信 \/ QQ/);
      }
      if (platform === 'qoder' && realm === 'cn') {
        const popupPromise = context.waitForEvent('page');
        await link.click();
        const popup = await popupPromise;
        await popup.waitForLoadState('domcontentloaded');
        assert.equal(await popup.evaluate(() => window.opener), null);
        assert.match(popup.url(), /^https:\/\/qoder\.com\.cn\//);
        await popup.close();
      }
      if (platform === 'trae') {
        const field = dialog.getByLabel('本次授权的回调地址');
        await field.fill('http://127.0.0.1:17388/authorize?synthetic=only');
        await dialog.getByRole('button', {name: '确认连接 Trae'}).click();
        await page.getByText('Trae 账号「Synthetic account」已连接', {exact: true}).waitFor();
        assert.equal(finishes.length, 1);
      }
    }
    await page.screenshot({path: path.join(output, `auth-${platform}-${realm}.png`), animations: 'disabled'});
    if (await dialog.isVisible()) await dialog.getByRole('button', {name: '关闭', exact: true}).click();
    cases.push({platform, realm, passed: true});
  }
  await page.setViewportSize({width: 390, height: 844});
  await setScope('trae', 'cn');
  failFinish = true;
  await page.getByRole('button', {name: '添加账号', exact: true}).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.getByLabel('本次授权的回调地址').fill('http://127.0.0.1:17388/authorize?synthetic=only');
  await dialog.getByRole('button', {name: '确认连接 Trae'}).click();
  await dialog.getByText(/请先刷新账号列表核对/).waitFor();
  reviewMode = 'unconfirmed';
  const finishBeforeReview = finishes.length;
  const startBeforeReview = starts.length;
  await dialog.getByRole('button', {name: '刷新状态', exact: true}).click();
  await dialog.getByText('结果待核对', {exact: true}).waitFor();
  assert.equal(finishes.length, finishBeforeReview, 'Read-only review must not resubmit the callback');
  assert.equal(starts.length, startBeforeReview, 'Read-only review must not start a fresh PKCE flow');
  assert.equal(await dialog.getByRole('button', {name: '重新获取'}).count(), 0);
  assert.equal(await dialog.getByRole('button', {name: '确认连接 Trae'}).count(), 0);
  const size = await dialog.boundingBox();
  assert.ok(size.x >= 0 && size.x + size.width <= 390);
  const close = dialog.getByRole('button', {name: '关闭', exact: true});
  await close.scrollIntoViewIfNeeded();
  await page.screenshot({path: path.join(output, 'auth-trae-unconfirmed-mobile.png'), animations: 'disabled'});
  await close.click();

  await page.getByRole('button', {name: '添加账号', exact: true}).first().click();
  await dialog.getByText(/暂不发起新连接/).waitFor();
  assert.equal(starts.length, startBeforeReview, 'Closing and reopening must retain unresolved evidence');
  reviewMode = 'success';
  await dialog.getByRole('button', {name: '刷新状态', exact: true}).click();
  await dialog.getByText('已确认保存', {exact: true}).waitFor();
  assert.equal(await dialog.getByText('Trae 账号「Synthetic account」已连接', {exact: true}).count(), 0,
    'A historical completed session is evidence, not confirmation of a new dialog');
  await dialog.getByRole('button', {name: '关闭', exact: true}).click();
  reviewMode = 'empty';

  await setScope('qoder', 'cn');
  delayReview = true;
  const beforeLateReview = starts.length;
  await page.getByRole('button', {name: '添加账号', exact: true}).first().click();
  for (let i = 0; !holdReview && i < 50; i++) await page.waitForTimeout(20);
  assert.ok(holdReview);
  await dialog.getByRole('button', {name: '关闭', exact: true}).click();
  holdReview();
  await page.waitForTimeout(300);
  assert.equal(starts.length, beforeLateReview, 'A late evidence response must not start authorization after closing');
  delayReview = false;

  await setScope('qoder', 'cn');
  delayStart = true;
  await page.getByRole('button', {name: '添加账号', exact: true}).first().click();
  await page.waitForFunction(() => document.querySelector('[role=dialog]'));
  for (let i = 0; !holdStart && i < 50; i++) await page.waitForTimeout(20);
  assert.ok(holdStart);
  await page.getByRole('dialog').getByRole('button', {name: '关闭', exact: true}).click();
  const pollBefore = polls.length;
  holdStart();
  await page.waitForTimeout(2400);
  assert.equal(await page.getByRole('dialog').count(), 0);
  assert.equal(polls.length, pollBefore, 'A late start response must not attach a timer to a closed dialog');
  assert.equal(polls.some(p => p.platform === 'trae'), false);
  return {cases, starts, polls, finishes, reviews, staleResponseIgnored: true, popupIsolated: true,
    unknownCompletionNotRetried: true, unresolvedEvidenceSurvivesReopen: true,
    reviewDoesNotWrite: true, historicalSuccessNotNewSuccess: true, mobileFits: true,
    scope: 'Synthetic API and provider pages only. No login, scan, credentials or real account write.'};
};
