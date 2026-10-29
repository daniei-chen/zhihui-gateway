'use strict';

const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {AuthorizationSession} = require('./session.cjs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const OWNER = 'synthetic-owner';
const STATE = 'synthetic-manager-state';
const TRACE = 'a'.repeat(32);
const REDIRECT = 'http://127.0.0.1:17388/authorize';
let server, browser, origin;

const html = `<!doctype html><meta charset="utf-8"><title>Synthetic login only</title>
<style>body{font:16px sans-serif;padding:24px}input,button{display:block;margin:16px;padding:12px}</style>
<input placeholder="输入手机号"><input placeholder="输入验证码">
<label><input type="checkbox" id="consent">Synthetic terms, only a human may confirm</label>
<button id="sms" onclick="if(consent.checked){document.body.dataset.sms=Number(document.body.dataset.sms||0)+1}else{document.body.dataset.needsConsent='true'}">获取验证码</button>
<button onclick="document.getElementById('terms').hidden=false">抖音登录</button>
<p id="terms" hidden>Official consent fixture; not automatically accepted.</p>
<button id="human" onclick="document.body.dataset.human='yes'">Human confirmation fixture</button>
<button id="finish" onclick="location.href=window.callback">Finish synthetic flow</button>
<script>
window.callback='${REDIRECT}?'+new URLSearchParams({loginTraceID:new URL(location.href).searchParams.get('login_trace_id'),authCodeInfo:JSON.stringify({AuthCode:'synthetic-only'})});
</script>`;

before(async () => {
  server = http.createServer((request, response) => {
    response.writeHead(200, {'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store'});
    response.end(html);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  browser = await chromium.launch({headless: true});
});

after(async () => {
  await browser?.close();
  if (server) await new Promise(resolve => server.close(resolve));
});

function bootstrap(extra = {}) {
  return origin + '/authorization?' + new URLSearchParams({
    auth_callback_url: REDIRECT, login_trace_id: TRACE,
    code_challenge_method: 'S256', code_challenge: 'x'.repeat(43), ...extra,
  });
}

async function fixture(t, options = {}) {
  let clock = 1000;
  const completed = [];
  const context = await browser.newContext({viewport: {width: 520, height: 760},
    serviceWorkers: 'block', acceptDownloads: false});
  const session = new AuthorizationSession({context, owner: OWNER, state: STATE,
    authUrl: bootstrap(), fixtureOrigin: origin, now: () => clock,
    onCallback: async url => { completed.push(url); return {confirmed: true}; }, ...options});
  t.after(() => session.close(OWNER, STATE));
  await session.start(OWNER, STATE);
  return {session, context, completed, advance: ms => { clock += ms; }};
}

async function mustReject(action, code) {
  await assert.rejects(action, error => error.code === code && error.message === code);
}

async function clickFrame(session, selector) {
  const box = await session.page.locator(selector).boundingBox();
  const frame = await session.capture(OWNER, STATE);
  assert.ok(frame.jpeg.length > 100);
  assert.equal(frame.url, undefined, 'Authorization URL must stay private');
  const event = {frameId: frame.id, x: box.x + box.width / 2, y: box.y + box.height / 2};
  await session.input(OWNER, STATE, {...event, type: 'down'});
  await session.input(OWNER, STATE, {...event, type: 'up'});
  return frame;
}

test('bootstrap rejects duplicate state, foreign origin and malformed URL without echoing input', () => {
  const args = {context: {}, owner: OWNER, state: STATE, onCallback() {}, fixtureOrigin: origin};
  for (const authUrl of [bootstrap() + '&login_trace_id=' + TRACE,
    bootstrap().replace(origin, 'https://unreviewed.invalid'), 'not-a-url-synthetic-secret']) {
    assert.throws(() => new AuthorizationSession({...args, authUrl}),
      error => error.code === 'invalid_bootstrap' && !error.message.includes('synthetic-secret'));
  }
});

test('owner/state and start are bound; unsupported commands never reach page', async t => {
  const {session} = await fixture(t);
  await mustReject(session.action('another-owner', STATE, 'choose_phone'), 'session_not_found');
  await mustReject(session.start(OWNER, STATE), 'already_started');
  await mustReject(session.action(OWNER, STATE, 'evaluate', 'window.location'), 'unknown_action');
  assert.equal(session.status(OWNER, STATE).productionReady, false);
});

test('phone/code filling uses finite fields; SMS and consent are never automatic', async t => {
  const {session, advance} = await fixture(t);
  assert.equal(await session.page.locator('body').getAttribute('data-sms'), null);
  await session.action(OWNER, STATE, 'choose_phone');
  await mustReject(session.action(OWNER, STATE, 'request_sms'), 'wrong_phase');
  await mustReject(session.action(OWNER, STATE, 'fill_phone', 'invalid'), 'invalid_phone');
  await session.action(OWNER, STATE, 'fill_phone', '13800000000');
  assert.equal(await session.page.getByPlaceholder('输入手机号').inputValue(), '13800000000');
  await session.action(OWNER, STATE, 'request_sms');
  assert.equal(await session.page.locator('#consent').isChecked(), false);
  assert.equal(await session.page.locator('body').getAttribute('data-needs-consent'), 'true');
  assert.equal(await session.page.locator('body').getAttribute('data-sms'), null);
  await clickFrame(session, '#consent');
  await session.action(OWNER, STATE, 'choose_phone');
  await mustReject(session.action(OWNER, STATE, 'request_sms'), 'sms_cooldown');
  advance(60001);
  await session.action(OWNER, STATE, 'request_sms');
  assert.equal(await session.page.locator('body').getAttribute('data-sms'), '1');
  await session.action(OWNER, STATE, 'fill_code', '123456');
  assert.equal(await session.page.getByPlaceholder('输入验证码').inputValue(), '123456');
  assert.equal(session.status(OWNER, STATE).phase, 'human');
});

test('Douyin selection presents actual fixture consent, never claims QR is verified', async t => {
  const {session} = await fixture(t);
  await session.action(OWNER, STATE, 'choose_douyin');
  assert.equal(await session.page.locator('#terms').isVisible(), true);
  assert.equal(await session.page.locator('#consent').isChecked(), false);
  assert.equal(session.status(OWNER, STATE).qrConfirmed, false);
});

test('real frame pointer clicks work; bounds, stale frame and resize are rejected', async t => {
  const {session, advance} = await fixture(t);
  const frame = await clickFrame(session, '#human');
  assert.equal(await session.page.locator('body').getAttribute('data-human'), 'yes');
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: -1, y: 5}), 'invalid_input');
  advance(600);
  const next = await session.capture(OWNER, STATE);
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 5, y: 5}), 'stale_frame');
  await session.page.setViewportSize({width: 390, height: 700});
  await mustReject(session.input(OWNER, STATE, {frameId: next.id, type: 'down', x: 5, y: 5}), 'stale_frame');
});

