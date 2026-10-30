import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from '../home-source/node_modules/jsdom/lib/api.js';
import {createPreviewServer} from '../tools/preview-server.mjs';

const source = readFileSync(new URL('../login/index.html', import.meta.url), 'utf8');
const script = source.match(/<script>([\s\S]*?)<\/script>/)[1];
function fixture(reply) {
  const dom = new JSDOM(source, {runScripts: 'outside-only', pretendToBeVisual: true,
    url: 'https://gateway.example/login?expired=1'});
  const w = dom.window, calls = [];
  w.matchMedia = () => ({matches: false, addEventListener() {}});
  w.fetch = async (path, options) => {
    calls.push({path, options});
    return path === '/api/me' ? {ok: false} : reply();
  };
  w.eval(script);
  return {w, calls, close: () => w.close(),
    $: selector => w.document.querySelector(selector),
    send: () => w.document.getElementById('loginForm').dispatchEvent(new w.Event('submit', {cancelable: true})),
    settle: () => new Promise(resolve => setImmediate(resolve))};
}

test('original controls, autofill, visibility and duplicate-submit guard remain functional', async () => {
  let resolve;
  const f = fixture(() => new Promise(r => {resolve = r;}));
  try {
    assert.match(f.$('#formStatus').textContent, /已过期/);
    f.send(); assert.equal(f.w.document.activeElement.id, 'username');
    f.$('#username').value = '  fixture-user  ';
    f.send(); assert.equal(f.w.document.activeElement.id, 'password');
    f.$('#password').value = '  synthetic password  ';
    f.$('#togglePassword').click();
    assert.equal(f.$('#password').type, 'text');
    f.$('#togglePassword').click();
    assert.equal(f.$('#password').type, 'password');
    f.send(); f.send();
    assert.equal(f.calls.length, 2);
    assert.equal(f.$('#submitBtn').disabled, true);
    assert.equal(f.calls[1].path, '/api/login');
    assert.deepEqual(JSON.parse(f.calls[1].options.body), {username: 'fixture-user', password: '  synthetic password  '});
    resolve({ok: false, status: 401, json: async () => ({detail: 'Synthetic rejection'})});
    await f.settle();
    assert.equal(f.$('#submitBtn').disabled, false);
    assert.equal(f.$('#password').value, '');
    assert.match(f.$('#formStatus').textContent, /Synthetic rejection/);
    assert.equal(f.w.localStorage.length, 0);
    assert.equal(f.w.sessionStorage.length, 0);
  } finally { f.close(); }
});

test('local preview intercepts original login in memory without sending credentials', async () => {
  const server = createPreviewServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  let dom;
  try {
    const html = await (await fetch(base + '/login')).text();
    const guard = await (await fetch(base + '/__preview__/login-guard.js')).text();
    dom = new JSDOM(html, {runScripts: 'outside-only', pretendToBeVisual: true, url: base + '/login'});
    const w = dom.window;
    w.matchMedia = () => ({matches: false, addEventListener() {}});
    w.Response = Response;
    w.fetch = () => {throw Error('Network must not be reached');};
    w.eval(guard); w.eval(script);
    w.document.getElementById('username').value = 'synthetic-only';
    w.document.getElementById('password').value = 'synthetic-only';
    w.document.getElementById('loginForm').dispatchEvent(new w.Event('submit', {cancelable: true}));
    await new Promise(resolve => setImmediate(resolve));
    assert.match(w.document.getElementById('formStatus').textContent, /未提交账号或密码/);
    assert.equal(w.document.getElementById('submitBtn').disabled, false);
    assert.equal(w.localStorage.length, 0);
    assert.equal(w.sessionStorage.length, 0);
  } finally { dom?.window.close(); await new Promise(resolve => server.close(resolve)); }
});
