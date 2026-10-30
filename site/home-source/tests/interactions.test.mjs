import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM, VirtualConsole } from 'jsdom';

const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
const script = readFileSync(new URL('../public/scripts/upgrade.js', import.meta.url), 'utf8');

function fixture(withMotion = false) {
  const errors = [];
  const console = new VirtualConsole();
  console.on('jsdomError', error => errors.push(error.message));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/', virtualConsole: console });
  const w = dom.window;
  let hidden = false, onScreen = true, clipboard = '';
  const mediaListeners = [];
  const media = { matches: false, addEventListener: (_, listener) => { mediaListeners.push(listener); } };
  const fine = { matches: true, addEventListener() {} };
  w.matchMedia = query => query.includes('prefers-reduced-motion') ? media : fine;
  Object.defineProperty(w.document, 'hidden', { get: () => hidden });
  Object.defineProperty(w, 'isSecureContext', { value: true });
  Object.defineProperty(w.navigator, 'clipboard', { value: { writeText: async value => { clipboard = value; } } });
  w.Element.prototype.getBoundingClientRect = () => ({ top: onScreen ? 100 : 10000, bottom: onScreen ? 500 : 10500, left: 0, right: 900, width: 900, height: 400 });
  w.Element.prototype.scrollIntoView = () => {};
  w.SVGElement.prototype.getTotalLength = () => 500;
  w.SVGElement.prototype.getPointAtLength = length => ({ x: length, y: 180 });
  let counter = 0;
  const frames = new Map();
  w.requestAnimationFrame = callback => { frames.set(++counter, callback); return counter; };
  w.cancelAnimationFrame = id => frames.delete(id);
  w.eval(script);
  let strokes = 0;
  if (withMotion) {
    w.HTMLCanvasElement.prototype.getContext = () => ({ clearRect() {}, setTransform() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() { strokes++; }, arc() {}, fill() {} });
    w.eval(readFileSync(new URL('../public/scripts/motion.js', import.meta.url), 'utf8'));
  }
  const $ = selector => w.document.querySelector(selector);
  return {
    w, $, errors, frames, click: selector => $(selector).click(),
    get clipboard() { return clipboard; },
    get strokes() { return strokes; },
    tick(time) { const batch = [...frames.values()]; frames.clear(); batch.forEach(callback => callback(time)); },
    reduce(value) { media.matches = value; mediaListeners.forEach(listener => listener()); },
    background(value) { hidden = value; w.document.dispatchEvent(new w.Event('visibilitychange')); },
    offscreen(value) { onScreen = !value; },
    close() { w.close(); },
  };
}