test('stale input releases a held pointer; no capture can replace a drag frame', async t => {
  const {session, advance} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  await mustReject(session.capture(OWNER, STATE), 'frame_not_available');
  advance(10001);
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'move', x: 45, y: 40}), 'stale_frame');
  assert.equal(session.pointerDown, false);
});

test('pointer cancel releases drag, invalidates frame, and respects frame rate', async t => {
  const {session, advance} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  await mustReject(session.capture(OWNER, STATE), 'frame_rate_limited');
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  await session.cancelInput(OWNER, STATE);
  assert.equal(session.pointerDown, false);
  assert.equal(session.frame, null);
  advance(501);
  assert.ok((await session.capture(OWNER, STATE)).id > frame.id);
});

test('same URL reload invalidates old frame and does not inherit a pressed pointer', async t => {
  const {session} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  await session.page.reload();
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'up', x: 40, y: 40}), 'stale_frame');
  assert.equal(session.pointerDown, false);
});

test('invalid pointer release coordinates cannot leave a held button', async t => {
  const {session} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'up', x: Infinity, y: 40}), 'invalid_input');
  assert.equal(session.pointerDown, false);
});

test('human keyboard allows fixed navigation only and requires fresh owner-bound frames', async t => {
  const {session, advance} = await fixture(t);
  await session.page.locator('#consent').focus();
  const frame = await session.capture(OWNER, STATE);
  await mustReject(session.humanKey('wrong-owner', STATE, {frameId: frame.id, key: 'Space'}), 'session_not_found');
  await mustReject(session.humanKey(OWNER, STATE, {frameId: frame.id, key: 'Control+L'}), 'invalid_key');
  await mustReject(session.humanKey(OWNER, STATE, {frameId: frame.id, key: 'synthetic-secret'}), 'invalid_key');
  assert.equal(await session.page.locator('#consent').isChecked(), false);
  await session.humanKey(OWNER, STATE, {frameId: frame.id, key: 'Space'});
  assert.equal(await session.page.locator('#consent').isChecked(), true, 'Only the human key command accepts fixture consent');
  await mustReject(session.humanKey(OWNER, STATE, {frameId: frame.id, key: 'Tab'}), 'stale_frame');
  advance(501);
  const next = await session.capture(OWNER, STATE);
  await session.humanKey(OWNER, STATE, {frameId: next.id, key: 'Tab'});
  assert.notEqual(await session.page.evaluate(() => document.activeElement.id), 'consent');
});

