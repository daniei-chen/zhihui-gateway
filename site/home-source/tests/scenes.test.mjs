import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import { NodeIO } from '@gltf-transform/core';
import { KHRDracoMeshCompression } from '@gltf-transform/extensions';
import draco from 'draco3dgltf';

test('compressed scan keeps every triangle, vertex and index with bounded quantization error', async () => {
  const manifest = JSON.parse(readFileSync(new URL('../public/models/thinker-manifest.json', import.meta.url)));
  const io = new NodeIO().registerExtensions([KHRDracoMeshCompression]).registerDependencies({ 'draco3d.decoder': await draco.createDecoderModule() });
  const originalPath = process.env.ZHIHUI_THINKER_REFERENCE || fileURLToPath(new URL('../reference-models/thinker-normalized.glb', import.meta.url));
  const original = await io.read(originalPath);
  const decoded = await io.read(fileURLToPath(new URL('../public/models/thinker.glb', import.meta.url)));
  const a = original.getRoot().listMeshes()[0].listPrimitives()[0];
  const b = decoded.getRoot().listMeshes()[0].listPrimitives()[0];
  assert.equal(b.getIndices().getCount() / 3, 837482);
  assert.equal(b.getAttribute('POSITION').getCount(), 418714);
  assert.deepEqual(b.getIndices().getArray(), a.getIndices().getArray());
  const source = a.getAttribute('POSITION').getArray(), result = b.getAttribute('POSITION').getArray();
  let maxError = 0;
  for (let i = 0; i < source.length; i++) maxError = Math.max(maxError, Math.abs(source[i] - result[i]));
  assert.ok(maxError < .00002, `Unexpected geometry error: ${maxError}`);
  assert.ok(b.getAttribute('NORMAL'));
  assert.equal(manifest.watertight, true);
  assert.equal(manifest.consistentWinding, true);
  assert.equal(manifest.degenerateFaces, 0);
  assert.equal(decoded.getRoot().listTextures().length, 0);
  assert.equal(decoded.getRoot().listAnimations().length, 0);
});

