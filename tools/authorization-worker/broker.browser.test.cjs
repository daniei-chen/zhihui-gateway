'use strict';

const {test, before, after} = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const {PassThrough, Writable} = require('node:stream');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {FixtureBroker} = require('./broker.cjs');
const {serveStdio} = require('./stdio.cjs');

const OWNER = 'synthetic-manager-user';
const STATE = 's'.repeat(43);
const TRACE = 'a'.repeat(32);
const REDIRECT = 'http://127.0.0.1:17388/authorize';
let browser, server, origin;
const html = `<!doctype html><meta charset="utf-8"><title>Synthetic transport fixture</title>
<style>body{font:14px sans-serif;padding:20px}input,button{display:block;margin:12px;padding:10px}</style>
<input placeholder="\u8f93\u5165\u624b\u673a\u53f7"><input placeholder="\u8f93\u5165\u9a8c\u8bc1\u7801">
<label><input id="consent" type="checkbox">Synthetic human consent</label>
<button id="sms" onclick="if(consent.checked)document.body.dataset.sms='yes'">\u83b7\u53d6\u9a8c\u8bc1\u7801</button>
<button onclick="document.body.dataset.douyin='selected'">\u6296\u97f3\u767b\u5f55</button>
<button id="finish" onclick="document.body.dataset.finished='yes';location.href=window.callback">Finish fixture</button>
<script>window.callback='${REDIRECT}?'+new URLSearchParams({loginTraceID:new URL(location.href).searchParams.get('login_trace_id'),authCodeInfo:'synthetic-only'});</script>`;

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
function open(extra = {}) {
  const authUrl = origin + '/authorization?' + new URLSearchParams({auth_callback_url: REDIRECT,
    login_trace_id: TRACE, code_challenge_method: 'S256', code_challenge: 'x'.repeat(43)});
  return {id: 1, command: 'open', owner: OWNER, state: STATE, authUrl, ...extra};
}
function command(handle, kind, extra = {}) {
  return {id: 2, command: kind, owner: OWNER, state: STATE, handle, ...extra};
}
function fixture(t, onCallback) {
  const contexts = [];
  const broker = new FixtureBroker({fixtureOrigin: origin, createContext: async options => {
    const context = await browser.newContext(options);
    contexts.push(context);
    return context;
  }, onCallback});
  t.after(() => broker.shutdown());
  return {broker, contexts};
}

test('synthetic browser transport performs finite phone/human flow and keeps callback private', async t => {
  const callbacks = [];
  const {broker, contexts} = fixture(t, async event => { callbacks.push(event); return {confirmed: true}; });
  const opened = await broker.execute(open());
  assert.equal(opened.ok, true);
  const handle = opened.data.handle, page = contexts[0].pages()[0];
  const contextClosed = new Promise(resolve => contexts[0].once('close', resolve));
  assert.equal((await broker.execute(command(handle, 'action', {kind: 'choose_phone'}))).ok, true);
  assert.equal((await broker.execute(command(handle, 'action', {kind: 'fill_phone', value: '13800000000'}))).ok, true);
  assert.equal(await page.getByPlaceholder('\u8f93\u5165\u624b\u673a\u53f7').inputValue(), '13800000000');
  assert.equal((await broker.execute(command(handle, 'action', {kind: 'request_sms'}))).ok, true);
  assert.equal(await page.locator('#consent').isChecked(), false);
  assert.equal(await page.locator('body').getAttribute('data-sms'), null, 'No automatic consent');
  const box = await page.locator('#finish').boundingBox();
  const frame = await broker.execute(command(handle, 'capture'));
  assert.equal(frame.ok, true);
  const jpeg = Buffer.from(frame.data.jpegBase64, 'base64');
  assert.ok(jpeg.length > 1000);
  assert.equal(jpeg.readUInt16BE(0), 0xffd8);
  const pointer = {frameId: frame.data.id, x: box.x + box.width / 2, y: box.y + box.height / 2};
  assert.equal((await broker.execute(command(handle, 'pointer', {...pointer, type: 'down'}))).ok, true);
  assert.equal((await broker.execute(command(handle, 'pointer', {...pointer, type: 'up'}))).ok, true);
  const until = Date.now() + 5000;
  let status;
  do {
    status = await broker.execute(command(handle, 'status'));
    if (status.data?.phase === 'complete') break;
    await new Promise(resolve => setTimeout(resolve, 10));
  } while (Date.now() < until);
  assert.equal(status.data?.phase, 'complete');
  assert.equal(status.data.qrConfirmed, false);
  assert.equal(status.data.productionReady, false);
  assert.equal(callbacks.length, 1);
  assert.equal(callbacks[0].owner, OWNER);
  assert.equal(callbacks[0].state, STATE);
  assert.equal(new URL(callbacks[0].callback).searchParams.get('loginTraceID'), TRACE);
  await contextClosed;
  assert.equal(contexts[0].pages().length, 0);
  for (const response of [opened, frame, status]) {
    assert.equal(JSON.stringify(response).includes('authCodeInfo'), false);
    assert.equal(JSON.stringify(response).includes(STATE), false);
  }
});

test('real private contexts clean up after JSONL EOF without returning hidden page data', async t => {
  const {broker, contexts} = fixture(t);
  const input = new PassThrough(), lines = [];
  const output = new Writable({write(chunk, encoding, callback) { lines.push(chunk.toString()); callback(); }});
  const serving = serveStdio({input, output, broker});
  input.end(JSON.stringify(open()) + '\n');
  await serving;
  const response = JSON.parse(lines[0]);
  assert.equal(response.ok, true);
  assert.equal(response.data.mode, 'synthetic_fixture');
  assert.equal(contexts[0].pages().length, 0);
  assert.equal(broker.stats().sessions, 0);
  assert.equal(lines.join('').includes(TRACE), false);
});

test('synthetic browser transport refuses real-provider bootstrap before browser creation', async t => {
  const {broker, contexts} = fixture(t);
  const result = await broker.execute(open({authUrl: open().authUrl.replace(origin, 'https://www.trae.cn')}));
  assert.equal(result.error.code, 'invalid_bootstrap');
  assert.equal(contexts.length, 0);
});