test('bounded human scrolling invalidates the old image and cannot operate during a drag', async t => {
  const {session, advance} = await fixture(t);
  await session.page.evaluate(() => { document.body.style.height = '3000px'; });
  const frame = await session.capture(OWNER, STATE);
  await mustReject(session.scroll(OWNER, STATE, {frameId: frame.id, x: 100, y: 100, deltaX: 0, deltaY: 601}), 'invalid_scroll');
  await mustReject(session.scroll(OWNER, STATE, {frameId: frame.id, x: 100, y: NaN, deltaX: 0, deltaY: 400}), 'invalid_scroll');
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  await mustReject(session.scroll(OWNER, STATE, {frameId: frame.id, x: 100, y: 100, deltaX: 0, deltaY: 400}), 'input_not_available');
  await session.cancelInput(OWNER, STATE);
  advance(501);
  const next = await session.capture(OWNER, STATE);
  await session.scroll(OWNER, STATE, {frameId: next.id, x: 400, y: 400, deltaX: 0, deltaY: 400});
  await session.page.waitForFunction(() => scrollY > 0);
  await mustReject(session.input(OWNER, STATE, {frameId: next.id, type: 'down', x: 40, y: 40}), 'stale_frame');
});

test('human input rate is bounded; limiting releases the drag and requires a fresh image', async t => {
  const {session, advance} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  await session.input(OWNER, STATE, {frameId: frame.id, type: 'down', x: 40, y: 40});
  for (let i = 0; i < 59; i++) await session.input(OWNER, STATE, {frameId: frame.id, type: 'move', x: 40, y: 40});
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'move', x: 40, y: 40}), 'input_rate_limited');
  assert.equal(session.pointerDown, false);
  assert.equal(session.frame, null);
  advance(1000);
  const next = await session.capture(OWNER, STATE);
  await session.input(OWNER, STATE, {frameId: next.id, type: 'down', x: 40, y: 40});
  await session.input(OWNER, STATE, {frameId: next.id, type: 'up', x: 40, y: 40});
});

test('simultaneous human commands cannot both validate the same frame before taking the busy slot', async t => {
  const {session} = await fixture(t);
  const frame = await session.capture(OWNER, STATE);
  let resume;
  const move = session.page.mouse.move.bind(session.page.mouse);
  session.page.mouse.move = async (...args) => {
    await new Promise(resolve => { resume = resolve; });
    return move(...args);
  };
  const first = session.input(OWNER, STATE, {frameId: frame.id, type: 'move', x: 40, y: 40});
  await mustReject(session.humanKey(OWNER, STATE, {frameId: frame.id, key: 'Tab'}), 'input_not_available');
  await mustReject(session.scroll(OWNER, STATE, {frameId: frame.id, x: 40, y: 40, deltaX: 0, deltaY: 100}), 'input_not_available');
  await mustReject(session.input(OWNER, STATE, {frameId: frame.id, type: 'move', x: 41, y: 40}), 'input_not_available');
  assert.equal(typeof resume, 'function');
  resume();
  await first;
});

test('trusted main-frame callback is consumed once; it never opens a loopback listener', async t => {
  const {session, context, completed} = await fixture(t);
  await session.page.locator('#finish').click().catch(() => {});
  await session.pending;
  assert.equal(completed.length, 1);
  assert.equal(session.status(OWNER, STATE).phase, 'complete');
  assert.equal(context.pages().length, 0, 'Provider cookies and code-bearing page are destroyed after completion');
  await mustReject(session.action(OWNER, STATE, 'fill_code', '123456'), 'action_not_available');
  await mustReject(session.capture(OWNER, STATE), 'frame_not_available');
});