test('every platform selector synchronizes all contexts and every code format', () => {
  const f = fixture();
  try {
    for (const origin of ['platform', 'route', 'code', 'hero-platform']) {
      for (const platform of ['workbuddy', 'qoder', 'trae']) {
        f.click(`[data-${origin}="${platform}"]`);
        for (const attribute of ['platform', 'route', 'code', 'hero-platform']) {
          assert.equal(f.$(`[data-${attribute}="${platform}"]`).getAttribute('aria-pressed'), 'true');
          assert.equal(f.w.document.querySelectorAll(`[data-${attribute}][aria-pressed="true"]`).length, 1);
        }
        const prefix = platform === 'workbuddy' ? '/v1' : `/${platform}/v1`;
        assert.equal(f.$('#platformPrefix').textContent, prefix + '/*');
        assert.ok(f.$('#routeLiveSummary').textContent.includes(prefix + '/*'));
        for (const format of ['curl', 'python', 'env']) {
          f.click(`[data-format="${format}"]`);
          const code = f.$('#codeBlock').textContent;
          assert.ok(code.includes(`https://YOUR_GATEWAY_HOST${prefix}`));
          assert.ok(code.includes('YOUR_API_KEY'));
          assert.ok(code.includes(format === 'curl' ? 'curl --request POST' : format === 'python' ? 'from openai import OpenAI' : 'export OPENAI_BASE_URL'));
        }
      }
    }
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('ambient motion renders, stops on pause/background/reduced motion, and resumes once', () => {
  const f = fixture(true);
  try {
    assert.equal(f.frames.size, 1);
    f.tick(100); f.tick(140);
    assert.ok(f.strokes > 0);
    assert.equal(f.$('canvas').dataset.flowState, 'running');
    f.click('#zh-motion-control');
    assert.equal(f.frames.size, 0);
    assert.equal(f.$('canvas').dataset.flowState, 'paused');
    f.click('#zh-motion-control');
    assert.equal(f.frames.size, 1);
    f.background(true); assert.equal(f.frames.size, 0);
    f.background(false); assert.equal(f.frames.size, 1);
    f.reduce(true); assert.equal(f.frames.size, 0);
    f.reduce(false); assert.equal(f.frames.size, 1);
    f.w.dispatchEvent(new f.w.Event('pagehide')); assert.equal(f.frames.size, 0);
    f.w.dispatchEvent(new f.w.Event('pageshow')); assert.equal(f.frames.size, 1);
    f.$('.visual-wrap').dispatchEvent(new f.w.MouseEvent('pointermove', { clientX: 700, clientY: 200 }));
    f.tick(200);
    assert.notEqual(f.$('.demo-browser').style.getPropertyValue('--stage-ry'), '');
    f.click('#zh-motion-control');
    assert.equal(f.$('.demo-browser').style.getPropertyValue('--stage-ry'), '');
    assert.equal(f.frames.size, 0);
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('workspace has eight exclusive images and supports keyboard navigation', () => {
  const f = fixture();
  try {
    for (const button of f.w.document.querySelectorAll('[data-console]')) {
      button.click();
      const visible = [...f.w.document.querySelectorAll('.console-panel')].filter(node => !node.hidden);
      assert.equal(visible.length, 1);
      assert.equal(visible[0].dataset.panel, button.dataset.console);
      assert.equal(f.$('#workspaceTitle').textContent, button.textContent);
      assert.ok(visible[0].querySelector('img').getAttribute('src').startsWith('/preview/'));
    }
    f.$('[data-console="security"]').dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    assert.equal(f.$('[data-console="overview"]').getAttribute('aria-pressed'), 'true');
    assert.equal(f.w.document.activeElement.dataset.console, 'overview');
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('ledger states and FAQ expanded state remain accurate', () => {
  const f = fixture();
  try {
    for (const [state, badge, activeCard] of [['settled', 'SETTLED', 2], ['unknown', 'UNKNOWN', 1], ['reject', 'REJECTED', 0]]) {
      f.click(`[data-ledger="${state}"]`);
      assert.equal(f.$('#ledgerBadge').textContent, badge);
      assert.equal(f.$('.ledger-wrap').dataset.ledgerState, state);
      assert.equal([...f.w.document.querySelectorAll('.ledger-card')].findIndex(node => node.classList.contains('focus')), activeCard);
    }
    const questions = [...f.w.document.querySelectorAll('.faq-question')];
    questions[1].click();
    assert.equal(questions[1].getAttribute('aria-expanded'), 'true');
    assert.equal(questions[0].getAttribute('aria-expanded'), 'false');
    questions[1].click();
    assert.equal(questions[1].getAttribute('aria-expanded'), 'false');
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('mobile menu closes via Escape, navigation, outside click and desktop resize', () => {
  const f = fixture();
  try {
    const closed = () => {
      assert.equal(f.$('#mobileTrigger').getAttribute('aria-expanded'), 'false');
      assert.equal(f.$('#mobileTrigger').getAttribute('aria-label'), '打开菜单');
      assert.equal(f.$('#mobileMenu').classList.contains('open'), false);
    };
    f.click('#mobileTrigger');
    f.w.document.dispatchEvent(new f.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    closed();
    assert.equal(f.w.document.activeElement.id, 'mobileTrigger');
    f.click('#mobileTrigger'); f.click('#mobileMenu a'); closed();
    f.click('#mobileTrigger'); f.click('#hero-title'); closed();
    f.click('#mobileTrigger'); f.w.dispatchEvent(new f.w.Event('resize')); closed();
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('route scenarios complete and interrupted motion cannot leave a running state', () => {
  const f = fixture();
  try {
    for (const scenario of ['fresh', 'sticky', 'fallback']) {
      f.click(`[data-scenario="${scenario}"]`);
      assert.equal(f.$('#zh23Run').disabled, true);
      f.tick(0); f.tick(1000); f.tick(2001);
      assert.equal(f.$('.zh23-route-result').dataset.state, 'done');
      assert.equal(f.$('#zh23Run').disabled, false);
      assert.equal(f.frames.size, 0);
    }
    for (const interrupt of [() => f.click('#zh-motion-control'), () => f.reduce(true), () => f.background(true), () => { f.offscreen(true); f.tick(3000); }]) {
      f.click('#zh23Run');
      interrupt();
      assert.equal(f.$('.zh23-route-result').dataset.state, 'done');
      assert.equal(f.$('#zh23Run').disabled, false);
      assert.equal(f.frames.size, 0);
      if (f.$('html').hasAttribute('data-home-flow-paused')) f.click('#zh-motion-control');
      f.reduce(false); f.background(false); f.offscreen(false);
    }
    f.reduce(true); f.click('#zh23Run');
    assert.equal(f.$('.zh23-route-result').dataset.state, 'done');
    assert.equal(f.$('#zh-motion-control').disabled, false);
    f.click('#zh-motion-control');
    assert.equal(f.$('html').hasAttribute('data-reduced-motion'), false);
    assert.equal(f.$('html').hasAttribute('data-motion-override'), true);
    f.click('#zh23Run');
    assert.equal(f.$('#zh23Run').disabled, true);
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('embedded ride is local and play overrides a reduced-motion default', () => {
  const f = fixture();
  try {
    const messages = [];
    const ride = f.$('#pelicanRide');
    ride.contentWindow.postMessage = value => messages.push(value);
    f.w.eval(readFileSync(new URL('../public/scripts/ride.js', import.meta.url), 'utf8'));
    assert.equal(ride.getAttribute('src'), '/animations/pelican-ride.html');
    assert.equal(ride.getAttribute('sandbox'), 'allow-scripts');
    assert.equal(messages.at(-1).paused, false);
    f.click('#rideMotionControl'); assert.equal(messages.at(-1).paused, true);
    f.click('#rideMotionControl'); assert.equal(messages.at(-1).paused, false);
    f.reduce(true); assert.equal(messages.at(-1).paused, true);
    assert.equal(f.$('#rideMotionControl').textContent, '播放骑行');
    f.click('#rideMotionControl');
    assert.equal(messages.at(-1).paused, false);
    assert.equal(messages.at(-1).reducedMotionOverride, true);
    f.background(true); assert.equal(messages.at(-1).paused, true);
    f.background(false); assert.equal(messages.at(-1).paused, false);
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('three opening screens, original scene colors, three shipping entries and detail shortcuts', () => {
  const f = fixture();
  try {
    const children = [...f.$('main').children].filter(node => node.tagName === 'SECTION' || node.id === 'top').slice(0,3);
    assert.deepEqual(children.map(node => node.id), ['top','task-scene','ride-preview']);
      assert.equal(f.$('#ride-preview').nextElementSibling.id, 'perspective');
    assert.equal(f.$('.hero #pelicanRide'), null);
    assert.equal(f.$('.hero #taskWorkspace'), null);
    assert.equal(f.$('#taskWorkspace').getAttribute('sandbox'), 'allow-scripts');
    assert.match(f.$('.hero .intro').textContent, /WorkBuddy、Qoder、Trae/);
    for (const attribute of ['platform', 'route', 'code', 'hero-platform']) {
      assert.equal(f.w.document.querySelectorAll(`[data-${attribute}]`).length, 3);
      assert.equal(f.$(`[data-${attribute}="codex"]`), null);
    }
    assert.doesNotMatch(f.w.document.body.textContent, /四网关|四种入口|Codex/);
    f.click('[data-open-workspace="models"]');
    assert.equal(f.$('[data-console="models"]').getAttribute('aria-pressed'), 'true');
    f.click('[data-open-workspace="usage"]');
    assert.equal(f.$('[data-console="usage"]').getAttribute('aria-pressed'), 'true');
    const messages = [];
    f.$('#taskWorkspace').contentWindow.postMessage = message => messages.push(message);
    f.w.eval(readFileSync(new URL('../public/scripts/ride.js', import.meta.url), 'utf8'));
    assert.equal(messages.at(-1).type, 'task-playback');
    f.click('#taskMotionControl'); assert.equal(messages.at(-1).paused, true);
    f.click('#taskMotionControl'); assert.equal(messages.at(-1).paused, false);
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});

test('copy writes the selected example and reports clipboard failures', async () => {
  const f = fixture();
  try {
    f.click('[data-code="qoder"]'); f.click('[data-format="python"]'); f.click('#copyCode');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.clipboard, f.$('#codeBlock').textContent);
    assert.equal(f.$('#toast').textContent, '配置示例已复制');
    f.w.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
    f.click('#copyCode'); await new Promise(resolve => setImmediate(resolve));
    assert.equal(f.$('#toast').textContent, '无法自动复制，请手动选取代码');
    assert.deepEqual(f.errors, []);
  } finally { f.close(); }
});