test('task animation advances, pauses and supports explicit reduced motion override', () => {
  const html = readFileSync(new URL('../public/animations/task-workspace.html', import.meta.url), 'utf8');
  const script = readFileSync(new URL('../public/animations/task-workspace.js', import.meta.url), 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, timers = new Map();
  let sequence = 0, hidden = false, change;
  const media = { matches: false, addEventListener: (_, cb) => { change = cb; } };
  w.matchMedia = () => media;
  Object.defineProperty(w.document, 'hidden', { get: () => hidden });
  w.setInterval = cb => { timers.set(++sequence, cb); return sequence; };
  w.clearInterval = id => timers.delete(id);
  const send = data => w.dispatchEvent(new w.MessageEvent('message', { source: w, data: { type: 'task-playback', ...data } }));
  try {
    w.eval(script);
    assert.equal(timers.size, 1);
    const tick = count => { for (let i=0;i<count;i++) [...timers.values()].forEach(cb => cb()); };
    tick(4);
    assert.equal(w.document.querySelectorAll('.context-card.arrived').length, 3);
    assert.equal(w.document.querySelectorAll('.context-card.focused').length, 1);
    const focused = w.document.querySelector('.context-card.focused').dataset.context;
    tick(2);
    assert.notEqual(w.document.querySelector('.context-card.focused').dataset.context, focused);
    assert.ok(w.document.getElementById('contextSummary').textContent.includes('关联'));
    tick(1);
    assert.equal(w.document.querySelector('.workspace').dataset.stage, 'build');
    assert.ok(w.document.querySelector('#codeStream code').textContent.length > 0);
    tick(10);
    assert.equal(w.document.querySelector('.workspace').dataset.stage, 'test');
    assert.ok(w.document.querySelectorAll('.check.checked').length > 0);
    tick(5);
    assert.equal(w.document.querySelector('.workspace').dataset.stage, 'done');
    assert.ok(w.document.querySelector('#deliveryReceipt').classList.contains('visible'));
    const frozen = w.document.querySelector('.workspace').outerHTML;
    send({ paused: true });
    assert.equal(timers.size, 0);
    tick(10);
    assert.equal(w.document.querySelector('.workspace').outerHTML, frozen);
    assert.equal(w.document.documentElement.dataset.playback, 'paused');
    media.matches = true; change(); send({ paused: false });
    assert.equal(timers.size, 0);
    send({ paused: false, reducedMotionOverride: true });
    assert.equal(timers.size, 1);
    tick(4);
    assert.equal(w.document.querySelector('.workspace').dataset.stage, 'context');
    assert.ok(w.document.documentElement.hasAttribute('data-motion-override'));
    hidden = true; w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.equal(timers.size, 0);
    hidden = false; w.document.dispatchEvent(new w.Event('visibilitychange'));
    assert.equal(timers.size, 1);
    const css = readFileSync(new URL('../public/animations/task-workspace.css', import.meta.url), 'utf8');
    assert.ok(css.includes('html:not([data-motion-override]) *'));
    assert.ok(css.includes('--blue:#688af4'));
    assert.equal(w.document.querySelectorAll('script[src],link[href]').length, 2);
    assert.equal(w.document.querySelectorAll('img[src],iframe').length, 0);
    assert.ok(css.includes('@media(max-width:600px)'));
    assert.doesNotMatch(w.document.body.textContent, /Codex/);
  } finally { w.close(); }
});

test('embedded workspace reports intrinsic height after resize without viewport-height feedback or code clipping',()=>{
  const html=readFileSync(new URL('../public/animations/task-workspace.html',import.meta.url),'utf8');
  const script=readFileSync(new URL('../public/animations/task-workspace.js',import.meta.url),'utf8');
  const css=readFileSync(new URL('../public/animations/task-workspace.css',import.meta.url),'utf8');
  const dom=new JSDOM(html,{runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,messages=[];
  let measure=570,resize;
  Object.defineProperty(w,'parent',{value:{postMessage:data=>messages.push(data)}});
  w.matchMedia=()=>({matches:false,addEventListener(){}});
  w.document.querySelector('.workspace').getBoundingClientRect=()=>({height:measure});
  w.ResizeObserver=class{constructor(fn){resize=fn;}observe(){}};
  try{
    const style=w.document.createElement('style');style.textContent=css;w.document.head.append(style);
    w.eval(script);
    const heights=()=>messages.filter(m=>m.type==='task-height');
    assert.equal(heights().at(-1).height,608); // 570 + body padding 18*2 + 2.
    resize();assert.equal(heights().length,1,'Identical height must not cause an iframe feedback loop');
    measure=652;resize();assert.equal(heights().at(-1).height,690);
    assert.equal(w.getComputedStyle(w.document.body).minHeight,'0px');
    assert.equal(w.getComputedStyle(w.document.querySelector('.code pre')).maxHeight,'none');
    assert.equal(w.getComputedStyle(w.document.querySelector('.code pre')).overflow,'visible');
  }finally{w.close();}
});

async function sculptureFixture(failLoad = false) {
  const html = readFileSync(new URL('../public/index.html', import.meta.url), 'utf8');
  const script = readFileSync(new URL('../public/scripts/sculpture.js', import.meta.url), 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window, observers = [], calls = [], viewCalls = [];
  let hidden = false, mediaChange, interactions, loads = 0;
  const media = { matches: false, addEventListener: (_, callback) => { mediaChange = callback; } };
  w.matchMedia = () => media;
  Object.defineProperty(w.document, 'hidden', { get: () => hidden });
  w.IntersectionObserver = class {
    constructor(cb, options) { this.cb = cb; this.options = options; observers.push(this); }
    observe() {} disconnect() {}
  };
  w.__loadViewer = async () => {
    loads++;
    if (failLoad) throw Error('Simulated WebGL/model failure');
    return { createSculptureViewer: async (_, onInteraction) => {
      interactions = onInteraction;
      return { setPlayback: (...args) => calls.push(args), setView: value => viewCalls.push(value), dispose() {} };
    } };
  };
  w.document.addEventListener('zhihui:motion-request', () => {
    w.document.documentElement.removeAttribute('data-reduced-motion');
    w.document.documentElement.removeAttribute('data-home-flow-paused');
    w.document.documentElement.setAttribute('data-motion-override', '');
    w.document.dispatchEvent(new w.Event('zhihui:motion-change'));
  });
  // Replace only the module boundary; execute the actual controller with a renderer double.
  w.eval(script.replace("import('/scripts/sculpture-viewer.js')", 'window.__loadViewer()'));
  const settle = () => new Promise(resolve => setImmediate(resolve));
  return {
    w, calls, viewCalls, settle, get loads() { return loads; },
    async approach() { observers[0].cb([{ isIntersecting: true }]); await settle(); },
    visible(value) { observers[1].cb([{ isIntersecting: value }]); },
    background(value) { hidden = value; w.document.dispatchEvent(new w.Event('visibilitychange')); },
    reduce(value) {
      media.matches = value;
      w.document.documentElement.removeAttribute('data-motion-override');
      w.document.documentElement.toggleAttribute('data-reduced-motion', value);
      mediaChange();
    },
    interact() { interactions(); },
    click(id) { w.document.querySelector(id).click(); },
  };
}

test('sculpture lazy loads once and respects manual, global, reduced, background and offscreen pause', async () => {
  const f = await sculptureFixture();
  try {
    assert.equal(f.loads, 0);
    await f.approach(); await f.approach();
    assert.equal(f.loads, 1);
    assert.equal(f.w.document.getElementById('thinkerGallery').dataset.state, 'ready');
    f.visible(true); assert.deepEqual(f.calls.at(-1), [true, true]);
    f.click('[data-sculpture-view=detail]');
    assert.equal(f.viewCalls.at(-1), 'detail');
    assert.deepEqual(f.calls.at(-1), [false, true]);
    f.click('#thinkerMotion'); assert.deepEqual(f.calls.at(-1), [true, true]);
    f.visible(false); assert.deepEqual(f.calls.at(-1), [true, false]);
    f.visible(true); f.background(true); assert.deepEqual(f.calls.at(-1), [false, true]);
    f.background(false); f.reduce(true); assert.deepEqual(f.calls.at(-1), [false, true]);
    f.click('#thinkerMotion'); assert.deepEqual(f.calls.at(-1), [true, true]);
    f.w.document.documentElement.setAttribute('data-home-flow-paused', '');
    f.w.document.dispatchEvent(new f.w.Event('zhihui:motion-change'));
    assert.deepEqual(f.calls.at(-1), [false, true]);
    f.click('#thinkerMotion'); f.interact();
    assert.deepEqual(f.calls.at(-1), [false, true]);
    f.click('#thinkerReset'); assert.equal(f.viewCalls.at(-1), 'full');
    assert.equal(f.w.document.querySelector('[data-sculpture-view=full]').getAttribute('aria-pressed'), 'true');
    f.w.dispatchEvent(new f.w.Event('pagehide'));
    assert.deepEqual(f.calls.at(-1), [false, true]);
  } finally { f.w.close(); }
});

test('sculpture loading failure keeps its poster and disables nonfunctional controls', async () => {
  const f = await sculptureFixture(true);
  try {
    await f.approach();
    assert.equal(f.w.document.getElementById('thinkerGallery').dataset.state, 'error');
    assert.ok(f.w.document.getElementById('thinkerStatus').textContent.includes('静态渲染'));
    assert.ok([...f.w.document.querySelectorAll('.sculpture-controls button')].every(button => button.disabled));
    await f.approach(); assert.equal(f.loads, 1);
  } finally { f.w.close(); }
});