test('wrong trace and subresource callback cannot trigger token consumption', async t => {
  const {session, completed} = await fixture(t);
  await session.page.evaluate(async callback => {
    await fetch(callback).catch(() => {});
  }, REDIRECT + '?loginTraceID=' + TRACE);
  assert.equal(completed.length, 0);
  await session.page.evaluate(() => { window.callback = window.callback.replace('a'.repeat(32), 'b'.repeat(32)); });
  await session.page.locator('#finish').click().catch(() => {});
  assert.equal(completed.length, 0);
});

test('duplicate trace and iframe callback are rejected before Manager handoff', async t => {
  const {session, completed} = await fixture(t);
  await session.page.evaluate(callback => {
    const frame = document.createElement('iframe');
    frame.src = callback;
    document.body.append(frame);
  }, REDIRECT + '?loginTraceID=' + TRACE);
  await session.page.waitForTimeout(100);
  assert.equal(completed.length, 0);
  await session.page.evaluate(() => { window.callback += '&loginTraceID=' + 'a'.repeat(32); });
  await session.page.locator('#finish').click().catch(() => {});
  assert.equal(completed.length, 0);
});

test('unreviewed navigation fails closed and does not become a generic remote browser', async t => {
  const {session} = await fixture(t);
  const response = session.page.waitForEvent('requestfailed');
  await session.page.evaluate(() => { location.href = 'http://192.0.2.1/unreviewed'; }).catch(() => {});
  await response;
  assert.equal(session.phase, 'unsupported_origin');
  await mustReject(session.action(OWNER, STATE, 'choose_phone'), 'action_not_available');
});

test('ordinary blocked third-party subresources do not disable an otherwise valid login', async t => {
  const {session} = await fixture(t);
  const failed = session.page.waitForEvent('requestfailed');
  await session.page.evaluate(async () => { await fetch('http://192.0.2.1/telemetry').catch(() => {}); });
  await failed;
  assert.equal(session.status(OWNER, STATE).phase, 'choose');
});

test('lost completion is unconfirmed, cannot replay, and late result cannot reopen a closed session', async t => {
  let resolve;
  const {session} = await fixture(t, {onCallback: () => new Promise(done => { resolve = done; })});
  await session.page.locator('#finish').click();
  assert.equal(session.phase, 'completing');
  await session.close(OWNER, STATE);
  resolve({confirmed: true});
  await session.pending;
  assert.equal(session.phase, 'closed');
  await mustReject(session.action(OWNER, STATE, 'choose_phone'), 'session_closed');
});

test('expiry destroys private browser context rather than only hiding the UI', async t => {
  const {session, context, advance} = await fixture(t);
  advance(300001);
  await mustReject(session.capture(OWNER, STATE), 'session_expired');
  await session.destroyPromise;
  assert.equal(session.phase, 'expired');
  assert.equal(context.pages().length, 0);
});

test('callback rejection never exposes exception text', async t => {
  const {session, context} = await fixture(t, {onCallback: () => { throw new Error('synthetic-callback-secret'); }});
  await session.page.locator('#finish').click().catch(() => {});
  await session.pending;
  assert.equal(session.status(OWNER, STATE).phase, 'unconfirmed');
  assert.equal(context.pages().length, 0);
  assert.doesNotMatch(JSON.stringify(session.status(OWNER, STATE)), /synthetic-callback-secret/);
});

test('failed page load destroys context and returns no provider URL or error body', async () => {
  const context = await browser.newContext();
  await context.route('**/*', route => route.abort());
  const session = new AuthorizationSession({context, owner: OWNER, state: STATE,
    authUrl: bootstrap(), fixtureOrigin: origin, onCallback() {}});
  const goto = context.newPage.bind(context);
  context.newPage = async () => {
    const page = await goto();
    page.goto = async () => { throw new Error('synthetic-provider-url-and-credentials'); };
    return page;
  };
  await mustReject(session.start(OWNER, STATE), 'official_page_unavailable');
  assert.equal(context.pages().length, 0);
  assert.equal(session.phase, 'closed');
});

test('unexpected browser context exit fails closed and never offers a waiting session', async t => {
  const {session, context} = await fixture(t);
  await context.close();
  assert.equal(session.phase, 'failed');
  assert.equal(session.closed, true);
  await mustReject(session.capture(OWNER, STATE), 'session_closed');
});
